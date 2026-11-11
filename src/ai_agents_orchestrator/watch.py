"""`aao watch [id]`: read-only live view of what Codex is doing on a task.

The service appends every Codex JSONL event of a task, already masked, to <state_dir>/resultados/<id>/events.jsonl.
This command prints a header (task, workspace, branch), replays the events so far and, unless --no-follow, keeps
following the file until the task reaches a final state. It ends with a tail of the diff (git diff --stat). It only
reads files: watching never interferes with the task.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import time
from pathlib import Path

FINAIS = ("em_revisao", "concluida", "bloqueada", "cancelada")
ESTADOS_EN = {"pendente": "pending", "em_andamento": "in progress", "em_revisao": "in review", "concluida": "completed",
              "bloqueada": "blocked", "cancelada": "cancelled"}


def _corta(texto: str, n: int = 300) -> str:
    texto = " ".join(str(texto or "").split())
    return texto if len(texto) <= n else texto[:n] + "..."


def formatar_evento(reg: dict) -> str | None:
    """One readable line per relevant event; None for noise."""
    ev = reg.get("ev") if isinstance(reg.get("ev"), dict) else reg
    tipo = ev.get("type", "")
    hora = time.strftime("%H:%M:%S", time.localtime(reg.get("t") or 0)) if reg.get("t") else "--:--:--"
    if tipo == "thread.started":
        return f"[{hora}] session started"
    if tipo in ("error", "turn.failed"):
        msg = ev.get("message") or (ev.get("error") or {}).get("message") or ""
        return f"[{hora}] ERROR {_corta(msg)}"
    if tipo == "turn.completed":
        u = ev.get("usage") or {}
        extra = f" (tokens in={u.get('input_tokens')}, out={u.get('output_tokens')})" if u else ""
        return f"[{hora}] turn completed{extra}"
    if tipo.startswith("item."):
        it = ev.get("item") or {}
        k = it.get("type") or it.get("item_type") or ""
        fase = tipo.split(".", 1)[1]
        if k == "command_execution":
            if fase == "started":
                return f"[{hora}] $ {_corta(it.get('command'), 200)}"
            if fase == "completed":
                code = it.get("exit_code")
                saida = _corta(it.get("aggregated_output") or "", 160)
                return f"[{hora}]   -> exit {code}" + (f": {saida}" if saida else "")
            return None
        if k == "file_change" and fase == "completed":
            arqs = [f"{c.get('kind', '?')} {c.get('path', '?')}" for c in it.get("changes") or []]
            return f"[{hora}] edit: " + (", ".join(arqs) or "(files)")
        if k in ("agent_message", "assistant_message") and fase == "completed":
            return f"[{hora}] codex: {_corta(it.get('text'), 500)}"
        if k == "reasoning" and fase == "completed":
            return f"[{hora}] thinking: {_corta(it.get('text'), 200)}"
        if k == "todo_list" and fase in ("started", "updated", "completed"):
            itens = [("[x] " if i.get("completed") else "[ ] ") + _corta(i.get("text"), 60) for i in it.get("items") or []]
            return f"[{hora}] plan: " + "; ".join(itens)
        if k == "error":
            return f"[{hora}] ERROR {_corta(it.get('message'))}"
    return None


def tarefa(cfg, tid: int) -> dict | None:
    try:
        con = sqlite3.connect(f"file:{cfg.db_quadro}?mode=ro", uri=True, timeout=2)
        con.row_factory = sqlite3.Row
        r = con.execute("SELECT * FROM tarefas WHERE id=?", (tid,)).fetchone()
        con.close()
        return dict(r) if r else None
    except sqlite3.Error:
        return None


def tarefa_padrao(cfg, chamar=None) -> int | None:
    """The running task (asks the service); otherwise the most recent task that has an event log."""
    if chamar:
        r = chamar(cfg, {"cmd": "status"}, timeout=3)
        if r.get("ok") and r.get("tarefa_atual"):
            return int(r["tarefa_atual"])
    logs = sorted(cfg.resultados.glob("*/events.jsonl"), key=lambda p: p.stat().st_mtime) if cfg.resultados.exists() else []
    for p in reversed(logs):
        if p.parent.name.isdigit():
            return int(p.parent.name)
    return None


def diff_stat(ws: str | None) -> str:
    if not ws or not Path(ws).is_dir():
        return ""
    try:
        r = subprocess.run(["git", "-c", "safe.directory=*", "-C", ws, "diff", "--stat", "HEAD"], capture_output=True,
                           text=True, timeout=20)
        c = subprocess.run(["git", "-c", "safe.directory=*", "-C", ws, "log", "--oneline", "-3"], capture_output=True,
                           text=True, timeout=20)
        return (r.stdout.strip() or "(no uncommitted changes)") + ("\nlatest commits:\n" + c.stdout.strip() if c.stdout.strip() else "")
    except (OSError, subprocess.TimeoutExpired):
        return ""


def main(cfg, tid: int | None = None, seguir: bool = True, chamar=None, saida=print, dormir=time.sleep,
         max_ciclos: int | None = None, intervalo: float = 1.0) -> int:
    tid = tid if tid is not None else tarefa_padrao(cfg, chamar)
    if tid is None:
        saida("watch: no running task and no task with an event log yet")
        return 1
    t = tarefa(cfg, tid) or {}
    saida(f"== Codex on task #{tid}: {t.get('titulo', '?')} [{ESTADOS_EN.get(t.get('estado'), t.get('estado', '?'))}]")
    saida(f"   repo: {t.get('repo') or '-'}  branch: {t.get('branch') or '-'}")
    saida(f"   workspace: {t.get('workspace') or '-'}")
    log = cfg.resultados / str(tid) / "events.jsonl"
    pos, ciclos = 0, 0
    while True:
        if log.exists():
            with open(log, "rb") as f:
                f.seek(pos)
                for linha in f:
                    if not linha.endswith(b"\n"):
                        break  # partial line: read it on the next pass
                    pos += len(linha)
                    try:
                        txt = formatar_evento(json.loads(linha.decode("utf-8", "replace")))
                    except ValueError:
                        continue
                    if txt:
                        saida(txt)
        t = tarefa(cfg, tid) or t
        ciclos += 1
        if not seguir or t.get("estado") in FINAIS or (max_ciclos is not None and ciclos >= max_ciclos):
            break
        dormir(intervalo)
    estado = ESTADOS_EN.get(t.get("estado"), t.get("estado", "?"))
    ds = diff_stat(t.get("workspace"))
    if ds:
        saida("== diff so far:\n" + ds)
    saida(f"== task #{tid} is {estado}")
    return 0


# ---------------------------------------------------------------- open the live view in another pane/window
TERMINAIS = (("x-terminal-emulator", ["-e"]), ("gnome-terminal", ["--"]), ("konsole", ["-e"]), ("xfce4-terminal", ["-x"]),
             ("kitty", []), ("alacritty", ["-e"]), ("wezterm", ["start", "--"]), ("xterm", ["-e"]))


def comando_watch(tid: int | None, prog: str = "aao") -> list[str]:
    return [prog, "watch"] + ([str(tid)] if tid is not None else [])


def abrir(tid: int | None, env: dict | None = None, which=None, rodar=None, prog: str = "aao") -> tuple[str, str]:
    """Opens `aao watch` beside the current session: a tmux split when inside tmux; else a new terminal window when a
    graphical session and a known terminal emulator exist; else returns the command to run by hand.
    Returns (how, command line)."""
    import os
    import shlex
    import shutil
    env = dict(os.environ) if env is None else env
    which = which or shutil.which
    rodar = rodar or (lambda argv: subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                                     stderr=subprocess.DEVNULL, start_new_session=True))
    cmd = comando_watch(tid, prog)
    linha = shlex.join(cmd)
    if env.get("TMUX") and which("tmux"):
        rodar(["tmux", "split-window", "-h", "-l", "45%", linha])
        return "tmux", linha
    if env.get("DISPLAY") or env.get("WAYLAND_DISPLAY"):
        for nome, prefixo in TERMINAIS:
            if which(nome):
                rodar([nome, *prefixo, *cmd])
                return nome, linha
    return "manual", linha
