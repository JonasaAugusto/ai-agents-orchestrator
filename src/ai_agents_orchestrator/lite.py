"""User-owned backend, in the serve process. No privileged files or Linux APIs.

This is a convenience boundary, not isolation from engines running as the same uid.
Only the interactive publish CLI constructs a backend that permits base pushes.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

from . import compat, engines, espelho, gitseguro, sanitize, workspace
from .config import Config


def serve(cfg: Config, dry_run: bool = False) -> int:
    from .cli import montar_servico
    if compat.euid() == 0:
        print("lite: run aao serve as your normal user, without sudo")
        return 2
    cfg.state_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    compat.private(cfg.state_dir)
    svc = None
    try:
        with compat.file_lock(cfg.state_dir / "serve.lock", blocking=False):
            svc = montar_servico(cfg, dry_run, True)
            svc.rodar()
        return 0
    except BlockingIOError:
        print("lite: aao serve is already running")
        return 2
    except OSError as e:
        print(f"lite: {e}")
        return 1
    finally:
        if svc:
            svc.estado.c.close()
            svc.quadro.c.close()


def write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            compat.private(Path(name))
            stream.write(text)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


lock = compat.file_lock


def nome_pasta_projeto(caminho: str | os.PathLike) -> str:
    """Claude Code names a project's transcript folder after its path, every non-alphanumeric char becoming '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(caminho))


def pasta_do_projeto(cfg) -> tuple[Path, str]:
    """Transcript folder of the project in use and where that choice came from.

    Order: explicit `lite.transcripts_project`; else the directory where `aao serve` runs; else a transcripts
    directory that already is a single project folder (jsonl files directly inside)."""
    raiz = cfg.fonte_transcripts
    explicito = (getattr(cfg, "transcripts_project", "") or "").strip()
    if explicito:
        # Either the encoded folder name already, or a project path (which gets encoded like Claude Code does).
        direto = raiz / explicito
        if not re.search(r"[\\/:]", explicito) and direto.is_dir():
            return direto, "lite.transcripts_project"
        return raiz / nome_pasta_projeto(explicito), "lite.transcripts_project"
    atual = raiz / nome_pasta_projeto(os.getcwd())
    if atual.is_dir():
        return atual, "cwd"
    # fonte_transcripts já aponta para a pasta de um projeto (jsonl direto, sem subpastas de projeto).
    return (raiz, "transcripts dir") if any(raiz.glob("*.jsonl")) else (atual, "cwd")


def diagnostico_transcripts(cfg) -> tuple[str, str, str]:
    """(state, message, fix) for the lite transcript mirror; states use the doctor values "ok", "warn" and "FAIL"."""
    pasta, origem = pasta_do_projeto(cfg)
    if pasta.is_dir() and any(pasta.glob("*.jsonl")):
        return "ok", f"mirroring {pasta} (chosen by {origem})", ""
    chave = "[lite] transcripts_project = \"<full path of your project folder>\""
    if origem == "lite.transcripts_project":
        return ("FAIL", f"lite.transcripts_project = {cfg.transcripts_project!r} points to {pasta}, which has no "
                "Claude Code transcripts", "Fix the value in config.toml, or open that project once in Claude Code.")
    if not cfg.fonte_transcripts.is_dir():
        return ("warn", f"transcripts directory {cfg.fonte_transcripts} does not exist yet",
                "Use Claude Code once, or set paths.claude_transcripts to the right directory.")
    return ("warn", f"no Claude Code transcripts for the folder where aao serve runs ({pasta})",
            f"Set it explicitly in config.toml: {chave}; or start aao serve from your project folder.")


class LocalBackend:
    def __init__(self, cfg: Config, *, approved_cli: bool = False):
        self.cfg, self.approved_cli = cfg, approved_cli

    def _git(self, args, cwd=None):
        # Only this backend's own gitdirs use the user's credential helper.
        env = engines.env_usuario()
        env.update(workspace.env_git(self.cfg))
        env.update(HOME=str(Path.home()), PATH=os.environ.get("PATH", "/usr/bin:/bin"),
                   GIT_CONFIG_GLOBAL=str(Path.home() / ".gitconfig"))
        r = gitseguro.git_confiavel(args, cwd=cwd, env=env)
        if r.returncode:
            raise workspace.GitErro(sanitize.mascarar(r.stderr or r.stdout, dev=True)[-300:])
        return r.stdout.strip()

    def _github_url(self, gh: str) -> str:
        # SSH só por escolha explícita (publish.push_url_scheme = "ssh"): quem usa chave em vez de token HTTPS.
        if self.cfg.push_url_scheme == "ssh":
            return f"git@github.com:{gh}.git"
        return f"https://github.com/{gh}.git"

    def _url(self, repo: str) -> str:
        spec = self.cfg.repos.get(repo, {})
        gh = spec.get("github", "")
        if gh and gh in self.cfg.push_allowlist and re.fullmatch(r"[\w.-]+/[\w.-]+", gh):
            return self._github_url(gh)
        if self.cfg.permitir_url_local and spec.get("github_url"):
            return spec["github_url"]
        raise workspace.GitErro("repo not in publish.push_allowlist")

    def espelhar_repo(self, repo):
        if repo not in self.cfg.repos or not re.fullmatch(r"[\w.-]+", repo) or repo in (".", ".."):
            return {"ok": False, "erro": "repo not allowed"}
        dst = self.cfg.repos_dir / f"{repo}.git"
        spec = self.cfg.repos[repo]
        base = spec.get("branch_base", "main")
        if not re.fullmatch(r"[\w.-]+", base) or base.startswith("-"):
            return {"ok": False, "erro": "invalid base"}
        try:
            with lock(self.cfg.state_dir / "mirror.lock"):
                dst.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
                if not dst.exists():
                    self._git(["init", "--bare", "-q", str(dst)])
                gh = spec.get("github")
                if gh:
                    if not re.fullmatch(r"[\w.-]+/[\w.-]+", gh):
                        raise workspace.GitErro("invalid github repo")
                    self._git(["fetch", "--no-tags", self._github_url(gh),
                               "+refs/heads/*:refs/heads/*"], dst)
                else:
                    src = Path(espelho._fonte_repo(spec) or "")
                    gd = src / ".git" if (src / ".git").is_dir() else src
                    if not (gd / "HEAD").is_file():
                        raise workspace.GitErro("local source unavailable")
                    with gitseguro.repositorio_isolado(gd) as private:
                        self._git(["fetch", "--no-tags", str(private), "+refs/heads/*:refs/heads/*"], dst)
                self._git(["symbolic-ref", "HEAD", f"refs/heads/{base}"], dst)
                self._git(["rev-parse", f"refs/heads/{base}"], dst)
                # Lite task branches stay local, but cross-review must still see them.
                for source in self._workspaces_recentes(repo):
                    with gitseguro.repositorio_isolado(source) as private:
                        self._git(["fetch", "--no-tags", str(private),
                                   "+refs/heads/agent/*:refs/heads/agent/*"], dst)
            status = {"ok": True, "fonte": "github" if gh else str(src)}
            return {"ok": True, "status": status}
        except (workspace.GitErro, OSError, subprocess.TimeoutExpired) as e:
            return {"ok": False, "erro": sanitize.mascarar(str(e), dev=True)}

    MAX_WORKSPACES_ESPELHADOS = 20

    def _workspaces_recentes(self, repo: str) -> list[Path]:
        """Só os workspaces mais recentes entram no espelho: o custo não cresce com o histórico de tarefas."""
        fontes = [p for p in (self.cfg.workspaces / repo).glob("*/.git") if p.exists()]
        fontes.sort(key=lambda p: p.stat().st_mtime_ns, reverse=True)
        return fontes[:self.MAX_WORKSPACES_ESPELHADOS]

    def _pasta_do_projeto(self) -> Path:
        return pasta_do_projeto(self.cfg)[0]

    def _mirror(self, hook=None, session=None, usage=False):
        cfg = self.cfg
        with lock(cfg.state_dir / "transcript.lock"):
            src = None
            if session is not None:
                if not isinstance(session, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", session):
                    return {"ok": False, "erro": "invalid session id"}
                candidates = list(cfg.fonte_transcripts.rglob(session + ".jsonl"))
            else:
                # Sem sessão do hook, só o projeto em uso (diretório do cwd) ou uma pasta de projeto única;
                # nunca a árvore inteira de ~/.claude/projects, que mistura outros projetos.
                candidates = list(self._pasta_do_projeto().glob("*.jsonl"))
            candidates = [p for p in candidates if p.is_file() and not p.is_symlink()
                          and p.resolve().is_relative_to(cfg.fonte_transcripts.resolve())]
            if candidates:
                src = max(candidates, key=lambda p: p.stat().st_mtime_ns)
            tail = espelho.cauda_transcript(src) if src else []
            write_private(cfg.mirror / "transcript-cauda.jsonl", "".join(json.dumps(r) + "\n" for r in tail))
            if usage:
                return {"ok": bool(src), "contadores": [r for r in tail if r["papel"] == "uso"]}
            memory = cfg.mirror / "memoria"
            memory.mkdir(mode=0o700, parents=True, exist_ok=True)
            seen = set()
            for md in cfg.fonte_memoria.glob("*.md"):
                if not md.is_symlink():
                    write_private(memory / md.name, sanitize.mascarar(md.read_text(encoding="utf-8"), dev=True))
                    seen.add(md.name)
            for old in memory.glob("*.md"):
                if old.name not in seen:
                    old.unlink()
            if hook:
                write_private(cfg.mirror / "hook-sinal.json", json.dumps({
                    k: sanitize.mascarar(str(hook.get(k, "")), dev=True)[:500]
                    for k in ("error", "error_details", "last_assistant_message", "session_id")}))
            return {"ok": True, "cauda": len(tail), "memoria": len(seen)}

    def espelhar(self, hook=None):
        r = self._mirror(hook, session=(hook or {}).get("session_id"))
        r["repos"] = {name: self.espelhar_repo(name) for name in self.cfg.repos}
        return r

    def espelhar_uso(self, sessao=None):
        return self._mirror(session=sessao, usage=True)

    def info(self, repo):
        return {"ok": True, "deploy": False}

    def acordar_claude(self, *_args, **_kwargs):
        return {"ok": False, "erro": "lite: use hook-prompt for handoff"}

    def publicar(self, *_args, **_kwargs):
        return {"ok": False, "erro": "lite has no deployment runner"}

    def push_branch(self, repo, tid, branch):
        # Local delivery until the human explicitly publishes the reviewed SHA.
        return {"ok": True, "local": True, "hash": workspace.git(self.cfg,
                ["rev-parse", f"refs/heads/{branch}"], self.cfg.workspaces / repo / str(tid)).stdout.strip()}

    def push_base(self, repo, tid, branch, *, commit, destino, agora=None):
        if not self.approved_cli:
            return {"ok": False, "erro": "lite: run aao publish interactively"}
        try:
            url = self._url(repo)
            if (not re.fullmatch(r"agent/[A-Za-z0-9._/-]+", branch) or ".." in branch
                    or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit)
                    or destino != workspace.base_do_repo(self.cfg, repo)):
                raise workspace.GitErro("invalid approved branch, commit or destination")
            ws = self.cfg.workspaces / repo / str(int(tid))
            with lock(self.cfg.state_dir / "publish.lock"):
                # Fetch objects through a generated gitdir; never run workspace hooks/config.
                with tempfile.TemporaryDirectory(prefix="publish-", dir=self.cfg.spool) as temp:
                    gd = Path(temp) / "objects.git"
                    self._git(["init", "--bare", "-q", str(gd)])
                    with gitseguro.repositorio_isolado(ws / ".git") as source:
                        self._git(["fetch", "--no-tags", str(source), f"refs/heads/{branch}:refs/heads/candidate"], gd)
                    sha = self._git(["rev-parse", "refs/heads/candidate"], gd)
                    if sha != commit:
                        raise workspace.GitErro("approved commit changed")
                    self._git(["fetch", "--no-tags", url, f"refs/heads/{destino}:refs/heads/base"], gd)
                    base = self._git(["rev-parse", "refs/heads/base"], gd)
                    tree = self._git(["merge-tree", "--write-tree", base, sha], gd).splitlines()[0]
                    merged = self._git(["commit-tree", tree, "-p", base, "-p", sha, "-m", f"Merge {branch}"], gd)
                    self._git(["push", "--no-tags", url, f"{merged}:refs/heads/{destino}"], gd)
            return {"ok": True, "hash": merged, "base": destino}
        except (workspace.GitErro, OSError, ValueError, subprocess.TimeoutExpired) as e:
            msg = sanitize.mascarar(str(e), dev=True)
            if re.search(r"Authentication failed|Permission denied|could not read Username|publickey", msg, re.I):
                msg += (" | git has no usable credential for this repository: configure your git credential helper"
                        " or set publish.push_url_scheme = \"ssh\" in config.toml")
            return {"ok": False, "erro": msg}
