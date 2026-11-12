"""Service guarantees for every Codex run: a snapshot injected into the prompt (time, SHA of each repo, real
capabilities, briefing age from the time DECLARED inside it) and the honest state of the final report.

Everything here is generated in code from the config and the disk, never from rule text: Codex trusts the snapshot,
not stale descriptions of the environment.
"""
from __future__ import annotations

import getpass
from . import compat
import re
import unicodedata
from datetime import datetime
from . import config, engines, prompts, regras, workspace
from .config import Config

BRIEFING_VELHO_MIN = 6 * 60  # a briefing older than 6 h triggers a warning
TOLERANCIA_FUTURO_MIN = 5  # clock skew/rounding: up to 5 min ahead is not "future"
# "Updated: YYYY-MM-DD HH:MM" (Portuguese "Atualizado em:" also accepted), in the configured timezone
RX_ATUALIZADO = re.compile(r"(?im)^\s*(?:Updated(?: at| on)?|Atualizado em)\s*:\s*(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})")
RX_SHA = re.compile(r"\b[0-9a-f]{7,40}\b")


class SincronizacaoErro(workspace.GitErro):
    """The repo mirror cannot be proven current: the task must not start."""


# ------------------------------------------------------------------ briefing
def briefing_declarado(texto: str) -> float | None:
    """Epoch of the time the briefing itself declares ("Updated: YYYY-MM-DD HH:MM", configured timezone)."""
    m = RX_ATUALIZADO.search(texto or "")
    if not m:
        return None
    a, mo, d, h, mi = (int(x) for x in m.groups())
    try:
        return datetime(a, mo, d, h, mi, tzinfo=config.fuso()).timestamp()
    except ValueError:
        return None


def avisos_briefing(texto: str | None, agora: float, shas: dict[str, str]) -> dict:
    """{"declarado": epoch|None, "idade_min": int|None, "avisos": [...]}. Age comes from the declared time, not mtime."""
    if texto is None:
        return {"declarado": None, "idade_min": None, "avisos": ["there is no readable BRIEFING.md: give the transcript tail more weight"]}
    avisos: list[str] = []
    dec = briefing_declarado(texto)
    idade = None
    if dec is None:
        avisos.append("the briefing does not declare 'Updated:'; its age is unknown, double-check the facts")
    else:
        idade = int((agora - dec) / 60)
        if idade < -TOLERANCIA_FUTURO_MIN:
            avisos.append(f"the briefing declares a time in the FUTURE ({datetime.fromtimestamp(dec, config.fuso()):%Y-%m-%d %H:%M}); "
                          "distrust its content and double-check the facts")
        elif idade > BRIEFING_VELHO_MIN:
            avisos.append(f"the briefing was updated {idade // 60} h {idade % 60:02d} min ago (stale, > 6 h); "
                          "there may be later work it does not mention")
    for linha in texto.splitlines():
        for repo, sha in shas.items():
            if not sha or not re.search(rf"(?<![\w-]){re.escape(repo)}(?![\w-])", linha):
                continue
            for cand in RX_SHA.findall(linha.lower()):
                if not (sha.startswith(cand) or cand.startswith(sha[:len(cand)])):
                    avisos.append(f"the briefing cites SHA {cand[:12]} for {repo}, but the mirror's current base is {sha[:12]}")
    return {"declarado": dec, "idade_min": idade, "avisos": avisos[:6]}


# ------------------------------------------------------------------ repos / SHA
def sha_base(cfg: Config, repo: str) -> str | None:
    """SHA of the base (branch_base) in the repo's bare mirror."""
    bare = cfg.repos_dir / f"{repo}.git"
    if not bare.exists():
        return None
    r = workspace.git(cfg, ["--git-dir", str(bare), "rev-parse", "--verify", "-q", f"refs/heads/{workspace.base_do_repo(cfg, repo)}"],
                      check=False)
    sha = r.stdout.strip()
    return sha if r.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", sha) else None


def repos_envolvidos(cfg: Config, repo: str, *textos: str) -> list[str]:
    """The task's repo plus any other configured repo mentioned in the text (e.g. the docs repo). Stable order."""
    juntos = " ".join(t for t in textos if t)
    out = [repo] if repo else []
    for nome in cfg.repos:
        if nome != repo and re.search(rf"(?<![\w-]){re.escape(nome)}(?![\w-])", juntos):
            out.append(nome)
    return out


def checar_status_espelho(repo: str, r: dict, sha_local: str | None) -> str | None:
    """None if the mirror refresh is provably good; otherwise the blocking message."""
    st = r.get("status") or {}
    if not r.get("ok"):
        return f"refreshing the mirror of {repo} failed ({str(r.get('erro') or st.get('motivo') or 'no detail')[:150]})"
    if st.get("desatualizado"):
        return f"the mirror of {repo} is stale ({str(st.get('motivo') or 'GitHub unavailable')[:150]})"
    if not sha_local:
        return f"mirror of {repo} has no base branch (no SHA to record)"
    if st.get("confirmado") is False:
        return f"could not confirm the SHA of {repo} on GitHub; not proceeding with a possibly stale mirror"
    if st.get("sha") and st["sha"] != sha_local:
        return f"the mirror SHA of {repo} ({sha_local[:10]}) differs from the one reported by the refresh ({st['sha'][:10]})"
    if st.get("sha_origem") and st["sha_origem"] != sha_local:
        return (f"the mirror of {repo} is at {sha_local[:10]} but GitHub (origin) is at {st['sha_origem'][:10]}; "
                "not proceeding with a stale mirror")
    return None


# ------------------------------------------------------------------ capabilities and snapshot
def capacidades(sandbox: str, modelo: str | None, esforco: str | None, motor: str = "codex", *,
                rede_config: bool = False, acesso_total: bool = False, acesso_total_leitura: bool = False) -> dict:
    """What the run REALLY has (same logic as CodexRunner.comando), not what some rule text says."""
    efetivo = engines.sandbox_efetivo(sandbox, acesso_total, acesso_total_leitura) if motor == "codex" else "acceptEdits (claude -p)"
    if efetivo == "read-only":
        rede = "no"
    elif efetivo == "workspace-write":
        rede = "yes" if rede_config else "no"
    else:
        rede = "yes"
    return {"uid": compat.euid(), "usuario": _usuario(), "sandbox": efetivo, "sandbox_configurado": sandbox, "rede": rede,
            "modelo": modelo or "Codex default", "esforco": esforco or "Codex default", "motor": motor}


def _usuario() -> str:
    try:
        import pwd
        return pwd.getpwuid(compat.euid()).pw_name
    except (KeyError, ImportError):
        return getpass.getuser()


def montar_snapshot(cfg: Config, agora: float, *, sandbox: str, modelo: str | None, esforco: str | None, motor: str = "codex",
                    briefing_texto: str | None = None, sincronizados: dict[str, str] | None = None,
                    envolvidos: list[str] | None = None) -> dict:
    """{"texto", "avisos", "shas", "capacidades", "briefing"}. `sincronizados`: repos whose mirror was refreshed and checked
    against GitHub just now (SHA recorded); the others show the mirror's SHA without that guarantee."""
    cap = capacidades(sandbox, modelo, esforco, motor, rede_config=cfg.codex_network, acesso_total=cfg.codex_full_access,
                      acesso_total_leitura=cfg.codex_full_access_read_only)
    shas = {r: sha_base(cfg, r) or "" for r in cfg.repos}
    sincronizados = sincronizados or {}
    shas.update({r: s for r, s in sincronizados.items() if s})
    brief = avisos_briefing(briefing_texto, agora, shas)
    quando = datetime.fromtimestamp(agora, config.fuso())
    avisos = list(brief["avisos"])
    aviso = regras.aviso_regras(prompts.regras_base(cfg), quando.date())
    if aviso:
        avisos.append(aviso)
    linhas = ["EXECUTION SNAPSHOT (generated by the service right now; it overrides any rule or briefing describing the environment):",
              f"- Time ({config.nome_fuso()}): {quando:%Y-%m-%d %H:%M}",
              f"- Execution: uid={cap['uid']} ({cap['usuario']}), sandbox={cap['sandbox']}"
              + (f" (configured {cap['sandbox_configurado']})" if cap["sandbox"] != cap["sandbox_configurado"] else "")
              + f", network={cap['rede']}, model={cap['modelo']}, effort={cap['esforco']}",
              f"- Base SHA of each repo in the mirror (git --git-dir {cfg.repos_dir}/<repo>.git):"]
    for r in cfg.repos:
        marca = ""
        if r in sincronizados:
            marca = " [synced with origin just now]"
        elif envolvidos and r in envolvidos:
            marca = " [involved in this task]"
        linhas.append(f"    {r}: {shas.get(r, '')[:12] or '(no mirror)'}{marca}")
    if brief["declarado"]:
        idade = brief["idade_min"]
        linhas.append(f"- Briefing: declares 'Updated' {datetime.fromtimestamp(brief['declarado'], config.fuso()):%Y-%m-%d %H:%M}"
                      + (f" ({idade // 60} h {idade % 60:02d} min ago)" if idade is not None and idade >= 0 else ""))
    elif briefing_texto is not None:
        linhas.append("- Briefing: no declared time")
    else:
        linhas.append("- Briefing: unavailable")
    for a in avisos:
        linhas.append(f"WARNING: {a}.")
    return {"texto": "\n".join(linhas), "avisos": avisos, "shas": shas, "capacidades": cap, "briefing": brief}


# ------------------------------------------------------------------ honest report state
ESTADOS_RELATORIO = {"completed": "concluida", "complete": "concluida", "done": "concluida", "finished": "concluida",
                     "partial": "parcial", "incomplete": "parcial", "blocked": "bloqueada","concluida": "concluida", "concluido": "concluida", "ok": "concluida", "pronta": "concluida", "pronto": "concluida",
                     "completa": "concluida", "completo": "concluida", "feita": "concluida", "feito": "concluida",
                     "bloqueada": "bloqueada", "bloqueado": "bloqueada", "bloqueio": "bloqueada",
                     "parcial": "parcial", "parcialmente": "parcial", "incompleta": "parcial", "incompleto": "parcial"}
RX_ESTADO = re.compile(r"(?im)^[\s>*_#`-]*(?:estado|status|state|situacao|resultado|result)(?:\s+final)?[\s*_`]*[:=][\s*_`]*([a-z]+)")


def estado_relatorio(texto: str) -> str | None:
    """State declared in the final report ("Status: completed | partial | blocked"; Portuguese "Estado: concluida |
    parcial | bloqueada" also accepted), first occurrence at the start of a line. Returns the internal value
    (concluida/parcial/bloqueada) or None when there is no field (the caller treats it as completed)."""
    plano = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode()
    for m in RX_ESTADO.finditer(plano):
        v = ESTADOS_RELATORIO.get(m.group(1).lower())
        if v:
            return v
    return None
