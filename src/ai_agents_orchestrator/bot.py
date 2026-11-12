"""Chat bot in "control panel" style: slash commands + buttons for every action that changes state.

Channel-agnostic: it talks to the service through `Servico.notificar` and receives updates in the Telegram update
shape (`message` / `callback_query`), which every channel adapter produces (see canal.py).
"""
from __future__ import annotations

import json
import queue
import re
import unicodedata
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import cartao, engines, historico, sanitize, workspace
from .servico import Servico, hora_br
from .watch import ESTADOS_EN

CACHE_SEG = 60  # /usage and /model: each query starts a Codex process
LENTOS = ('usage', 'model', 'effort')  # commands that run subprocesses: own lane, never block the others
# Portuguese command names kept as aliases (the project started in pt-BR).
ALIASES = {"start": "help", "ajuda": "help", "tarefa": "task", "quadro": "board", "contexto": "context",
           "retomar": "resume", "pausar": "pause", "cancelar": "cancel", "ultimo": "last", "historico": "history",
           "limite": "limit", "teto": "cap", "novo": "new", "motor": "engine"}

AJUDA = """Backup agent (Codex). Commands:
/status - engine, task, queue, Claude state
/model - show/change the Codex model (buttons)
/effort [level] - show/change Codex reasoning effort (buttons)
/usage - plan usage and limits
/engine - codex | claude | auto
/repos, /repo <name> - allowed repositories / default
/task [repo] <text> - create a formal task (asks for confirmation)
Text without a slash: conversation with Codex, which edits code on an agent/chat-* branch (Publish/Review/Discard buttons)
/new - end the chat session and start a new branch
/board - Claude and Codex tasks (buttons)
/context - Codex checks the briefing now
/resume, /pause, /cancel - control the queue
/last, /diff, /branches - results
/logs - latest log lines (no secrets)
/history [N] - last N messages of this chat (default 15)
/auto - automatic continuation on/off
/cap [n] - show/adjust the daily cap of Codex limit checks
/limit - latest evidence of a Claude usage limit
Every action that changes something asks for confirmation with a button (expires in 10 min).
A production deploy button only exists after a confirmed merge on GitHub."""

CONFIRMA = [("Confirm", "sim"), ("Cancel", "nao")]


RE_PUBLICAR = re.compile(r"\b(publi\w*|sub(ir|a|e|o)\b|produc\w*|deploy\w*|release|ship\w*|lancar|go live|no ar|merge|push)")
AVISO_SEM_PUBLICAR = ("[Backup agent rule: do NOT publish, do NOT merge/push into the base, no deploy/production. "
                      "Only prepare the work on the branch; publishing only happens via the owner's button.]")


class Bot:
    def __init__(self, svc: Servico):
        self.s = svc
        svc.bot = self
        self.cfg, self.estado, self.quadro = svc.cfg, svc.estado, svc.quadro
        self._cache: dict = {}
        self._cache_lock = threading.Lock()
        self._fila: queue.Queue = queue.Queue()
        self._rapida: threading.Thread | None = None
        self._lenta = ThreadPoolExecutor(max_workers=3, thread_name_prefix="bot-lento")
        self._conversa = ThreadPoolExecutor(max_workers=1, thread_name_prefix="bot-conversa")

    # ------------------------------------------------------------ despacho (fios)
    def despachar(self, u: dict) -> None:
        """Called by the channel's receive loop: never blocks. Fast lane = 1 thread, in order (confirmations and text
        depend on order); slow lane = pool for commands that run subprocesses."""
        if self._eh_lento(u):
            self._lenta.submit(self._seguro, u)
            return
        if self._rapida is None or not self._rapida.is_alive():
            self._rapida = threading.Thread(target=self._laco_rapido, daemon=True, name="bot-rapido")
            self._rapida.start()
        self._fila.put(u)

    def _eh_lento(self, u: dict) -> bool:
        m = u.get("message")
        if not m or m.get("chat", {}).get("id") != self.cfg.chat_id:
            return False
        t = (m.get("text") or "").strip()
        return t.startswith("/") and t[1:].partition(" ")[0].split("@")[0].lower() in LENTOS

    def _laco_rapido(self) -> None:
        while True:
            self._seguro(self._fila.get())

    def _seguro(self, u: dict) -> None:
        try:
            self.processar(u)
        except Exception as e:
            self.s.log(f"bot: {type(e).__name__}: {e}")

    def _cacheado(self, chave: str, fn, valido=lambda v: True):
        agora = self.s.agora()
        with self._cache_lock:
            c = self._cache.get(chave)
            if c and agora - c[0] < CACHE_SEG:
                return c[1]
        v = fn()  # outside the lock: two simultaneous queries only repeat the work
        if valido(v):
            with self._cache_lock:
                self._cache[chave] = (agora, v)
        return v

    # ------------------------------------------------------------ input
    def processar(self, u: dict) -> None:
        if "callback_query" in u:
            return self._callback(u["callback_query"])
        m = u.get("message")
        if not m:
            return
        if m.get("chat", {}).get("id") != self.cfg.chat_id or m.get("chat", {}).get("type") != "private" \
                or (m.get("from") or {}).get("id") != self.cfg.chat_id:
            return  # total silence for any other chat
        texto = (m.get("text") or "").strip()
        if not texto:
            return
        self.s.hist.gravar(historico.DONO, texto)
        if len(texto) > self.cfg.max_msg_in:
            return self.resp(f"Message too long (limit {self.cfg.max_msg_in} characters).")
        if texto.startswith("/"):
            cmd, _, arg = texto.partition(" ")
            cmd = cmd[1:].split("@")[0].lower()
            return self.comando(cmd, arg.strip())
        return self.texto_livre(texto)

    def resp(self, texto: str, botoes=None) -> None:
        self.s.notificar(texto, botoes)

    def _callback(self, cq: dict) -> None:
        msg = cq.get("message") or {}
        if (cq.get("from") or {}).get("id") != self.cfg.chat_id or msg.get("chat", {}).get("id") != self.cfg.chat_id:
            return  # never answers anyone else
        kind, payload, op = self.s.botoes.validar(cq.get("data", ""), self.cfg.chat_id, self.s.agora())
        if kind is not None:  # only the action label: never the HMAC/payload
            self.s.hist.gravar(historico.DONO, f"[button] {kind}/{op}")
        tg = self.s.tg
        if kind is None:
            motivo = {"expirada": "This button expired.", "usada": "This button was already used."}.get(payload, "Invalid button.")
            if tg:
                tg.responder_callback(cq["id"], motivo)
            return
        if tg:
            tg.responder_callback(cq["id"], "ok")
            if msg.get("message_id"):
                tg.remover_botoes(msg["message_id"])
        self.executar_acao(kind, payload, op)

    # ------------------------------------------------------------ commands
    def comando(self, cmd: str, arg: str) -> None:
        cmd = ALIASES.get(cmd, cmd)
        f = getattr(self, "c_" + cmd, None)
        if not f:
            return self.resp("Unknown command. /help")
        f(arg)

    def c_help(self, _a=""):
        self.resp(AJUDA)

    def c_status(self, _a=""):
        d = self.s.status()
        b = d["briefing"]
        bt = f"{b['idade_min']} min, {b['linhas']} lines" + (" (STALE)" if b["velho"] else "") if b["existe"] else "missing"
        self.resp("\n".join([
            f"Engine: {d['motor']} | Model: {d['modelo']} | Effort: {d['esforco']}",
            f"Codex: {'logged in' if d['codex_logado'] else 'NOT LOGGED IN'}" + (f", at its limit until {d['codex_limite_ate']}" if d["codex_limite_ate"] else ""),
            f"Queue: {'PAUSED' if d['pausado'] else 'active'} | running: {('#' + str(d['tarefa_atual'])) if d['tarefa_atual'] else 'nothing'} | queued: {d['fila']}",
            f"Claude: {d['claude']} | fallback active: {'yes' if d['fallback_ativo'] else 'no'} | /auto: {'on' if d['auto'] else 'off'}",
            f"Armed for the hook: {'yes' if d['armado'] else 'no'} | limit checks today: {d['acordadas_hoje']}/{d['teto_acordadas']}",
            f"Briefing: {bt}",
            f"Last handoff: {d['ultimo_handoff'] or '-'}"]))

    def c_model(self, _a=""):
        atual = self.estado.get("modelo") or "Codex default"
        mid = self.s.notificar("⏳ checking the models…")
        mods = self._cacheado("modelos", lambda: engines.modelos_codex(self.cfg.state_dir, self.cfg.codex_bin), bool)[:8]
        opcoes = [("Codex default", "m:")] + [(m["nome"] or m["slug"], "m:" + m["slug"]) for m in mods] + [("Cancel", "nao")]
        kb = self.s.botoes.teclado("model", {}, opcoes, linhas=2)
        self.s.editar(mid, f"Current model: {atual}. Choose (availability depends on your plan):", kb)

    def c_effort(self, arg=""):
        modelo = self.estado.get("modelo")
        mods = self._cacheado("modelos", lambda: engines.modelos_codex(self.cfg.state_dir, self.cfg.codex_bin), bool)
        niveis = engines.niveis_effort(mods, modelo)
        atual = self.estado.get("esforco") or "Codex default"
        quando = f"Model: {modelo or 'Codex default'}. Current effort: {atual}."
        arg = arg.strip().lower()
        if arg:
            if arg in ("padrao", "default"):
                self.estado.set("esforco", None)
                return self.resp(f"Effort: Codex default. Model: {modelo or 'Codex default'}.")
            if arg not in niveis:
                return self.resp(f"Invalid level. Valid: {', '.join(niveis)} (or default). {quando}")
            self.estado.set("esforco", arg)
            return self.resp(f"Effort: {arg}. {quando.split('. Current effort')[0]}.")
        opcoes = [("Codex default", "e:")] + [(n, "e:" + n) for n in niveis] + [("Cancel", "nao")]
        kb = self.s.botoes.teclado("effort", {}, opcoes, linhas=3)
        self.resp(f"{quando} Choose the level:", kb)

    def c_usage(self, _a="", forcar: bool = False):
        mid = self.s.notificar("⏳ checking plan usage…")
        if forcar:  # [Refresh]/[Try again]: explicit request, skip the 60 s cache
            with self._cache_lock:
                self._cache.pop("uso", None)
        u = self._cacheado("uso", lambda: engines.uso_codex(self.cfg.state_dir, self.cfg.codex_bin), lambda v: bool(v.get("ok")))
        agora = self.s.agora()
        if not u.get("ok"):
            kb = self.s.botoes.teclado("usage", {}, [("Try again", "atualizar")])
            return self.s.editar(mid, cartao.cartao_erro(u.get("erro")), kb, html=True)
        bot = {"modelo": self.estado.get("modelo") or "Codex default", "esforco": self.estado.get("esforco") or "Codex default",
               "acordadas": self.estado.contagem_dia("acordadas", self.s.hoje()), "teto": self.s.teto_acordadas(),
               "tarefas_hora": len(self.estado.execucoes_desde(agora - 3600, "tarefa")), "teto_tarefas": self.cfg.tarefas_por_hora}
        kb = self.s.botoes.teclado("usage", {}, [("Refresh", "atualizar")])
        self.s.editar(mid, cartao.cartao_usage(u, agora, bot), kb, html=True)

    def c_engine(self, _a=""):
        kb = self.s.botoes.teclado("motor", {}, [("Codex", "codex"), ("Claude", "claude"), ("Auto", "auto"), ("Cancel", "nao")], linhas=2)
        extra = "" if (self.s.claude and self.s.claude.disponivel()) else " (headless Claude has no credentials: auto/claude use Codex)"
        self.resp(f"Current engine: {self.estado.get('motor', 'codex')}{extra}.", kb)

    def c_repos(self, _a=""):
        padrao = self.estado.get("repo_padrao")
        self.resp("Allowed repositories:\n" + "\n".join(
            f"- {n}: {s.get('descricao') or s.get('description', '')}" + (" (default)" if n == padrao else "") for n, s in self.cfg.repos.items()))

    def c_repo(self, arg: str):
        if arg not in self.cfg.repos:
            return self.resp("Use /repo <name>. See /repos.")
        kb = self.s.botoes.teclado("repo", {"repo": arg}, CONFIRMA)
        self.resp(f"Set '{arg}' as the default repository?", kb)

    def c_task(self, arg: str):
        if not arg:
            return self.resp("Use /task [repo] <description>.")
        primeira, _, resto = arg.partition(" ")
        if primeira in self.cfg.repos and resto.strip():
            return self._confirmar_tarefa(primeira, resto.strip())
        repo = self._repo_plausivel(arg)
        if repo:
            return self._confirmar_tarefa(repo, arg)
        self._escolher_repo(arg)

    def _repo_plausivel(self, texto: str) -> str | None:
        """Single plausible repo: mentioned in the text, or the default (/repo), or the only allowed one. None = ask."""
        n = self._norm(texto.replace(AVISO_SEM_PUBLICAR, ""))
        citados = [r for r in self.cfg.repos if self._norm(r) in n]
        if len(citados) == 1:
            return citados[0]
        if citados:
            return None
        if self._fala_do_bot(n):
            return self.cfg.self_repo if self.cfg.self_repo in self.cfg.repos else None
        padrao = self.estado.get("repo_padrao")
        if padrao in self.cfg.repos:
            return padrao
        if len(self.cfg.repos) == 1:
            return next(iter(self.cfg.repos))
        return None

    def _fala_do_bot(self, n: str) -> bool:
        """Is the request about this orchestrator itself? Only matters when `service.self_repo` is configured."""
        if not self.cfg.self_repo:
            return False
        return bool(re.search(r"orchestrator|\b(bot|backup agent|agente reserva|telegram|whatsapp|effort|esforco|daily cap|teto|acionamento\w*|buttons?|botoes|comando|command)\b", n))

    def _repo_citado(self, texto: str) -> str | None:
        n = self._norm(texto.replace(AVISO_SEM_PUBLICAR, ""))
        citados = [r for r in self.cfg.repos if self._norm(r) in n]
        if len(citados) == 1:
            return citados[0]
        if not citados and self.cfg.self_repo in self.cfg.repos and self._fala_do_bot(n):
            return self.cfg.self_repo  # request about the bot itself: pick its own repo
        return None

    def c_new(self, _a=""):
        self.resp("Chat session closed. The next message starts a new branch." if self.s.chat_encerrar()
                  else "There was no open chat session; the next message starts a new one.")

    def _escolher_repo(self, texto: str) -> None:
        if not self.cfg.repos:
            return self.resp("No allowed repositories are configured.")
        opcoes = [(r, "r:" + r) for r in self.cfg.repos] + [("Cancel", "nao")]
        kb = self.s.botoes.teclado("escolher_repo", {"texto": texto}, opcoes, linhas=2)
        self.resp("In which repository should I create this task?\n" + texto[:300], kb)

    def c_cap(self, arg: str):
        atual = self.s.teto_acordadas()
        hoje = self.estado.contagem_dia("acordadas", self.s.hoje())
        if not arg:
            return self.resp(f"Daily cap of Codex limit checks (Claude usage limit): {atual}; used today: {hoje}. "
                             "Only real Codex calls count. Adjust with /cap <1-100>.")
        if not arg.isdigit() or not 1 <= int(arg) <= 100:
            return self.resp("Use /cap <number from 1 to 100>.")
        self.estado.set("teto_acordadas", int(arg))
        self.resp(f"Daily cap set to {int(arg)} (used today: {hoje}).")

    def _confirmar_tarefa(self, repo: str, texto: str):
        kb = self.s.botoes.teclado("tarefa", {"repo": repo, "texto": texto},
                                   [("Confirm", "sim"), ("Edit", "editar"), ("Cancel", "nao")])
        self.resp(f"Create a Codex task in '{repo}'?\n{texto[:600]}", kb)

    def c_board(self, _a=""):
        ts = [t for t in self.quadro.listar(limite=40) if t["estado"] not in ("concluida", "cancelada")][:6]
        if not ts:
            return self.resp("Board is empty.")
        for t in ts:
            kb = self.s.botoes.teclado("quadro", {"tid": t["id"]},
                                       [("View", "ver"), ("Assign to Codex", "codex"), ("Assign to Claude", "claude"), ("Cancel", "cancelar")], linhas=2)
            self.resp(f"#{t['id']} [{t['dono']}] {ESTADOS_EN.get(t['estado'], t['estado'])} - {t['titulo']} ({t['repo'] or '-'})" +
                      (f"\nWarning: {t['aviso']}" if t["aviso"] else ""), kb)

    def c_context(self, _a=""):
        bi = self.s.briefing_info()
        kb = self.s.botoes.teclado("contexto", {}, CONFIRMA)
        self.resp("Codex will read BRIEFING.md and check the transcript tail and the tasks." +
                  (f" Briefing is {bi['idade_min']} min old." if bi["existe"] else " There is no briefing.") + " Confirm?", kb)

    def c_resume(self, _a=""):
        self.resp("Resume the Codex queue?", self.s.botoes.teclado("retomar", {}, CONFIRMA))

    def c_pause(self, _a=""):
        self.resp("Pause the queue (the current task finishes)?", self.s.botoes.teclado("pausar", {}, CONFIRMA))

    def c_cancel(self, _a=""):
        if not self.s.tarefa_atual:
            return self.resp("No task in progress.")
        self.resp(f"Cancel task #{self.s.tarefa_atual} in progress?", self.s.botoes.teclado("cancelar", {}, CONFIRMA))

    def c_auto(self, _a=""):
        kb = self.s.botoes.teclado("auto", {}, [("Turn on", "ligar"), ("Turn off", "desligar"), ("Cancel", "nao")])
        self.resp(f"Automatic continuation: {'on' if self.s.auto() else 'off'}.", kb)

    def _ultimo(self):
        tid = self.estado.get("ultimo_resultado")
        return self.quadro.ver(tid) if tid else None

    def c_last(self, _a=""):
        t = self._ultimo()
        if not t:
            return self.resp("No result yet.")
        self.resp(f"#{t['id']} {t['titulo']} [{ESTADOS_EN.get(t['estado'], t['estado'])}]\nBranch: {t['branch']}\nPatch: {t['patch']}\n"
                  f"HANDOFF: {self.cfg.resultados}/{t['id']}/HANDOFF.md\n\n{(t['resumo'] or '')[:1500]}")

    def c_diff(self, _a=""):
        t = self._ultimo()
        if not t or not t["workspace"]:
            return self.resp("No branch to compare.")
        base = workspace.base_do_repo(self.cfg, t["repo"])
        try:
            st = workspace.git(self.cfg, ["diff", "--stat", f"origin/{base}...HEAD"], t["workspace"]).stdout
        except workspace.GitErro as e:
            return self.resp(f"Failed: {e}")
        self.resp(f"{t['branch']} vs {base}:\n{st[-1500:] or '(no changes)'}")

    def c_branches(self, _a=""):
        ts = [t for t in self.quadro.listar(limite=30) if t["branch"]]
        self.resp("\n".join(f"#{t['id']} {t['branch']} [{ESTADOS_EN.get(t['estado'], t['estado'])}]" for t in ts) or "No agent branches.")

    def c_logs(self, _a=""):
        self.resp("\n".join(list(self.s.ring)[-30:]) or "(no lines)")

    def c_history(self, arg: str = ""):
        try:
            n = min(max(int(arg.split()[0]), 1), 100) if arg.strip() else 15
        except ValueError:
            return self.resp("Usage: /history [N]")
        regs = historico.consultar(self.s.hist.caminho, n)
        linhas = [historico.formatar(r, 160) for r in regs]
        saida, tam = [], 0
        for l in reversed(linhas):  # trim to the channel limit keeping the most recent
            if tam + len(l) + 1 > self.cfg.max_msg_out - 50:
                break
            saida.append(l); tam += len(l) + 1
        self.resp("\n".join(reversed(saida)) or "(empty history)")

    def c_limit(self, _a=""):
        cl, v = self.estado.get("claude_limite", {}), self.estado.get("ultimo_veredito", {})
        if not cl and not v:
            return self.resp("No limit evidence recorded.")
        self.resp(f"Last Codex verdict: {v.get('limite', '-')} ({v.get('evidencia', '')[:200]})\n"
                  f"Episode: {'ACTIVE' if cl.get('ativo') else 'over'}; reset {hora_br(cl.get('reset_epoch'))}; "
                  f"type {cl.get('tipo') or '-'}\nEvidence: {cl.get('evidencia', '-')[:250]}")

    # ------------------------------------------------------------ free text
    def texto_livre(self, texto: str):
        ed = self.estado.get("edit_pending")
        if ed:
            self.estado.delete("edit_pending")
            return self._confirmar_tarefa(ed["repo"], texto)
        aj = self.estado.get("ajuste_pending")
        if aj:
            self.estado.delete("ajuste_pending")
            kb = self.s.botoes.teclado("ajuste", {"tids": aj["tids"], "texto": texto}, CONFIRMA)
            return self.resp(f"Apply this instruction to tasks {aj['tids']} and continue?\n{texto[:500]}", kb)
        revisoes = self._revisoes_do_dono()
        if revisoes and self._pedido_publicar(texto) and self._aprovacao_curta(texto):
            return self._mostrar_revisoes(revisoes)
        if self._pedido_publicar(texto):
            texto = texto.rstrip() + "\n\n" + AVISO_SEM_PUBLICAR
        self._conversa.submit(self._seguro_conversa, texto)  # conversation with Codex: never blocks the fast lane

    def _seguro_conversa(self, texto: str) -> None:
        try:
            self.s.conversar(texto, self._repo_citado(texto), self._repo_plausivel(texto))
        except Exception as e:
            self.s.log(f"conversation: {type(e).__name__}: {e}")
            self.resp("I could not talk to Codex right now.")

    @staticmethod
    def _norm(texto: str) -> str:
        t = unicodedata.normalize("NFKD", texto.lower())
        return "".join(c for c in t if not unicodedata.combining(c))

    def _pedido_publicar(self, texto: str) -> bool:
        return bool(RE_PUBLICAR.search(self._norm(texto)))

    def _aprovacao_curta(self, texto: str) -> bool:
        return len(texto) <= 60 and len(texto.split()) <= 8

    def _revisoes_do_dono(self) -> list[dict]:
        return [t for t in self.quadro.listar(("em_revisao",), limite=40)
                if t["kind"] in ("dev", "chat") and t["origem"] in ("dono", "handoff")]

    def _mostrar_revisoes(self, revisoes: list[dict]) -> None:
        if len(revisoes) == 1:
            return self._quadro({"tid": revisoes[0]["id"]}, "ver")
        self.resp("There is more than one task in review. Which one do you want to publish?")
        for t in revisoes[:6]:
            kb = self.s.botoes.teclado("quadro", {"tid": t["id"]}, [("View", "ver"), ("Cancel", "cancelar")])
            self.resp(f"#{t['id']} [{t['origem']}] {t['titulo']} ({t['repo'] or '-'})", kb)

    # ------------------------------------------------------------ confirmed actions
    def executar_acao(self, kind: str, p: dict, op: str) -> None:
        s = self.s
        if op == "nao":
            return self.resp("Ok, cancelled.")
        if kind == "tarefa":
            if op == "editar":
                self.estado.set("edit_pending", {"repo": p["repo"]})
                return self.resp("Send the new task text.")
            t = self.quadro.criar(p["texto"][:80], p["texto"], p["repo"], "codex", "dono")
            return self.resp(f"Task #{t['id']} queued.")
        if kind == "escolher_repo":
            return self._confirmar_tarefa(op[2:], p["texto"])
        if kind == "instrucao":
            ft = self.estado.get("fallback_thread") or {}
            base = self.quadro.ver(ft.get("tid", 0)) or {}
            repo = base.get("repo") or self.estado.get("repo_padrao") or next(iter(self.cfg.repos), "")
            t = self.quadro.criar("Owner instruction", p["texto"], repo, "codex", "dono", instrucoes=p["texto"])
            self.quadro.atualizar(t["id"], workspace=ft.get("ws"), thread_id=ft.get("thread"))
            return self.resp(f"Instruction sent (#{t['id']}).")
        if kind == "motor":
            self.estado.set("motor", op)
            return self.resp(f"Engine: {op}.")
        if kind == "model":
            self.estado.set("modelo", op[2:] or None)
            return self.resp(f"Model: {op[2:] or 'Codex default'}.")
        if kind == "usage":  # slow query: off the fast lane, never blocks the other buttons
            self._ultimo_lento = self._lenta.submit(lambda: self.c_usage(forcar=True))
            return
        if kind == "effort":
            self.estado.set("esforco", op[2:] or None)
            return self.resp(f"Effort: {op[2:] or 'Codex default'}.")
        if kind == "repo":
            self.estado.set("repo_padrao", p["repo"])
            return self.resp(f"Default repository: {p['repo']}.")
        if kind == "pausar":
            self.estado.set("pausado", True)
            return self.resp("Queue paused.")
        if kind == "retomar":
            self.estado.set("pausado", False)
            return self.resp("Queue resumed.")
        if kind == "cancelar":
            return self.resp(str(s.cancelar_tarefa(s.tarefa_atual).get("obs", "cancelled")) if s.tarefa_atual else "Nothing in progress.")
        if kind == "auto":
            self.estado.set("auto", op == "ligar")
            return self.resp(f"Automatic continuation {'on' if op == 'ligar' else 'off'}.")
        if kind == "contexto":
            self.resp("Checking...")
            return self._bg(lambda: s.claude_limite(None, manual=True))
        if kind == "handoff":
            return self._handoff(p, op)
        if kind == "ajuste":
            for tid in p["tids"]:
                self.quadro.atualizar(tid, instrucoes=p["texto"], estado="pendente", aviso=None)
            return self.resp("Instruction recorded; continuing.")
        if kind == "volta":
            if op == "pausar":
                self.estado.set("pausado", True)
                return self.resp("Codex paused (the current task finishes).")
            return self.resp("Ok, Codex keeps running and Claude splits the work with it.")
        if kind == "quadro":
            return self._quadro(p, op)
        if kind == "publicar":
            return self._publicar(p, op)
        if kind == "producao":
            self.resp("Running the production deploy...")
            return self._bg(lambda: self._producao(p))
        self.resp("Unknown action.")

    def _bg(self, fn):
        threading.Thread(target=fn, daemon=True).start()

    def _handoff(self, p, op):
        if op == "continuar":
            for tid in p["tids"]:
                self.quadro.atualizar(tid, estado="pendente", aviso=None)
            return self.resp("Continuing the tasks.")
        if op == "ajustar":
            self.estado.set("ajuste_pending", {"tids": p["tids"]})
            return self.resp("Send the text with the adjustment.")
        self.resp("Ok, waiting. The tasks stay blocked until you send /board or /context.")

    def _quadro(self, p, op):
        t = self.quadro.ver(p["tid"])
        if not t:
            return self.resp("Task not found.")
        if op == "ver":
            txt = (f"#{t['id']} [{t['dono']}] {ESTADOS_EN.get(t['estado'], t['estado'])} - {t['titulo']}\nRepo: {t['repo']} Branch: {t['branch'] or '-'}\n"
                   f"Files: {', '.join(t['alterados'] or t['arquivos']) or '-'}\n{(t['resumo'] or t['descricao'] or '')[:1200]}")
            kb = None
            if t["estado"] == "em_revisao" and t["kind"] in ("dev", "chat"):
                ultimo = ("Discard" if t["kind"] == "chat" else "Don't publish", "descartar" if t["kind"] == "chat" else "nao")
                opcoes = [("Review first", "revisar"), ultimo]
                if not workspace.sem_remoto(self.cfg, t["repo"]):
                    opcoes.insert(0, ("Publish", "publicar"))
                kb = self.s.botoes.teclado("publicar", self.s.aprovacao_publicacao(t["id"]), opcoes)
            return self.resp(txt, kb)
        if op in ("codex", "claude"):
            campos = {"dono": op}
            if t["estado"] in ("bloqueada",):
                campos["estado"] = "pendente"
            self.quadro.atualizar(t["id"], **campos)
            return self.resp(f"#{t['id']} assigned to {op}.")
        if op == "cancelar":
            return self.resp(str(self.s.cancelar_tarefa(t["id"]).get("obs", f"#{t['id']} cancelled.")))

    def _publicar(self, p, op):
        t = self.quadro.ver(p["tid"])
        if not t:
            return self.resp("Task not found.")
        if op == "nao":
            return self.resp("Ok, nothing published. The branch is still available in /branches.")
        if op == "descartar":
            sessao = self.estado.get("chat_sessao") or {}
            if sessao.get("tid") == t["id"]:
                self.s.chat_encerrar()
            self.quadro.atualizar(t["id"], estado="cancelada", erro="discarded by the owner")
            return self.resp("Discarded. The chat session ended; the branch stays in the workspace and on GitHub (branches are never deleted). "
                             "The next message starts a new branch.")
        if op == "revisar":
            self.quadro.criar(f"Review branch {t['branch']} before publishing", f"Task #{t['id']} ({t['titulo']}). Do a cross-review.",
                              t["repo"], "claude", "codex", t["alterados"] or [])
            self.s.gerar_recado()
            return self.resp("I asked Claude for a review (note and board). When you want to publish, open /board > View.")
        self.resp("Merging and pushing to GitHub (no production deploy)...")
        self._bg(lambda: self._publicar_bg(t["id"], p))

    def _publicar_bg(self, tid: int, aprovacao: dict):
        r = self.s.publicar_tarefa(tid, aprovacao)
        if not r["ok"]:
            return self.resp(f"Publishing to GitHub failed: {r['erro']}")
        t = self.quadro.ver(tid)
        if (self.estado.get("chat_sessao") or {}).get("tid") == tid:
            self.s.chat_encerrar()  # published: the next request opens a new branch
        info = self.s.root.info(t["repo"])
        msg = f"Published on GitHub: {r['base']} at {r['hash'][:10]}."
        if info.get("deploy"):
            kb = self.s.botoes.teclado("producao", {"tid": tid, "acao": "deploy"}, [("Deploy to production", "producao"), ("Not now", "nao")])
            return self.resp(msg + "\nThis repository has a deploy configured. Deploy to production?", kb)
        self.resp(msg + "\nNo automatic deploy is configured for this repository; production is handled outside the bot.")

    def _producao(self, p):
        r = self.s.producao_tarefa(p["tid"], p.get("acao", "deploy"))
        self.resp(("Production deploy finished." if r.get("ok") else f"Production deploy failed: {r.get('erro') or 'see output'}") + "\n" + sanitize.mascarar(str(r.get("saida", "")))[-500:])
