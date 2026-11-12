"""MANDATORY masking of everything the backup agent (and therefore the model provider) can read.

Claude Code transcripts and memory files may contain secrets pasted into the chat, personal data and e-mail
content. This module produces the MIRROR: a read-only, masked copy that is the only thing Codex sees. The
originals stay where they are, unreadable by the service user.

Masks are conservative: they prefer masking too much over leaking. There is no "smart" heuristic here; the
patterns are regexes, so a secret in a novel format can still slip through (documented limitation).

Pattern groups:
  - "outros": credentials of other services (private keys, bot tokens, AWS/B2, restic, app passwords) - always;
  - "pii": personal data (e-mails, Brazilian CPF/CNPJ and phone formats, 11-digit numbers) - always;
  - "dev": development secrets (API keys, GitHub/Slack tokens, JWTs, KEY=value) - on by default, configurable.
"""
from __future__ import annotations

import json
import re

OCULTO = "[secret-removed]"

# ---------------------------------------------------------------- padroes
_PADROES: list[tuple[str, str, re.Pattern, str]] = []


def _p(grupo: str, nome: str, regex: str, subst: str, flags: int = 0) -> None:
    _PADROES.append((grupo, nome, re.compile(regex, flags), subst))


# --- other services (always)
_p("outros", "chave-privada", r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)",
   "[private-key-removed]", re.S)
_p("outros", "token-telegram", r"\b\d{8,}:[A-Za-z0-9_-]{30,}", "[telegram-token-removed]")
_p("outros", "aws", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b", "[aws-key-removed]")
_p("outros", "b2", r"\bK00[0-9A-Za-z+/]{28,}", "[b2-key-removed]")
_p("outros", "restic", r"(?i)\b(RESTIC_[A-Z_]*|B2_ACCOUNT_[A-Z]+|AWS_SECRET_ACCESS_KEY)\s*[=:]\s*\S+", r"\1=" + OCULTO)
_p("outros", "senha-app", r"(?i)((?:senha|password|passwd|app[ _-]?pass\w*|gmail|imap|smtp)[^\n]{0,60}?[\s:=\"'(])([a-z]{4}(?: ?[a-z]{4}){3})(?![a-z])",
   r"\1" + OCULTO)
# --- development secrets (group "dev", see MASCARAR_DEV)
_p("dev", "jwt", r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}", "[jwt-removed]")
_p("dev", "bearer", r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{12,}", r"\1 [token-removed]")
_p("dev", "sk", r"\bsk-[A-Za-z0-9_-]{16,}", "[sk-key-removed]")
_p("dev", "github", r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}|\bgithub_pat_[A-Za-z0-9_]{20,}", "[github-token-removed]")
_p("dev", "slack", r"\bxox[abprs]-[A-Za-z0-9-]{10,}", "[slack-token-removed]")
_p("dev", "google-api", r"\bAIza[0-9A-Za-z_-]{30,}", "[google-key-removed]")
_p("dev", "url-credencial", r"(?i)\b([a-z][a-z0-9+.-]*://)[^\s/:@]+:[^\s/@]+@", r"\1[credential-removed]@")
_p("dev", "atrib-env", r"(?i)\b([A-Z0-9_]*(?:KEY|TOKEN|PASSWORD|PASSWD|SECRET|SENHA|CREDENTIAL)[A-Z0-9_]*)\s*[=:]\s*(\"[^\"\n]*\"|'[^'\n]*'|[^\s\"',;]+)",
   r"\1=" + OCULTO)
_p("dev", "atrib-json", r"(?i)(\"[^\"\n]*(?:key|token|password|passwd|secret|senha|credential)[^\"\n]*\"\s*:\s*)\"[^\"\n]*\"",
   r"\1\"" + OCULTO + "\"")
# --- personal data (always)
_p("pii", "cpf", r"(?<!\d)\d{3}\.\d{3}\.\d{3}-\d{2}(?!\d)", "[cpf-removed]")
_p("pii", "cnpj", r"(?<!\d)\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}(?!\d)", "[cnpj-removed]")
_EMAIL = re.compile(r"(?i)\b[A-Z0-9._%+-]+@([A-Z0-9-]+(?:\.[A-Z0-9-]+)+)\b")
DOMINIOS_PERMITIDOS: set[str] = {"localhost", "example.com", "noreply.github.com"}  # see privacy.allowed_email_domains
EMAILS_PERMITIDOS: set[str] = set()  # see privacy.allowed_emails
_p("pii", "fone-55", r"(?<![\d.])\+?55[\s-]?\(?\d{2}\)?[\s-]?9?\d{4}[\s-]?\d{4}(?!\d)", "[phone-removed]")
_p("pii", "fone-ddd", r"(?<![\d.])\(?\b\d{2}\)?[\s-]?9\d{4}[\s-]?\d{4}(?!\d)", "[phone-removed]")
_p("pii", "fone-fixo", r"(?<![\d.])\(\d{2}\)\s?\d{4}-\d{4}(?!\d)", "[phone-removed]")
_p("pii", "numero11", r"(?<![\d.\-:/])\d{11}(?![\d.\-:/])", "[number11-removed]")
# generic token (group dev only): >=32 chars with upper, lower and digit (git SHAs and paths are kept)
_GENERICO = re.compile(r"(?<![A-Za-z0-9_/.\-])[A-Za-z0-9_-]{32,}(?![A-Za-z0-9_/\-])")


def _generico(m: re.Match) -> str:
    s = m.group(0)
    if re.fullmatch(r"[0-9a-fA-F]+", s):  # git hash: useful context, not a secret
        return s
    if re.search(r"[A-Z]", s) and re.search(r"[a-z]", s) and re.search(r"\d", s):
        return "[token-removed]"
    return s


def _email_sub(m: re.Match) -> str:
    if m.group(0).lower() in EMAILS_PERMITIDOS or m.group(1).lower() in DOMINIOS_PERMITIDOS:
        return m.group(0)
    return "[email-removed]"


MASCARAR_DEV = True  # privacy.mask_dev_secrets (default true): also mask development secrets in the mirror


def configurar(mascarar_dev: bool = True, dominios=None, emails=None) -> None:
    """Applies the [privacy] section of the config (called by config.carregar)."""
    global MASCARAR_DEV
    MASCARAR_DEV = bool(mascarar_dev)
    if dominios is not None:
        DOMINIOS_PERMITIDOS.clear()
        DOMINIOS_PERMITIDOS.update(d.lower() for d in dominios)
    if emails is not None:
        EMAILS_PERMITIDOS.clear()
        EMAILS_PERMITIDOS.update(x.lower() for x in emails)


def mascarar(texto: str, dev: bool | None = None) -> str:
    """Applies the masks. Idempotent. `dev` turns the development-secrets group on/off (default: MASCARAR_DEV)."""
    return mascarar_com_contagem(texto, dev)[0]


def mascarar_com_contagem(texto: str, dev: bool | None = None) -> tuple[str, dict[str, int]]:
    """Like mascarar(), but also returns how many items of each kind were removed (for reports)."""
    dev = MASCARAR_DEV if dev is None else dev
    cont: dict[str, int] = {}
    if not texto:
        return texto, cont
    for grupo, nome, rx, sub in _PADROES:
        if grupo == "dev" and not dev:
            continue
        texto, n = rx.subn(sub, texto)
        if n:
            cont[nome] = n
    texto, n = _EMAIL.subn(_email_sub, texto)
    if n and "[email-removed]" in texto:
        cont["email"] = texto.count("[email-removed]")
    if dev:
        novo = _GENERICO.sub(_generico, texto)
        if novo != texto:
            cont["token-generico"] = 1
        texto = novo
    return texto, cont


def mascarar_obj(obj, dev: bool = True):
    """Masks every string inside a JSON-like structure (keeps it valid JSON)."""
    if isinstance(obj, str):
        return mascarar(obj, dev)
    if isinstance(obj, list):
        return [mascarar_obj(x, dev) for x in obj]
    if isinstance(obj, dict):
        return {k: OCULTO if re.search(r"(?i)key|token|password|passwd|secret|senha|credential|authorization|cookie", str(k))
                else mascarar_obj(v, dev) for k, v in obj.items()}
    return obj


# ---------------------------------------------------------------- what NEVER goes to the mirror
# Tools whose results are the owner's personal content (mail, drive, meetings, documents...).
FERRAMENTAS_PESSOAIS = re.compile(r"(?i)gmail|outlook|mail|google_?(drive|docs|sheets|slides)|calendar|meeting|drive")
# Tool inputs that touch secret files/commands: their results are dropped.
ENTRADA_SENSIVEL = re.compile(
    r"(?i)(\.env\b|auth\.json|id_rsa|id_ed25519|\.pem\b|/etc/ssh|restic|credentials|"
    r"\.config/gh|\.netrc|shadow|\bimap\b|gmail|printenv|\benv\b\s*(\||$)|secrets?)")


def resultado_deve_cair(nome_ferramenta: str, entrada: str) -> str | None:
    """Returns the reason (str) when this tool result must not reach the mirror."""
    if FERRAMENTAS_PESSOAIS.search(nome_ferramenta or ""):
        return "personal content (mail/drive/meetings) is never sent"
    if ENTRADA_SENSIVEL.search(entrada or ""):
        return "touches a sensitive file or command"
    return None


def _truncar(t: str, n: int) -> str:
    if len(t) <= n:
        return t
    return t[: n // 2] + f"\n[... {len(t) - n} characters omitted ...]\n" + t[-n // 2:]


def _texto_blocos(content) -> str:
    if isinstance(content, str):
        return content
    partes = []
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                partes.append(str(b.get("text", "")))
            elif isinstance(b, str):
                partes.append(b)
    return "\n".join(partes)


def _info_limite(d: dict) -> dict:
    info = ((d.get("apiErrorParams") or {}).get("rate_limit_info") or {}) if isinstance(d.get("apiErrorParams"), dict) else {}
    q = d.get("quotaLimits") or {}
    return {
        "reset_epoch": info.get("resetsAt") or q.get("resetsAt"),
        "tipo_limite": info.get("rateLimitType") or q.get("rateLimitType"),
        "status_limite": info.get("status") or q.get("status"),
    }


def registro_para_espelho(d: dict, tools: dict[str, tuple[str, str]],
                          max_texto: int = 6000, max_ferramenta: int = 500) -> list[dict]:
    """Converts ONE Claude Code transcript record into 0..n compact, masked records.

    `tools` maps tool_use_id -> (name, input) and is updated here. Sub-agent records (isSidechain),
    attachments and metadata are dropped: the sub-agent's final summary comes back as a tool_result.
    """
    tipo = d.get("type")
    if d.get("isSidechain") or tipo not in ("user", "assistant"):
        return []
    ts = d.get("timestamp")
    msg = d.get("message") or {}
    content = msg.get("content")
    saida: list[dict] = []

    if tipo == "assistant" and d.get("isApiErrorMessage"):
        reg = {"t": ts, "papel": "erro", "texto": mascarar(_texto_blocos(content))[:500],
               "error": d.get("error"), "apiError": d.get("apiError"), "status": d.get("apiErrorStatus"),
               "id": d.get("uuid")}
        reg.update(_info_limite(d))
        return [reg]

    if isinstance(content, str):
        if tipo == "user":
            txt = content
            if txt.startswith("<local-command") or txt.startswith("<command-"):
                txt = _truncar(txt, 300)
            saida.append({"t": ts, "papel": "usuario", "texto": mascarar(_truncar(txt, max_texto)),
                          "id": d.get("uuid"), "meta": bool(d.get("isMeta"))})
        else:
            saida.append({"t": ts, "papel": "assistente", "texto": mascarar(_truncar(content, max_texto)),
                          "id": d.get("uuid")})
        return saida

    if not isinstance(content, list):
        return []
    for b in content:
        if not isinstance(b, dict):
            continue
        bt = b.get("type")
        if bt == "text":
            papel = "usuario" if tipo == "user" else "assistente"
            saida.append({"t": ts, "papel": papel, "texto": mascarar(_truncar(str(b.get("text", "")), max_texto)),
                          "id": d.get("uuid"), "meta": bool(d.get("isMeta"))})
        elif bt == "tool_use":
            nome = str(b.get("name", ""))
            entrada = json.dumps(b.get("input", {}), ensure_ascii=False)
            tools[str(b.get("id"))] = (nome, entrada)
            if len(tools) > 800:
                for k in list(tools)[:200]:
                    tools.pop(k, None)
            motivo = resultado_deve_cair(nome, entrada)
            txt = "[input omitted: " + motivo + "]" if motivo and ENTRADA_SENSIVEL.search(entrada) else mascarar(_truncar(entrada, 300))
            saida.append({"t": ts, "papel": "ferramenta", "ferramenta": nome, "texto": txt})
        elif bt == "tool_result":
            nome, entrada = tools.get(str(b.get("tool_use_id")), ("", ""))
            motivo = resultado_deve_cair(nome, entrada)
            if motivo:
                txt = f"[result removed: {motivo}]"
            else:
                txt = mascarar(_truncar(_texto_blocos(b.get("content")), max_ferramenta))
            saida.append({"t": ts, "papel": "resultado", "ferramenta": nome, "texto": txt})
    return saida
