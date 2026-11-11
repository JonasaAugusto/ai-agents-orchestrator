"""Test doubles used by --dry-run and by the tests (no real calls to OpenAI, Telegram or root)."""
from __future__ import annotations

import json
from pathlib import Path

from .engines import LimiteInfo, Resultado


class CodexFalso:
    """Replaces CodexRunner. `respostas` is a queue of Resultado (or callables) consumed by each run; `eventos` are
    JSONL-like dicts delivered to on_event on every run (to exercise the live event log)."""

    def __init__(self, state_dir: Path | None = None, respostas: list | None = None, logado: bool = True):
        self.state_dir = state_dir
        self.respostas = list(respostas or [])
        self.chamadas: list[dict] = []
        self._logado = logado
        self.cancelado = False
        self.eventos: list[dict] = []

    def logado(self) -> bool:
        return self._logado

    def cancelar(self) -> None:
        self.cancelado = True

    def executar(self, prompt, cwd, sandbox="workspace-write", timeout=0, modelo=None, schema=None, resume=None,
                 add_dirs=(), on_event=None, esforco=None) -> Resultado:
        self.chamadas.append({"prompt": prompt, "cwd": str(cwd), "sandbox": sandbox, "modelo": modelo,
                              "schema": bool(schema), "resume": resume, "esforco": esforco})
        for ev in self.eventos:
            if on_event:
                on_event(ev)
        if self.respostas:
            r = self.respostas.pop(0)
            return r(prompt, cwd) if callable(r) else r
        return Resultado(ok=True, texto="[dry-run] no real call to Codex.", thread_id="dry-thread", segundos=0.1)


class RootFalso:
    CHAVE = b"t" * 32

    def __init__(self, cfg=None):
        self.cfg = cfg  # with cfg, push_* run the REAL daemon logic (local git), without a socket
        self.chamadas: list[tuple] = []
        self.acordar_ok = True
        self.espelho_status: dict = {}  # repo -> canned espelhar_repo answer (or an exception to raise)
        self.deploy_repos = {"web-app"}  # repos that report a registered deploy

    def espelhar_repo(self, repo):
        self.chamadas.append(("espelhar_repo", repo))
        r = self.espelho_status.get(repo, {"ok": True})
        if isinstance(r, Exception):
            raise r
        return r

    def espelhar(self, hook=None):
        self.chamadas.append(("espelhar", hook))
        return {"ok": True}

    def acordar_claude(self, tipo, tid=None, estado=None):
        """Records the text the REAL daemon would build from these fields (never free text)."""
        from . import root_daemon
        from .config import Config
        texto = root_daemon.texto_acordar(self.cfg or Config(), {"tipo": tipo, "tid": tid, "estado": estado})
        self.chamadas.append(("acordar_claude", texto))
        return {"ok": self.acordar_ok}

    def info(self, repo):
        return {"ok": True, "deploy": repo in self.deploy_repos}

    def publicar(self, repo, commit, acao, agora=None):
        self.chamadas.append(("publicar", repo, commit, acao))
        return {"ok": True, "saida": "ok"}

    def push_branch(self, repo, tid, branch):
        self.chamadas.append(("push_branch", repo, tid, branch))
        if not self.cfg:
            return {"ok": True, "hash": "0" * 40}
        from . import root_daemon as rd
        return rd.push(self.cfg, {"repo": repo, "tid": tid, "branch": branch, "modo": "branch"})

    def push_base(self, repo, tid, branch, agora=None):
        import time, uuid
        from . import root_daemon as rd
        self.chamadas.append(("push_base", repo, tid, branch))
        if not self.cfg:
            return {"ok": True, "hash": "0" * 40}
        nonce, exp = uuid.uuid4().hex, int(time.time() + 300)
        req = {"repo": repo, "tid": tid, "branch": branch, "modo": "base", "nonce": nonce, "exp": exp, "commit": branch,
               "mac": rd.assinar(self.CHAVE, repo, branch, "push-base", nonce, exp)}
        return rd.push(self.cfg, req, self.CHAVE)
