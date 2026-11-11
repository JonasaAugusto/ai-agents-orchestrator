"""HTML card for /usage: plan windows with a bar, percentage and reset time (configured timezone).

Rules: only escapes and formats; missing data omits the line (never "None"/JSON); traffic light green/yellow/orange.
"""
from __future__ import annotations

import html
from datetime import datetime
from . import config

DIAS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
BLOCOS = 10


def esc(v) -> str:
    return html.escape(str(v), quote=False)


def semaforo(pct: float) -> str:
    return "🟢" if pct <= 60 else ("🟡" if pct <= 85 else "🟠")


def barra(pct: float, n: int = BLOCOS) -> str:
    cheios = min(n, max(0, round(pct / 100 * n)))
    if pct > 0 and cheios == 0:
        cheios = 1
    return "▰" * cheios + "▱" * (n - cheios)


def duracao(seg: float) -> str:
    m = max(0, int(seg // 60))
    if m >= 1440:
        d, resto = divmod(m, 1440)
        return f"{d} d {resto // 60} h" if resto // 60 else f"{d} d"
    if m >= 60:
        return f"{m // 60} h {m % 60:02d} min" if m % 60 else f"{m // 60} h"
    return f"{max(m, 1)} min"


def nome_janela(minutos) -> str:
    try:
        m = int(minutos)
    except (TypeError, ValueError):
        return "Usage window"
    if m == 10080:
        return "Weekly window"
    if m == 1440:
        return "Daily window"
    if m % 1440 == 0:
        return f"{m // 1440}-day window"
    if m % 60 == 0:
        h = m // 60
        return f"{h}-hour window"
    return f"{m}-min window"


def quando(reset: float, agora: float) -> str:
    tz = config.fuso()
    dt = datetime.fromtimestamp(reset, tz)
    if dt.date() == datetime.fromtimestamp(agora, tz).date():
        return f"at {dt:%H:%M}"
    return f"{DIAS[dt.weekday()]}, {dt:%Y-%m-%d} at {dt:%H:%M}"


def _janela(j: dict, agora: float) -> list[str]:
    linhas = [f"<b>⏱ {esc(nome_janela(j.get('duracao_min')))}</b>"]
    pct = j.get("usado_pct")
    if isinstance(pct, (int, float)):
        pct = min(100.0, max(0.0, float(pct)))
        linhas.append(f"{semaforo(pct)} {barra(pct)} <b>{round(pct)}%</b> used · {100 - round(pct)}% left")
    reset = j.get("reseta_em")
    if isinstance(reset, (int, float)) and reset > 0:
        falta = reset - agora
        linhas.append(f"Resets in {duracao(falta)} ({quando(reset, agora)})" if falta > 0 else "Resetting now")
    return linhas


def cartao_usage(u: dict, agora: float, bot: dict | None = None) -> str:
    """u = result of engines.uso_codex (ok=True). bot = optional local values (None/missing = omitted)."""
    blocos: list[list[str]] = [["<b>📊 Codex usage</b>"]]
    plano = u.get("plano")
    if plano:
        blocos[0].append(f"Plan: <b>{esc(str(plano).capitalize())}</b>")
    for nome in ("primaria", "secundaria"):
        j = u.get(nome)
        if j and (j.get("usado_pct") is not None or j.get("reseta_em")):
            blocos.append(_janela(j, agora))
    if u.get("limite_atingido"):
        blocos.append(["🟠 Limit reached: " + esc(u["limite_atingido"])])
    if len(blocos) == 1 and not plano:
        blocos.append(["Codex reported no usage windows right now."])
    b = bot_bloco(bot or {})
    if b:
        blocos.append(b)
    return "\n\n".join("\n".join(x) for x in blocos)


def bot_bloco(bot: dict) -> list[str]:
    linhas: list[str] = []
    me = [f"{r}: {esc(bot[k])}" for r, k in (("Model", "modelo"), ("Effort", "esforco")) if bot.get(k)]
    if me:
        linhas.append(" · ".join(me))
    if bot.get("acordadas") is not None and bot.get("teto"):
        linhas.append(f"Limit checks today: {esc(bot['acordadas'])}/{esc(bot['teto'])}")
    if bot.get("tarefas_hora") is not None and bot.get("teto_tarefas"):
        linhas.append(f"Tasks in the last hour: {esc(bot['tarefas_hora'])}/{esc(bot['teto_tarefas'])}")
    return ["<b>🤖 Bot</b>"] + linhas if linhas else []


def cartao_erro(erro: str | None) -> str:
    motivo = {"no answer": "Codex did not answer in time."}.get(erro or "", "Codex did not return the plan data.")
    return f"<b>📊 Codex usage</b>\n\n⚠️ I could not read the usage right now. {motivo}\nWithout reliable data I do not estimate."
