"""`aao` / `ai-agents-orchestrator` CLI: service, optional root daemon, setup/doctor and local commands for Claude Code
(through a local Unix socket or authenticated loopback TCP).

Portuguese subcommand names from the project's origins are accepted as aliases (e.g. `delegar` = `delegate`).
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import date
from pathlib import Path

from . import __version__, compat, config, engines, espelho, historico, ipc, regras, workspace
from . import root_daemon
from .bot import Bot
from .config import CLI
from .db import Estado, Quadro
from .servico import Servico, publicar_base, aprovacao_publicacao

FINAIS = ("em_revisao", "concluida", "bloqueada", "cancelada")
ESTADOS_CLI = {"pendente": "pending", "em_andamento": "in progress", "em_revisao": "in review",
               "concluida": "completed", "bloqueada": "blocked", "cancelada": "cancelled"}
FILTROS_ESTADO = {label: state for state, label in ESTADOS_CLI.items()}
FILTROS_ESTADO.update(running="em_andamento", review="em_revisao")
AVISO_SEM_CANAL = ("Without a chat channel you cannot send prompts to Codex from your phone, receive its notifications, "
                   "or approve publishing with a button. Telegram is the channel that enables this "
                   "(WhatsApp is available as an unofficial, at-your-own-risk alternative).")


def chamar(cfg, req: dict, timeout: float | None = 30) -> dict:
    try:
        connection, token = ipc.connect(cfg, 30 if timeout is None else timeout)
        with connection as c:
            c.settimeout(timeout)
            c.sendall((json.dumps({"token": token, **req} if token else req) + "\n").encode())
            dados = b""
            while not dados.endswith(b"\n"):
                p = c.recv(65536)
                if not p:
                    break
                dados += p
        return json.loads(dados.decode())
    except (OSError, ValueError):
        fix = "run aao serve" if cfg.mode == "lite" else f"systemctl status {config.NOME}"
        return {"ok": False, "erro": f"service unavailable ({fix})"}


def _cfg():
    env = dict(os.environ)
    cfg = config.carregar(env)
    if cfg.mode == "lite":
        return cfg
    for k, v in config.ler_env_file().items():
        env.setdefault(k, v)
    return config.carregar(env)


def _imprimir(d: dict) -> int:
    print(json.dumps(d, ensure_ascii=False, indent=1, default=str))
    return 0 if d.get("ok", True) else 1


def criar_canal(cfg):
    """Chat channel chosen in the config (`service.channel`). None = CLI/Claude Code only."""
    if cfg.channel == "telegram":
        from .telegram import Telegram
        if not cfg.token or not cfg.chat_id:
            print("telegram: AAO_TELEGRAM_TOKEN and telegram.chat_id are required (run `aao setup`)", file=sys.stderr)
            sys.exit(78)
        return Telegram(cfg.token, cfg.chat_id, cfg.max_msg_out)
    if cfg.channel == "whatsapp":
        from . import whatsapp
        print(whatsapp.AVISO_RISCO, file=sys.stderr)
        try:
            return whatsapp.criar(cfg)
        except whatsapp.WhatsAppErro as e:
            print(f"whatsapp: {e}", file=sys.stderr)
            sys.exit(78)
    print(AVISO_SEM_CANAL, file=sys.stderr)
    return None


def montar_servico(cfg, dry_run: bool, sem_telegram: bool, chave: str | None = None):
    cfg.preparar_dirs()
    estado, quadro = Estado(cfg.db_estado), Quadro(cfg.db_quadro)
    chave = os.environ.get("AAO_PUBLISH_KEY", "") if chave is None else chave
    if dry_run:
        from .testing import CodexFalso, RootFalso
        codex, claude, root = CodexFalso(cfg.state_dir), None, RootFalso()
        sem_telegram = True
    else:
        codex = engines.CodexRunner(cfg.state_dir, cfg.codex_bin, rede=cfg.codex_network, acesso_total=cfg.codex_full_access,
                                    acesso_total_leitura=cfg.codex_full_access_read_only,
                                    child_env=engines.env_usuario() if cfg.mode == "lite" else None)
        claude = engines.ClaudeRunner(cfg.state_dir, cfg.claude_bin,
                                     child_env=engines.env_usuario() if cfg.mode == "lite" else None)
        if cfg.mode == "lite":
            from .lite import LocalBackend
            root = LocalBackend(cfg)
            sem_telegram = True
        else:
            root = root_daemon.RootClient(str(cfg.root_socket), chave_hex=chave)
        if cfg.codex_full_access:
            print("WARNING: codex.full_access = true -> Codex runs with danger-full-access (NO sandbox).", file=sys.stderr)
    canal = None if sem_telegram else criar_canal(cfg)
    svc = Servico(cfg, estado, quadro, canal, codex, claude, root, sem_telegram=canal is None)
    Bot(svc)
    return svc


def _hook_prompt(cfg, evento: str) -> int:
    """UserPromptSubmit/SessionStart: stdout becomes Claude context. Note from Codex + briefing reminder."""
    partes = []
    b = chamar(cfg, {"cmd": "balanceamento"}, timeout=2)
    if b.get("ok") and b.get("instrucao"):
        partes.append(b["instrucao"])
    r = chamar(cfg, {"cmd": "recado", "limite": 2500}, timeout=5)
    if r.get("ok") and r.get("texto"):
        partes.append(r["texto"])
        # in fallback Claude "takes the baton" (tells the owner); in parallel work it only marks the note as read
        if r.get("em_fallback"):
            chamar(cfg, {"cmd": "claude_assumiu", "marcar_notas": False}, timeout=5)
    try:
        b = chamar(cfg, {"cmd": "briefing_status"}, timeout=3)
        idade = b.get("idade_min")
        con = sqlite3.connect(cfg.db_quadro.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
        trabalho = con.execute("SELECT count(*) FROM tarefas WHERE estado NOT IN ('concluida','cancelada')").fetchone()[0] > 0
        con.close()
        marca = cfg.state_dir / "lembrete-ts"
        recente = marca.exists() and time.time() - marca.stat().st_mtime < 1800
        if trabalho and not recente and (idade is None or idade > cfg.lembrete_min):
            partes.append(f"Reminder: Codex's BRIEFING.md in {cfg.para_codex}/ "
                          f"{'does not exist' if idade is None else f'is {idade} min old'}. Update it in at most 40 lines "
                          "(in progress, decisions, pending items, next steps) at the next milestone.")
            marca.touch()
    except Exception:
        pass
    if partes:
        print("\n".join(partes))
    return 0


def _publicar(cfg, tid: int, autorizado: bool = False, chave_arq: Path | None = None, root=None,
              interativo: bool | None = None, entrada=input) -> int:
    """Publishes (merge --no-ff into the base + push, via the root daemon) a task delegated by Claude. Root only, and
    only with the owner's confirmation typed in an interactive terminal (the task id). The `--approved-by-owner`
    flag is self-attested (an agent can pass it), so on its own it no longer publishes anything."""
    if cfg.mode == "lite":
        from .lite import lock
        with lock(cfg.state_dir / "publish-cli.lock"):
            return _publicar_lite(cfg, tid, root=root, interativo=interativo, entrada=entrada)
    if compat.euid() != 0:
        print("refused: must run as root")
        return 2
    if interativo is None:
        interativo = sys.stdin.isatty() and sys.stdout.isatty()
    if not interativo:
        print("refused: publishing from the CLI needs the owner at an interactive terminal (type the task id to "
              "confirm). --approved-by-owner alone is not accepted; tasks from the chat use the [Publish] button.")
        return 2
    quadro = Quadro(cfg.db_quadro)
    t = quadro.ver(tid)
    if not t:
        print("task not found")
        quadro.c.close()
        return 1
    if t["origem"] != "claude":
        print("refused: only tasks delegated by Claude are published from the CLI (chat tasks use the Publish button)"); return 2
    if t["estado"] != "em_revisao" or t.get("publicado"):
        print(f"refused: task #{tid} is '{t['estado']}' (must be em_revisao and not yet published)"); return 2
    try:
        aprovacao = aprovacao_publicacao(cfg, t)
    except workspace.GitErro as e:
        print(str(e))
        quadro.c.close()
        return 1
    try:
        digitado = (entrada(f"Publish task #{tid}, commit {aprovacao.get('commit', 'unknown')}, "
                           f"to {aprovacao.get('repo', 'unknown')}/{aprovacao.get('destino', 'unknown')}? "
                           "Type the task id to confirm: ")
                    or "").strip()
    except EOFError:
        digitado = ""
    if digitado != str(tid):
        print("refused: confirmation did not match the task id")
        return 2
    if root is None:
        try:
            chave = (chave_arq or cfg.publish_key_file).read_text().strip()
        except OSError:
            print("publish key not readable"); return 1
        root = root_daemon.RootClient(str(cfg.root_socket), chave_hex=chave)
    r = publicar_base(cfg, quadro, root, tid, time.time(), aprovacao)
    # the service runs as another user: give it back any WAL files root may have created
    try:
        st = os.stat(cfg.db_quadro)
        for suf in ("-wal", "-shm"):
            f = Path(str(cfg.db_quadro) + suf)
            if f.exists():
                os.chown(f, st.st_uid, st.st_gid)
    except OSError:
        pass
    return _imprimir(r)


def _publicar_lite(cfg, tid, *, root=None, interativo=None, entrada=input):
    from .lite import LocalBackend
    if interativo is None:
        interativo = sys.stdin.isatty() and sys.stdout.isatty()
    if not interativo:
        print("refused: aao publish requires an interactive terminal and ID SHA confirmation")
        return 2
    quadro = Quadro(cfg.db_quadro)
    try:
        t = quadro.ver(tid)
        if not t or t["estado"] != "em_revisao" or t.get("publicado"):
            print("refused: task must be reviewed and not yet published")
            return 2
        approval = aprovacao_publicacao(cfg, t)
        sha = approval.get("commit")
        if not sha:
            print("refused: task has no publishable commit")
            return 2
        try:
            answer = entrada(f"Publish #{tid}, SHA {sha}, repo {approval['repo']}, base {approval['destino']}? "
                             f"Type {tid} {sha}: ").strip()
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer != f"{tid} {sha}":
            print("refused: confirmation must match the task ID and full SHA")
            return 2
        backend = root or LocalBackend(cfg)
        backend.approved_cli = True
        try:
            return _imprimir(publicar_base(cfg, quadro, backend, tid, time.time(), approval))
        finally:
            backend.approved_cli = False
    except workspace.GitErro as e:
        print(str(e))
        return 1
    finally:
        quadro.c.close()


def instalar(origem: Path, destino: Path, exigir_root: bool = True) -> int:
    """Copies the HEAD commit (branch main, clean tree) of `origem` to `destino`, root-owned and not writable by
    group/others. This is how a self-edited orchestrator changes what runs: review, merge into main, then install.
    Nothing is restarted here."""
    import shutil
    import subprocess
    import tarfile
    import tempfile
    if exigir_root and compat.euid() != 0:
        print("install: root only (and after review)"); return 1
    from .gitseguro import git_como_dono

    def g(*a):  # root never honours the checkout's own config: it reads it as the checkout's owner, hardened
        return git_como_dono(origem, list(a))
    if g("rev-parse", "--is-inside-work-tree").returncode != 0:
        print(f"install: {origem} is not a git repository"); return 1
    if g("branch", "--show-current").stdout.strip() != "main":
        print("install: the source must be on branch main (agent/* branches are never installed)"); return 1
    if g("status", "--porcelain", "--untracked-files=no").stdout.strip():
        print("install: the source has uncommitted changes; commit (or discard) them first"); return 1
    commit = g("rev-parse", "HEAD").stdout.strip()
    with tempfile.TemporaryDirectory(prefix="install-") as tmp:
        tar = Path(tmp) / "src.tar"
        with open(tar, "wb") as f:
            presentes = set(g("ls-tree", "--name-only", "HEAD").stdout.split())
            pastas = [n for n in ("src", "rules", "templates", "examples", "README.md", "pyproject.toml", "LICENSE")
                      if n in presentes]
            r = git_como_dono(origem, ["archive", "--format=tar", "HEAD", *pastas], timeout=300, stdout=f,
                              stderr=subprocess.PIPE, text=False)
        if r.returncode != 0:
            print("install: git archive failed: " + r.stderr.decode()[-200:]); return 1
        novo = destino.with_name(destino.name + ".new")
        shutil.rmtree(novo, ignore_errors=True)
        novo.mkdir(parents=True)
        with tarfile.open(tar) as t:
            t.extractall(novo, filter="data")
        (novo / "VERSION").write_text(f"{commit}\n{time.strftime('%Y-%m-%dT%H:%M:%S%z')}\n")
        if exigir_root:
            for raiz, dirs, arqs in os.walk(novo):
                for n in [raiz] + [os.path.join(raiz, x) for x in dirs + arqs]:
                    os.chown(n, 0, 0)
        for raiz, dirs, arqs in os.walk(novo):
            for n in dirs + arqs:
                p = os.path.join(raiz, n)
                os.chmod(p, 0o755 if os.path.isdir(p) else 0o644)
        os.chmod(novo, 0o755)
        if exigir_root and (codigo := config.codigo_inseguro(novo)):
            print("install: unsafe permissions: " + ", ".join(codigo)); shutil.rmtree(novo, ignore_errors=True); return 1
        velho = destino.with_name(destino.name + ".old")
        shutil.rmtree(velho, ignore_errors=True)
        if destino.exists():
            destino.rename(velho)
        novo.rename(destino)
        shutil.rmtree(velho, ignore_errors=True)
    print(f"installed {commit[:10]} in {destino}. Restart the services when no task is running "
          f"(`{CLI} status` shows tarefa_atual: null).")
    return 0


def verificar_regras(arquivo: Path | None = None, *, hoje: date | None = None, raiz: Path | None = None,
                     templates: Path | None = None) -> int:
    """Rules linter: size, review date, contradictions and cited paths. An expired review date only warns (exit 0)."""
    cfg = None
    if arquivo is None:
        cfg = _cfg()
        arquivo = cfg.rules_file
    try:
        texto = Path(arquivo).read_text(encoding="utf-8")
    except OSError:
        print(f"check-rules: cannot read {arquivo}")
        return 1
    hoje = hoje if hoje is not None else date.today()
    aviso = regras.aviso_regras(texto, hoje)
    if aviso:
        print(f"WARNING: {aviso}.", flush=True)
    erros = regras.problemas_regras(texto, raiz or Path(arquivo).parent, hoje)
    pasta_t = templates if templates is not None else (cfg.templates_dir if cfg else None)
    if pasta_t and Path(pasta_t).is_dir():
        for t in sorted(Path(pasta_t).glob("AGENTS.*.md")):
            erros += regras.problemas_template(t)
    for e in erros:
        print(f"ERROR: {e}")
    print(f"check-rules: {arquivo}: {'ok' if not erros else f'{len(erros)} problem(s)'}")
    return 1 if erros else 0


def _sub(sp, nome: str, *aliases: str, **kw):
    return sp.add_parser(nome, aliases=list(aliases), **kw)


# subcommand aliases (Portuguese names) -> canonical English name
CANONICO = {"servir": "serve", "verificar-regras": "check-rules", "instalar": "install", "espelhar": "mirror",
            "claude-limite": "claude-limit", "claude-assumiu": "claude-resumed", "pausar": "pause",
            "codex-retomar": "resume", "armar": "arm", "desarmar": "disarm", "historico": "history",
            "briefing-atualizar": "briefing-check", "recado": "note", "delegar": "delegate", "listar": "list",
            "ver": "show", "resultado": "result", "cancelar": "cancel", "esperar": "wait", "revisar": "review",
            "publicar": "publish", "reivindicar": "claim"}


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog=CLI, description="Orchestrate Claude Code and OpenAI Codex CLI as a relay team.")
    ap.add_argument("--config", type=Path, help="explicit user config file (also used by logon tasks)")
    ap.add_argument("--version", action="version", version=__version__)
    sp = ap.add_subparsers(dest="cmd", required=True, metavar="COMMAND")
    s = _sub(sp, "serve", "servir", help="run the service (worker, chat channel, local socket)")
    s.add_argument("--dry-run", action="store_true", help="fake Codex/root daemon, no chat channel")
    s.add_argument("--no-chat", "--sem-telegram", dest="sem_canal", action="store_true", help="run without a chat channel")
    _sub(sp, "stop", help="gracefully stop the lite user service (including a logon task)")
    _sub(sp, "root-daemon", help="run the optional privileged daemon (root only)")
    setup = _sub(sp, "setup", help="interactive wizard, or an isolated non-interactive evaluation config")
    setup.add_argument("--non-interactive", action="store_true", help="no prompts, no secrets, no publishing")
    setup.add_argument("--output-dir", type=Path, help="explicit new/empty directory for non-interactive setup")
    setup.add_argument("--repo-source", type=Path, help="local-only repository for non-interactive evaluation")
    service = _sub(sp, "service", help="manage the Windows user logon task")
    service.add_argument("action", choices=["install", "uninstall"])
    service.add_argument("--windows", action="store_true", required=True)
    d = _sub(sp, "doctor", help="check the installation and print fix instructions")
    d.add_argument("--json", action="store_true", help="machine-readable output")
    d.add_argument("--fix", action="store_true", help="create missing state directories and example rules; never restart services")
    vr = _sub(sp, "check-rules", "verificar-regras", help="lint the base rules file")
    vr.add_argument("--file", help="rules file (default: paths.rules_file)")
    ins = _sub(sp, "install", "instalar", help="install a root-owned copy from a git checkout (self-editing setups)")
    ins.add_argument("--source", "--origem", dest="origem", default=str(config.DEV_TREE))
    ins.add_argument("--dest", "--destino", dest="destino", default=str(config.INSTALADO))
    e = _sub(sp, "mirror", "espelhar", help="refresh the mirror now (transcript tail, memory, repos)")
    e.add_argument("--no-repos", "--sem-repos", dest="sem_repos", action="store_true")
    _sub(sp, "claude-limit", "claude-limite", help="hook glue for StopFailure(rate_limit): forwards the payload")
    _sub(sp, "claude-usage", help="record Claude transcript token counters (Stop hook; always exits 0)")
    _sub(sp, "claude-resumed", "claude-assumiu", help="Claude is back: marks the baton and notes as read")
    st = _sub(sp, "status", help="service status")
    st.add_argument("--brief", action="store_true", help="short queue and resume schedule")
    _sub(sp, "pause", "pausar", help="pause the Codex queue")
    _sub(sp, "resume", "codex-retomar", help="resume the Codex queue")
    _sub(sp, "arm", "armar", help="enable automatic handoff on Claude's usage limit (sends context to OpenAI)")
    _sub(sp, "disarm", "desarmar", help="disable automatic handoff")
    hs = _sub(sp, "history", "historico", help="chat channel history")
    hs.add_argument("--last", "--ultimas", dest="ultimas", type=int, default=50)
    hs.add_argument("--since", "--desde", dest="desde", help="YYYY-MM-DD")
    hs.add_argument("--search", "--busca", dest="busca")
    _sub(sp, "briefing-status", help="age and size of the living briefing")
    _sub(sp, "briefing-check", "briefing-atualizar", help="check the briefing length")
    _sub(sp, "note", "recado", help="print the unread note from Codex")
    h = _sub(sp, "hook-prompt", help="hook glue for UserPromptSubmit/SessionStart")
    h.add_argument("--event", "--evento", dest="evento", default="UserPromptSubmit")
    dl = _sub(sp, "delegate", "delegar", help="queue a task for Codex")
    dl.add_argument("--repo", required=True)
    dl.add_argument("--title", "--titulo", dest="titulo", required=True)
    dl.add_argument("--description", "--descricao", dest="descricao", default="")
    dl.add_argument("--files", "--arquivos", dest="arquivos", default="", help="comma-separated paths/globs the task touches")
    dl.add_argument("--briefing", help="markdown file with the task briefing")
    dl.add_argument("--briefing-text", "--briefing-texto", dest="briefing_texto")
    dl.add_argument("--priority", dest="prioridade", type=int, default=0, help="higher values run first (default 0)")
    dl.add_argument("--model", "--modelo", dest="modelo", help="Codex model for this task only (default: the service setting)")
    dl.add_argument("--effort", "--esforco", dest="esforco", choices=("low", "medium", "high", "xhigh", "max", "ultra"),
                    help="reasoning effort for this task only")
    dl.add_argument("--wait", action="store_true", help="wait for the task and print `result --short` (exit 0 = review/completed)")
    dl.add_argument("--timeout", type=int, default=0, help="with --wait: seconds to wait (default: until it finishes)")
    dl.add_argument("--motor", default="codex", help=argparse.SUPPRESS)
    ls = _sub(sp, "list", "listar", help="list board tasks")
    ls.add_argument("--state", "--estado", dest="estado")
    ls.add_argument("--owner", "--dono", dest="dono")
    for n, pt, ajuda in (("show", "ver", "show a task"), ("result", "resultado", "show a task result"),
                         ("cancel", "cancelar", "cancel a task")):
        x = _sub(sp, n, pt, help=ajuda)
        x.add_argument("id", type=int)
        if n in ("show", "result"):
            modos = x.add_mutually_exclusive_group()
            modos.add_argument("--short", action="store_true", help="status, branch, diffstat, warnings and up to 10 report lines")
            modos.add_argument("--full", action="store_true", help="complete task record")
    w = _sub(sp, "wait", "esperar", help="wait for a task to reach a final state (exit 0 = review/completed)")
    w.add_argument("id", type=int)
    w.add_argument("--timeout", type=int, default=0)
    wt = _sub(sp, "watch", help="live view of what Codex is doing on a task (read-only)")
    wt.add_argument("id", type=int, nargs="?", help="task id (default: the running task)")
    wt.add_argument("--no-follow", action="store_true", help="print what happened so far and exit")
    wo = _sub(sp, "watch-open", help="open `watch` in a tmux split or a new terminal window (else print the command)")
    wo.add_argument("id", type=int, nargs="?")
    rv = _sub(sp, "review", "revisar", help="cross-review of a branch by Codex (read-only)")
    rv.add_argument("--repo", required=True)
    rv.add_argument("--branch", required=True)
    rv.add_argument("--title", "--titulo", dest="titulo")
    pb = _sub(sp, "publish", "publicar", help="merge + push a reviewed task (interactive approval; lite: ID and full SHA)")
    pb.add_argument("id", type=int)
    pb.add_argument("--approved-by-owner", "--autorizado-pelo-dono", dest="autorizado", action="store_true",
                    help="deprecated, ignored: the owner must confirm interactively")
    cl = _sub(sp, "claim", "reivindicar", help="take a task (default owner: claude)")
    cl.add_argument("id", type=int)
    cl.add_argument("--owner", "--dono", dest="dono", default="claude", choices=["claude", "codex"])
    cl.add_argument("--files", "--arquivos", dest="arquivos", default="")
    return ap


def main(argv=None) -> int:
    if compat.WINDOWS:
        for stream in (sys.stdin, sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace")
    a = parser().parse_args(argv)
    if a.config:
        os.environ["AAO_CONFIG"] = str(a.config.resolve())
    cmd = CANONICO.get(a.cmd, a.cmd)
    if cmd == "install":
        return instalar(Path(a.origem), Path(a.destino))
    if cmd == "check-rules":
        return verificar_regras(Path(a.file) if a.file else None)
    if cmd == "setup":
        from . import assistente
        if a.non_interactive:
            if a.output_dir is None:
                parser().error("--output-dir is required with --non-interactive")
            return assistente.nao_interativo(a.output_dir, a.repo_source)
        if a.output_dir is not None or a.repo_source is not None:
            parser().error("--output-dir and --repo-source require --non-interactive")
        return assistente.main()
    try:
        cfg = _cfg()
    except ValueError as error:
        print(f"Configuration error: {error}", file=sys.stderr)
        return 2
    if cmd == "service":
        from . import windows_service
        return windows_service.manage(cfg, a.action, config.default_config_file())
    if cmd == "doctor":
        from . import doctor
        return doctor.main(cfg, como_json=a.json, fix=a.fix)
    if cmd == "watch":
        from . import watch
        return watch.main(cfg, a.id, seguir=not a.no_follow, chamar=chamar)
    if cmd == "watch-open":
        from . import watch
        como, linha = watch.abrir(a.id)
        if como == "manual":
            print(f"No tmux session or graphical terminal found. Run this in another terminal:\n  {linha}")
        else:
            print(f"Opened the live Codex view ({como}): {linha}")
        return 0
    if cmd == "serve":
        if cfg.mode == "lite":
            from .lite import serve
            return serve(cfg, a.dry_run)
        # the config (token) is already in memory; keep the HMAC key in memory, mark the process non-dumpable and
        # drop both secrets from the environment so same-uid engines cannot read /proc/<pid>/environ
        from . import seguranca
        chave = os.environ.get("AAO_PUBLISH_KEY", "")
        chave = seguranca.proteger_processo().get("AAO_PUBLISH_KEY", chave)
        svc = montar_servico(cfg, a.dry_run, a.sem_canal, chave=chave)
        svc.rodar()
        return 0
    if cmd == "root-daemon":
        if cfg.mode == "lite":
            print("lite mode does not use a root daemon")
            return 2
        root_daemon.servir(cfg)
        return 0
    if cmd == "mirror":
        if cfg.mode == "lite":
            from .lite import LocalBackend
            backend = LocalBackend(cfg)
            return _imprimir(backend._mirror() if a.sem_repos else backend.espelhar())
        return _imprimir(espelho.espelhar(cfg, com_repos=not a.sem_repos))
    if cmd == "claude-limit":  # minimal glue for the StopFailure hook: only forwards the payload
        payload = None
        if not sys.stdin.isatty():
            try:
                payload = json.loads(sys.stdin.read() or "null")
            except ValueError:
                payload = None
        chamar(cfg, {"cmd": "claude_limite", "payload": payload}, timeout=5)
        return 0  # the hook must never fail nor slow Claude down
    if cmd == "claude-usage":
        # Whitelist only the session id; never forward or print the hook's conversation payload.
        try:
            payload = json.loads(sys.stdin.read(65536) or "{}") if not sys.stdin.isatty() else {}
            sessao = payload.get("session_id") if isinstance(payload, dict) else None
            chamar(cfg, {"cmd": "claude_usage", "session_id": sessao}, timeout=4)
        except (OSError, ValueError, TypeError):
            pass
        return 0
    if cmd == "history":
        try:
            regs = historico.consultar(cfg.logs / "historico.jsonl", a.ultimas, a.desde, a.busca)
        except ValueError:
            print("--since must be YYYY-MM-DD"); return 2
        except PermissionError:
            print("permission denied: run as root or as the service user"); return 1
        for r in regs:
            print(historico.formatar(r))
        return 0
    if cmd == "hook-prompt":
        return _hook_prompt(cfg, a.evento)
    if cmd in ("arm", "disarm"):
        if cmd == "arm":
            cfg.armado_flag.parent.mkdir(parents=True, exist_ok=True)
            cfg.armado_flag.touch()
            if cfg.mode == "full":
                import grp, pwd
                try:
                    os.chown(cfg.armado_flag, pwd.getpwnam(cfg.service_user).pw_uid, grp.getgrnam(cfg.service_user).gr_gid)
                except (KeyError, PermissionError):
                    pass
        else:
            cfg.armado_flag.unlink(missing_ok=True)
        print("armed" if cfg.armado_flag.exists() else "disarmed")
        return 0
    if cmd == "briefing-check":
        p = cfg.para_codex / "BRIEFING.md"
        if not p.exists():
            print("BRIEFING.md does not exist"); return 1
        linhas = p.read_text(encoding="utf-8").splitlines()
        print(f"ok: {len(linhas)} lines" + (" (more than 40: trim it)" if len(linhas) > 40 else ""))
        return 0
    if cmd == "publish":
        return _publicar(cfg, a.id, a.autorizado)
    if cmd == "wait":
        r = chamar(cfg, {"cmd": "esperar", "id": a.id, "timeout": max(0, a.timeout)},
                   timeout=a.timeout + 5 if a.timeout > 0 else None)
        if not r.get("ok"):
            print(r.get("erro")); return 2
        t = r["tarefa"]
        print(json.dumps({"id": t["id"], "estado": t["estado"], "branch": t["branch"], "patch": t["patch"],
                          "resumo": (t["resumo"] or "")[:800]}, ensure_ascii=False))
        return 0 if t["estado"] in ("em_revisao", "concluida") else 1
    req = {"cmd": {"resume": "retomar", "claude-resumed": "claude_assumiu", "briefing-status": "briefing_status",
                   "pause": "pausar", "note": "recado", "delegate": "delegar", "list": "listar", "show": "ver",
                   "result": "resultado", "cancel": "cancelar", "review": "revisar", "claim": "reivindicar"}.get(cmd, cmd)}
    if cmd == "delegate":
        briefing = a.briefing_texto
        if a.briefing:
            briefing = Path(a.briefing).read_text(encoding="utf-8")
        req.update(repo=a.repo, titulo=a.titulo, descricao=a.descricao, briefing=briefing,
                   arquivos=[x for x in a.arquivos.split(",") if x], prioridade=a.prioridade,
                   modelo=a.modelo, esforco=a.esforco)
    elif cmd == "list":
        estado = FILTROS_ESTADO.get(a.estado, a.estado)
        req.update(estados=[estado] if estado else None, dono=a.dono)
    elif cmd in ("show", "result", "cancel"):
        req["id"] = a.id
        if cmd in ("show", "result"):
            req.update(short=a.short, full=a.full)
    elif cmd == "review":
        req.update(repo=a.repo, branch=a.branch, titulo=a.titulo)
    elif cmd == "claim":
        req.update(id=a.id, dono=a.dono, arquivos=[x for x in a.arquivos.split(",") if x])
    r = chamar(cfg, req)
    if cmd == "delegate" and a.wait and r.get("ok"):
        print(f"queued task #{r['id']}; waiting...", file=sys.stderr)
        w = chamar(cfg, {"cmd": "esperar", "id": r["id"], "timeout": max(0, a.timeout)},
                   timeout=a.timeout + 5 if a.timeout > 0 else None)
        if not w.get("ok"):
            print(w.get("erro")); return 2
        t = w["tarefa"]
        print(f"Status: {t['estado']}\nBranch: {t.get('branch') or '-'}")
        print(f"Diffstat: {t.get('diffstat') or '-'}")
        print(f"Warnings: {t.get('aviso') or '-'}; error: {t.get('erro') or '-'}")
        print("\n".join((t.get("resumo") or "(no report)").splitlines()[:10]))
        return 0 if t["estado"] in ("em_revisao", "concluida") else 1
    if cmd == "list" and r.get("ok"):
        r = {**r, "tarefas": [{**t, "estado": ESTADOS_CLI.get(t["estado"], t["estado"])}
                              for t in r.get("tarefas", [])]}
    if cmd == "status" and a.brief and r.get("ok"):
        from datetime import datetime
        agenda = r.get("retomada_agendada") or {}
        def hora(epoch):
            return datetime.fromtimestamp(epoch, config.fuso()).strftime("%H:%M") if epoch else "-"
        from .medicao import linha_janela
        uso = r.get("uso", {})
        for nome in ("5h", "7d"):
            print(linha_janela(nome, uso.get("janelas", {}).get(nome, {})))
        print(f"fila: {r.get('fila', 0)}; pausado: {r.get('pausado', False)}; tarefa: {r.get('tarefa_atual') or '-'}")
        print(f"retomada agendada: Claude {hora(agenda.get('claude'))} / Codex {hora(agenda.get('codex'))}")
        print(f"balance: {uso.get('modo', 'unknown')}; tasks_per_hour: {uso.get('tasks_per_hour', '?')}")
        return 0
    if cmd in ("result", "show") and (a.short or (cmd == "result" and not a.full)) and r.get("ok"):
        t = r["tarefa"]
        print(f"Status: {t['estado']}\nBranch: {t.get('branch') or '-'}")
        print(f"Diffstat: {t.get('diffstat') or '-'}")
        print(f"Warnings: {t.get('aviso') or '-'}; error: {t.get('erro') or '-'}")
        print(t.get("resumo") or "(no report)")
        return 0
    if cmd == "note":
        print(r.get("texto", ""))
        return 0
    return _imprimir(r)


if __name__ == "__main__":
    sys.exit(main())
