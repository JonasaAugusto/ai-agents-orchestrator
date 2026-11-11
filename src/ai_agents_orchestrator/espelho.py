"""Mirror (runs as ROOT when the root daemon is used: it is the only part that reads the originals).

Mechanical and on demand (no timers/polling). Delivers to Codex, in <state_dir>/mirror:
  - transcript-cauda.jsonl: ONLY the recent tail of the Claude Code session (last N text messages + API error
    records), without tool output. Codex uses it only to CHECK Claude's BRIEFING.md;
  - memoria/*.md: Claude Code memory files, masked;
  - hook-sinal.json: payload of the StopFailure hook (error, message, time);
  - repos-status.json + ../repos/<name>.git: bare clones of the allowed repos (including Claude's local
    branches that are not on GitHub).
Understanding the context and deciding what to do is Codex's job, not this module's.
"""
from __future__ import annotations

import fcntl
import glob
import grp
import json
import os
import pwd
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from . import sanitize
from .config import Config

USUARIO = "aao"  # replaced by cfg.service_user (see _ids)
TAIL_BYTES = 12 * 1024 * 1024

LEIAME = """# Mirror (read-only)

Copy generated for the backup agent. Personal data (e-mails, phone numbers, national ids) and credentials of
other services are masked; development secrets are masked too unless the operator disabled it. If something
seems missing, it was removed on purpose.

READ FIRST: ../para-codex/BRIEFING.md (and the task briefing in ../para-codex/tarefas/<id>.md).
This mirror is for CHECKING the briefing, not for rebuilding the context from scratch.

- transcript-cauda.jsonl: last text messages of the Claude Code session (papel: usuario=user | assistente=assistant | erro=error).
  "erro" records carry error/apiError/status and, when present, reset_epoch and tipo_limite (five_hour, seven_day).
  Limit messages look like: "You've hit your session limit · resets 2:40pm (Area/City)" or
  "You've hit your weekly limit · resets Nov 9, 2am (...)".
- memoria/*.md: working rules and project state (Claude Code memory).
- hook-sinal.json (optional): payload of the StopFailure hook.
- repos-status.json: status of the clones in ../repos/<name>.git (use git --git-dir).
"""


def _ids(usuario: str | None = None) -> tuple[int | None, int | None]:
    try:
        return pwd.getpwnam(usuario or USUARIO).pw_uid, grp.getgrnam(usuario or USUARIO).gr_gid
    except KeyError:
        return None, None


def _dono(path: Path, uid: int | None, gid: int | None, modo: int | None = None) -> None:
    try:
        if uid is not None:
            os.chown(path, uid, gid if gid is not None else -1)
        if modo is not None:
            os.chmod(path, modo)
    except OSError:
        pass


def _gravar(path: Path, texto: str, gid: int | None, modo: int = 0o640) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(texto, encoding="utf-8")
    os.chmod(tmp, modo)
    if gid is not None:
        try:
            os.chown(tmp, 0, gid)
        except OSError:
            pass
    os.replace(tmp, path)


def agora_iso(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts if ts is not None else time.time(), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------ transcript tail
def cauda_transcript(src: Path, max_msgs: int = 60, tail_bytes: int = TAIL_BYTES, max_texto: int = 2000,
                     dev: bool | None = None) -> list[dict]:
    """Last `max_msgs` text messages (user/assistant) + recent API errors, masked."""
    tamanho = src.stat().st_size
    with open(src, "rb") as f:
        f.seek(max(0, tamanho - tail_bytes))
        dados = f.read()
    linhas = dados.split(b"\n")
    if tamanho > tail_bytes:
        linhas = linhas[1:]  # first line is probably truncated
    msgs: list[dict] = []
    erros: list[dict] = []
    tools: dict = {}
    for linha in linhas:
        if not linha.strip():
            continue
        try:
            d = json.loads(linha)
        except ValueError:
            continue
        for reg in sanitize.registro_para_espelho(d, tools, max_texto=max_texto):
            if reg["papel"] == "erro":
                erros.append(reg)
            elif reg["papel"] in ("usuario", "assistente") and not reg.get("meta") and (reg.get("texto") or "").strip():
                msgs.append(reg)
    cauda = msgs[-max_msgs:] + erros[-10:]
    cauda.sort(key=lambda r: r.get("t") or "")
    for r in cauda:
        r["texto"] = sanitize.mascarar(r["texto"], dev)
    return cauda


def _sessao_atual(cfg: Config) -> Path | None:
    if not cfg.fonte_transcripts.is_dir():
        return None
    cands = list(cfg.fonte_transcripts.glob("*.jsonl"))
    return max(cands, key=lambda p: p.stat().st_mtime) if cands else None


def _espelhar_memoria(cfg: Config, gid: int | None) -> int:
    n = 0
    destino = cfg.mirror / "memoria"
    destino.mkdir(parents=True, exist_ok=True)
    vistos = set()
    if cfg.fonte_memoria.is_dir():
        for md in sorted(cfg.fonte_memoria.glob("*.md")):
            _gravar(destino / md.name, sanitize.mascarar(md.read_text(encoding="utf-8", errors="replace")), gid)
            vistos.add(md.name)
            n += 1
    for velho in destino.glob("*.md"):
        if velho.name not in vistos:
            velho.unlink()
    os.chmod(destino, 0o750)
    if gid is not None:
        try:
            os.chown(destino, 0, gid)
        except OSError:
            pass
    return n


# ------------------------------------------------------------------ repositories
def _fonte_repo(spec: dict) -> Path | str | None:
    if spec.get("fonte_url") and not spec.get("fonte") and not spec.get("fonte_glob"):
        return spec["fonte_url"]
    if spec.get("fonte"):
        p = Path(spec["fonte"])
        return p if (p / ".git").exists() or (p / "HEAD").exists() else None
    if spec.get("fonte_glob"):
        cands = [Path(p) for p in glob.glob(spec["fonte_glob"]) if (Path(p) / ".git").exists()]
        return max(cands, key=lambda p: p.stat().st_mtime) if cands else None
    return None


def _git(args: list[str], cwd: Path | None = None, timeout: int = 300) -> subprocess.CompletedProcess:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": "/root", "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C.UTF-8"}
    return subprocess.run(["git", "-c", "safe.directory=*", *args], cwd=cwd, env=env, capture_output=True,
                          text=True, timeout=timeout)


def _url_github(spec: dict) -> str | None:
    gh = spec.get("github")
    return f"https://github.com/{gh}.git" if gh else None


def _sha_ref(dst: Path | str, base: str, bare: bool = True) -> str | None:
    """SHA of refs/heads/<base> in a repo (bare or with .git)."""
    args = ["--git-dir", str(dst)] if bare else ["-C", str(dst)]
    r = _git([*args, "rev-parse", "--verify", "-q", f"refs/heads/{base}"])
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def _sha_origem(spec: dict, url: str | None, base: str) -> str | None:
    """SHA of the base at the source of truth: GitHub ls-remote (repo with GitHub) or the local source branch."""
    if url:
        r = _git(["ls-remote", url, f"refs/heads/{base}"], timeout=60)
        linha = r.stdout.split() if r.returncode == 0 else []
        return linha[0] if linha else None
    fonte = _fonte_repo(spec)
    if not fonte or str(fonte).startswith(("http://", "https://")):
        return None
    f = Path(str(fonte))
    return _sha_ref(f / ".git" if (f / ".git").exists() else f, base)


def _com_sha(spec: dict, dst: Path, st: dict, url: str | None) -> dict:
    """Adds to a SUCCESSFUL refresh status the mirror SHA, the origin SHA and whether it could be confirmed."""
    if not st.get("ok"):
        return st
    base = spec.get("branch_base", "main")
    st["sha"] = _sha_ref(dst, base)
    if url or _fonte_repo(spec):
        st["sha_origem"] = _sha_origem(spec, url, base)
        st["confirmado"] = bool(st["sha_origem"])
    return st


def _atualizar_um(spec: dict, dst: Path) -> dict:
    return _com_sha(spec, dst, _atualizar_um_bruto(spec, dst), _url_github(spec))


def _atualizar_um_bruto(spec: dict, dst: Path) -> dict:
    """Repo with `github`: the source of truth is ALWAYS GitHub (current branches and tags, root's credential);
    the local copy (`fonte`/`fonte_glob`) is only a fallback without network or when the repo has no GitHub.
    Claude's local branches (fixed `fonte`) go to `refs/heads/local-src/*`, never touching GitHub branches."""
    base = spec.get("branch_base", "main")
    url = _url_github(spec)
    if url:
        if not dst.exists():
            r = _git(["init", "--bare", "-q", str(dst)])
            if r.returncode != 0:
                return {"ok": False, "motivo": sanitize.mascarar(r.stderr)[:200]}
        r = _git(["--git-dir", str(dst), "fetch", "-q", "--force", "--tags", url,
                  "+refs/heads/*:refs/heads/*"], timeout=300)
        if r.returncode == 0:
            _git(["--git-dir", str(dst), "symbolic-ref", "HEAD", f"refs/heads/{base}"])
            if spec.get("fonte") and _fonte_repo({"fonte": spec["fonte"]}):
                _git(["--git-dir", str(dst), "fetch", "-q", "--force", str(spec["fonte"]),
                      "+refs/heads/*:refs/heads/local-src/*"])
            return {"ok": True, "fonte": "github", "atualizado": agora_iso()}
        erro_rede = sanitize.mascarar(r.stderr)[:200]
        fonte = _fonte_repo(spec)
        if not fonte or not dst.exists():
            return {"ok": False, "motivo": "GitHub unavailable: " + erro_rede, "fonte": "github"}
        # no network: fall back to the local copy, but flag it as possibly stale
        r2 = _git(["--git-dir", str(dst), "fetch", "-q", "--force", str(fonte), "+refs/heads/*:refs/heads/local-src/*"])
        return {"ok": r2.returncode == 0, "fonte": Path(str(fonte)).name, "desatualizado": True,
                "motivo": "GitHub unavailable, using local copy: " + erro_rede, "atualizado": agora_iso()}
    fonte = _fonte_repo(spec)
    if not fonte:
        return {"ok": False, "motivo": "local source unavailable"}
    if not dst.exists():
        r = _git(["clone", "--mirror", "--no-hardlinks", str(fonte), str(dst)])
    else:
        r = _git(["--git-dir", str(dst), "fetch", "--prune", "--force", str(fonte), "+refs/*:refs/*"])
    ok = r.returncode == 0
    st = {"ok": ok, "fonte": Path(str(fonte)).name, "atualizado": agora_iso()}
    if not ok:
        st["motivo"] = sanitize.mascarar(r.stderr)[:200]
    else:
        _git(["--git-dir", str(dst), "config", "--unset-all", "remote.origin.url"])
        _git(["--git-dir", str(dst), "symbolic-ref", "HEAD", f"refs/heads/{base}"])
    return st


def atualizar_repos(cfg: Config, nomes: list[str] | None = None) -> dict:
    uid, gid = _ids(cfg.service_user)
    cfg.repos_dir.mkdir(parents=True, exist_ok=True)
    _dono(cfg.repos_dir, uid, gid, 0o750)
    status: dict = {}
    for nome, spec in cfg.repos.items():
        if nomes and nome not in nomes:
            continue
        dst = cfg.repos_dir / f"{nome}.git"
        try:
            status[nome] = _atualizar_um(spec, dst)
        except (subprocess.TimeoutExpired, OSError) as e:
            status[nome] = {"ok": False, "motivo": type(e).__name__}
        if dst.exists() and uid is not None:
            for raiz, _dirs, arqs in os.walk(dst):
                _dono(Path(raiz), uid, gid)
                for a in arqs:
                    _dono(Path(raiz) / a, uid, gid)
    _gravar(cfg.mirror / "repos-status.json", json.dumps(status, ensure_ascii=False, indent=1), gid)
    return status


def espelhar(cfg: Config, agora: float | None = None, hook_payload: dict | None = None,
             com_repos: bool = True, cauda_msgs: int = 60) -> dict:
    """One on-demand pass. Protected by flock."""
    agora = agora if agora is not None else time.time()
    uid, gid = _ids(cfg.service_user)
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    cfg.mirror.mkdir(parents=True, exist_ok=True)
    _dono(cfg.mirror, 0, gid, 0o750)
    lock = open(cfg.state_dir / "espelho.lock", "a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock.close()
        return {"ok": False, "motivo": "another pass in progress"}
    try:
        src = _sessao_atual(cfg)
        n = 0
        if src:
            cauda = cauda_transcript(src, cauda_msgs)
            n = len(cauda)
            _gravar(cfg.mirror / "transcript-cauda.jsonl",
                    "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in cauda), gid)
        nmem = _espelhar_memoria(cfg, gid)
        if hook_payload:
            hook = {"recebido_em": agora_iso(agora), "error": hook_payload.get("error"),
                    "error_details": sanitize.mascarar(str(hook_payload.get("error_details") or ""))[:300],
                    "mensagem": sanitize.mascarar(str(hook_payload.get("last_assistant_message") or ""))[:500],
                    "session_id": hook_payload.get("session_id")}
            _gravar(cfg.mirror / "hook-sinal.json", json.dumps(hook, ensure_ascii=False, indent=1), gid)
        _gravar(cfg.mirror / "LEIAME.md", LEIAME, gid)
        repos = atualizar_repos(cfg) if com_repos else None
        return {"ok": True, "cauda": n, "memoria": nmem, "repos": repos,
                "sessao": src.stem if src else None}
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()
