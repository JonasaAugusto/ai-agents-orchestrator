"""Counter-only accounting and deterministic balance policy. No model calls or conversation storage."""
from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime

JANELAS = {"5h": 5 * 3600, "7d": 7 * 86400}
ECONOMICO = "modo economico: delegue revisao/testes ao Codex, use result --short, nao leia diffs"


def numero(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def contador(v) -> int:
    return v if type(v) is int and 0 <= v <= 10**12 else 0


def contadores(usage, *, claude=False) -> tuple[int, int]:
    u = usage if isinstance(usage, dict) else {}
    entrada = contador(u.get("input_tokens"))
    if claude:
        entrada += contador(u.get("cache_read_input_tokens")) + contador(u.get("cache_creation_input_tokens"))
    # Codex cached_input_tokens is already included in input_tokens.
    return entrada, contador(u.get("output_tokens"))


def registro_uso(d: dict, sessao: str) -> dict | None:
    """One API message, even when the transcript repeats it for several streamed blocks."""
    msg = d.get("message")
    if d.get("type") != "assistant" or not isinstance(msg, dict):
        return None
    ident = msg.get("id") or d.get("uuid")
    if not isinstance(ident, str) or not ident or not isinstance(msg.get("usage"), dict):
        return None
    entrada, saida = contadores(msg["usage"], claude=True)
    return {"papel": "uso", "t": d.get("timestamp"),
            "id": hashlib.sha256((sessao + ":" + ident).encode()).hexdigest(),
            "tokens_in": entrada, "tokens_out": saida}


def importar_cauda(estado, caminho, agora: float) -> int:
    """Idempotent upserts; persists hashes, timestamps and counters, never transcript text."""
    try:
        with caminho.open("rb") as f:
            tamanho = f.seek(0, 2)
            f.seek(max(0, tamanho - 12 * 1024 * 1024))
            linhas = f.read().splitlines()
        if tamanho > 12 * 1024 * 1024:
            linhas = linhas[1:]
    except OSError:
        return 0
    registros = []
    for linha in linhas:
        try:
            registros.append(json.loads(linha))
        except ValueError:
            continue
    return importar_registros(estado, registros, agora)


def importar_registros(estado, registros: list, agora: float) -> int:
    """The root daemon returns these same mirrored counters while holding the mirror lock."""
    ids = set()
    for r in registros:
        try:
            if not isinstance(r, dict) or r.get("papel") != "uso" or not re.fullmatch(r"[a-f0-9]{64}", str(r.get("id", ""))):
                continue
            ts = datetime.fromisoformat(r["t"].replace("Z", "+00:00"))
            if ts.tzinfo is None or not 0 < ts.timestamp() <= agora:
                continue
            entrada, saida = contador(r.get("tokens_in")), contador(r.get("tokens_out"))
            estado.registrar_execucao("turno", "claude", 0, True, agora=ts.timestamp(),
                                      tokens_in=entrada, tokens_out=saida, usage_key="claude:" + r["id"])
            ids.add(r["id"])
        except (ValueError, TypeError, KeyError, OverflowError):
            continue
    return len(ids)


def problemas_config(cfg) -> list[str]:
    problemas = []
    if not (numero(cfg.balance_ratio_low) and numero(cfg.balance_ratio_high)
            and 0 < cfg.balance_ratio_low < cfg.balance_ratio_high):
        problemas.append("balance: require 0 < ratio_low < ratio_high")
    if not (numero(cfg.balance_alert_low) and numero(cfg.balance_alert_high)
            and 0 <= cfg.balance_alert_low < cfg.balance_alert_high <= 100):
        problemas.append("balance: require 0 <= alert_other_below_pct < alert_high_pct <= 100")
    if not numero(cfg.balance_tasks_factor) or not 0 < cfg.balance_tasks_factor <= 1:
        problemas.append("balance.low_ratio_tasks_factor must be in (0,1]")
    if not (0 < cfg.balance_refresh_seconds <= cfg.balance_max_age_seconds):
        problemas.append("balance: require 0 < refresh_seconds <= max_age_seconds")
    for motor in ("claude", "codex"):
        for janela in JANELAS:
            v = getattr(cfg, f"{motor}_tokens_{janela}")
            if type(v) is not int or not 0 <= v <= 10**12:
                problemas.append(f"balance.{motor}_tokens_{janela} must be a nonnegative token quota")
    return problemas


def calcular(cfg, estado, agora: float) -> dict:
    erros = problemas_config(cfg)
    cache = estado.get("codex_uso_balance", {})
    fresco = (isinstance(cache, dict) and numero(cache.get("quando"))
              and 0 <= agora - cache["quando"] <= cfg.balance_max_age_seconds)
    dados = cache.get("dados", {}) if fresco else {}
    janelas = {}
    for nome, segundos in JANELAS.items():
        totais = estado.tokens_entre(agora - segundos, agora)
        motores = {}
        for motor in ("claude", "codex"):
            tokens = totais.get(motor, {"tokens_in": 0, "tokens_out": 0})
            cota = getattr(cfg, f"{motor}_tokens_{nome}")
            pct = 100 * sum(tokens.values()) / cota if cota > 0 else None
            fonte = "tokens/quota" if pct is not None else "unknown"
            if motor == "codex" and isinstance(dados, dict) and dados.get("ok"):
                for k in ("primaria", "secundaria"):
                    w = dados.get(k)
                    if not isinstance(w, dict) or w.get("duracao_min") != segundos // 60:
                        continue
                    p, reset = w.get("usado_pct"), w.get("reseta_em")
                    if numero(p) and 0 <= p <= 100 and (reset is None or (numero(reset) and reset > agora)):
                        pct, fonte = p, "account/rateLimits/read"
                        break
            motores[motor] = {**tokens, "usado_pct": pct, "fonte": fonte}
        c, x = (motores[m]["usado_pct"] for m in ("claude", "codex"))
        # 0/0 or missing data is unknown. Positive/0 is explicitly above the target (JSON-safe).
        razao = c / x if c is not None and x is not None and x > 0 else None
        acima = c is not None and x == 0 and c > 0
        modo = "unknown" if razao is None and not acima else (
            "economico" if acima or razao > cfg.balance_ratio_high else (
                "claude" if razao < cfg.balance_ratio_low else "equilibrado"))
        janelas[nome] = {**motores, "razao": razao, "codex_zero": acima, "modo": modo}
    modos = [w["modo"] for w in janelas.values()]
    modo = "economico" if "economico" in modos else ("claude" if "claude" in modos else (
        "equilibrado" if "equilibrado" in modos else "unknown"))
    if not cfg.balance_enabled or erros:
        modo = "disabled" if not cfg.balance_enabled else "invalid"
    teto = cfg.tarefas_por_hora
    if modo == "claude" and teto > 0:
        teto = max(1, int(teto * cfg.balance_tasks_factor))
    instrucao = ECONOMICO if modo == "economico" else (
        "Claude may review directly; Codex tasks_per_hour reduced to balance usage." if modo == "claude" else "")
    return {"modo": modo, "janelas": janelas, "tasks_per_hour": teto,
            "instrucao": instrucao, "erros": erros, "codex_cache_fresco": fresco}


def linha_janela(nome: str, w: dict) -> str:
    def pct(m):
        p = w.get(m, {}).get("usado_pct")
        return f"{p:.0f}%" if p is not None else "?"
    razao = "infinita" if w.get("codex_zero") else (f"{w['razao']:.1f}" if w.get("razao") is not None else "?")
    return f"{nome}: Claude {pct('claude')} · Codex {pct('codex')} · razao {razao}"
