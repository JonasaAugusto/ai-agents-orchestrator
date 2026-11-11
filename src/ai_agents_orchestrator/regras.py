"""Rules file checks ("rules linter").

The base rules file (``paths.rules_file``) is injected into every Codex prompt, so it must stay short, dated and
consistent. ``aao check-rules`` runs these checks; an expired review date only WARNS, everything else fails.
"""
from __future__ import annotations

import glob
import re
import unicodedata
from datetime import date
from pathlib import Path

MAX_LINHAS = 60
MAX_BYTES = 32 * 1024
MAX_LINHAS_TEMPLATE = 40


def _normal(texto: str) -> str:
    normal = unicodedata.normalize("NFKD", texto.casefold())
    return "".join(c for c in normal if not unicodedata.combining(c))


def vencimento_regras(texto: str) -> date | None:
    """Review date: "review by: YYYY-MM-DD" (Portuguese "revisar ate: YYYY-MM-DD" also accepted)."""
    validade = re.search(r"(?:review by|revisar ate)\s*:?\s*(\d{4}-\d{2}-\d{2})", _normal(texto))
    try:
        return date.fromisoformat(validade[1]) if validade else None
    except ValueError:
        return None


def aviso_regras(texto: str, hoje: date) -> str | None:
    vencimento = vencimento_regras(texto)
    if vencimento is not None and hoje > vencimento:
        return f"rules expired, please review the rules file (review by: {vencimento.isoformat()})"
    return None


_NEGA = re.compile(
    r"(?:denied|forbidden|blocked|inaccessible|negad\w*|proibid\w*|inacessivel|bloquead\w*|"
    r"(?:not|never|no|nao|nunca|sem)\s+(?:\w+\s+){0,3}"
    r"(?:access\w*|read\w*|touch\w*|allow\w*|authori[sz]\w*|acess\w*|ler|lei\w*|consult\w*|toqu\w*|autoriz\w*|permit\w*|liberad\w*))")
_PERMITE = re.compile(
    r"authori[sz]\w*|allow\w*|permit\w*|full access|unrestricted|may (?:read|access)|can (?:read|access)|"
    r"autoriz\w*|liberad\w*|acesso\s+(?:total|irrestrito)|pode\s+(?:ler|acessar|consultar)")


def problemas_regras(texto: str, raiz: Path, hoje: date) -> list[str]:
    """Problems in a rules text. `raiz` resolves relative paths cited in the text. Expiry is reported separately by
    aviso_regras (warning only)."""
    erros = []
    if len(texto.splitlines()) > MAX_LINHAS:
        erros.append(f"more than {MAX_LINHAS} lines")
    if len(texto.encode("utf-8")) > MAX_BYTES:
        erros.append("more than 32 KiB")
    normal = _normal(texto)
    if vencimento_regras(texto) is None:
        erros.append("missing or invalid review date")

    acessos = {p: set() for p in ("/root", "/etc")}
    trechos = re.split(r"[.!?;]|\n(?=\s*(?:[-*#]|not\b|never\b|nao\b|nunca\b|access\b|acesso\b|may\b|pode\b))"
                       r"|\b(?:but|however|mas|porem)\b", normal)
    for trecho in trechos:
        negado, permitido = _NEGA.search(trecho), _PERMITE.search(trecho)
        for caminho in re.findall(r"/(?:root|etc)(?![\w/*-]|\.[\w])", trecho):
            if negado:
                acessos[caminho].add("denied")
            elif permitido:
                acessos[caminho].add("allowed")
    for caminho, estados in acessos.items():
        if len(estados) > 1:
            erros.append(f"contradictory access: {caminho}")

    # Absolute/relative paths, even outside backticks; URLs are not files.
    sem_urls = re.sub(r"https?://[^\s`<>]+", "", texto)
    caminhos = set(re.findall(r"(?<![\w/])(?:/|(?:[\w.-]+/)+)[\w./*<>-]+", sem_urls))
    caminhos.update(re.findall(r"(?<![\w/.-])([\w.-]+\.(?:py|sh|md|jsonl?))(?![\w/-]|\.[\w])", sem_urls))
    for caminho in sorted(caminhos):
        caminho = caminho.rstrip(".,")
        # Slashes in dates, alternatives and branch names do not cite files.
        if not caminho.startswith("/") and not re.search(r"\.[a-zA-Z][\w-]*$", caminho):
            continue
        padrao = re.sub(r"<[^>]+>", "*", caminho)
        destino = Path(padrao) if padrao.startswith("/") else raiz / padrao
        if not glob.glob(str(destino)):
            erros.append(f"path does not exist: {caminho}")
    return erros


def problemas_template(caminho: Path) -> list[str]:
    if not caminho.is_file():
        return [f"template missing: {caminho.name}"]
    if len(caminho.read_text(encoding="utf-8").splitlines()) > MAX_LINHAS_TEMPLATE:
        return [f"template longer than {MAX_LINHAS_TEMPLATE} lines: {caminho.name}"]
    return []
