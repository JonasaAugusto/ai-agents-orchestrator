"""OPTIONAL minimal ROOT daemon (ai-agents-orchestrator-root.service). The only privileged component; it accepts a
fixed set of operations and nothing else:

  espelhar / espelhar_repo -> refresh the mirror (transcript tail, memory, bare repos)
  acordar_claude           -> EXPERIMENTAL: post a FIXED notice (built here, never agent-authored text) into the
                              interactive Claude Code session socket (undocumented local format; disabled unless
                              claude.wakeup_enabled = true)
  push                     -> push an `agent/*` branch (or, with a signed request, merge into the base) using ROOT's
                              git credential; the token never leaves root (see `push()`); only repos whose GitHub
                              slug is in publish.push_allowlist
  publicar                 -> run a deploy command from an EXACT allowlist (publish.json), only with an HMAC-signed
                              request issued after the owner pressed [Deploy] in Telegram
  info                     -> whether a repo has a deploy registered

Authentication: socket 0660 root:<service group> + SO_PEERCRED (service uid or root). No free-form shell.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import pwd
import re
import socket
import struct
import subprocess
import threading
import time
import uuid
from pathlib import Path

from . import arquivos_root, config, espelho, gitseguro
from .config import CLI, Config

MANIFESTO = Config().publish_manifest  # replaced by cfg.publish_manifest at runtime
CHAVE = Config().publish_key_file
SESSOES = Config().claude_sessions_dir
MAX_LINHA = 200_000
RX_BRANCH = re.compile(r"^agent/[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")
RX_SEGREDO = re.compile(r"(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|x-access-token:[^@\s]+@|"
                        r"(?i:password=)\S+|(?i:authorization:)[^\n]*")


def assinar(chave: bytes, repo: str, commit: str, acao: str, nonce: str, exp: int) -> str:
    return hmac.new(chave, f"{repo}|{commit}|{acao}|{nonce}|{exp}".encode(), hashlib.sha256).hexdigest()


def _auditar(cfg: Config, evento: dict) -> None:
    evento["quando"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    fd = arquivos_root.abrir_append(cfg.shared_state / "private" / "auditoria-root.jsonl")
    try:
        os.write(fd, (json.dumps(evento, ensure_ascii=False) + "\n").encode())
    finally:
        os.close(fd)


# ---------------------------------------------------------------- acordar o Claude
def sessao_claude(sessoes: Path = SESSOES, nome: str | None = None) -> dict | None:
    """Most recent live interactive session (or the one with the given name)."""
    melhor = None
    for f in sessoes.glob("*.json"):
        try:
            d = json.loads(f.read_text())
            os.kill(int(d["pid"]), 0)
        except (OSError, ValueError, KeyError):
            continue
        if d.get("kind") != "interactive" or not d.get("messagingSocketPath"):
            continue
        if nome and d.get("name") != nome:
            continue
        if melhor is None or int(d.get("updatedAt", 0)) > int(melhor.get("updatedAt", 0)):
            melhor = d
    return melhor


def montar_mensagem(sessao: dict, corpo: str, remetente: str = "codex-orchestrator", de: str = "uds:ai-agents-orchestrator") -> str:
    """EXPERIMENTAL. One JSON line in the format observed in the Claude Code client's cross-session messaging socket.
    This is NOT a documented API and may change in any Claude Code release."""
    conteudo = (f'<cross-session-message from="{de}" from-name="{remetente}">\n{corpo}\n</cross-session-message>')
    msg = {"msgV": 1, "msg_id": uuid.uuid4().hex, "type": "user",
           "message": {"role": "user", "content": conteudo}, "parent_tool_use_id": None,
           "session_id": sessao.get("sessionId"), "uuid": str(uuid.uuid4()),
           "origin": {"kind": "peer", "from": de, "name": remetente, "body": corpo}}
    return json.dumps(msg, ensure_ascii=False) + "\n"


def acordar_claude(corpo: str, nome: str | None = None, sessoes: Path = SESSOES) -> dict:
    corpo = corpo[:4000]
    s = sessao_claude(sessoes, nome)
    if not s:
        return {"ok": False, "erro": "no interactive Claude Code session found"}
    caminho = s["messagingSocketPath"]
    try:
        st = os.lstat(caminho)
        if st.st_uid != os.geteuid():
            return {"ok": False, "erro": "socket owned by another user"}
        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        c.settimeout(5)
        c.connect(caminho)
        c.sendall(montar_mensagem(s, corpo).encode())
        c.shutdown(socket.SHUT_WR)
        c.close()
    except OSError as e:
        return {"ok": False, "erro": type(e).__name__}
    return {"ok": True, "sessao": s.get("name") or s.get("sessionId")}


ESTADOS_AVISO = {"pendente": "pending", "em_andamento": "in progress", "em_revisao": "in review",
                 "concluida": "completed", "bloqueada": "blocked", "cancelada": "cancelled"}


def texto_acordar(cfg: Config, req: dict) -> str | None:
    """The ONLY texts the daemon ever posts into the Claude session. Built here from validated fields; nothing written
    by an agent (reports, summaries, free text from the socket) is ever forwarded. None = invalid request."""
    tipo = req.get("tipo")
    if tipo == "fim":
        try:
            tid = int(req.get("tid"))
        except (TypeError, ValueError):
            return None
        estado = ESTADOS_AVISO.get(str(req.get("estado")))
        if tid <= 0 or not estado:
            return None
        return f"Task #{tid} finished with status {estado}. Run `{CLI} result {tid}`."
    if tipo == "volta":
        return ("Claude's usage limit should be renewed. Codex kept developing meanwhile. "
                f"Read {cfg.para_claude}/HANDOFF-PARA-CLAUDE.md, run `{CLI} claude-resumed` "
                "and split the work with Codex (it can keep working on tasks in parallel).")
    return None


# ---------------------------------------------------------------- publicar
def carregar_manifesto(caminho: Path = MANIFESTO) -> dict:
    try:
        return json.loads(caminho.read_text())
    except (OSError, ValueError):
        return {}


def _script_ok(p: str, permitidos: list[str]) -> bool:
    if p not in permitidos:
        return False
    try:
        st = os.stat(p)
    except OSError:
        return False
    return st.st_uid == 0 and not (st.st_mode & 0o022)


def resolver_acao(man: dict, repo: str, acao: str) -> tuple[list[str], int] | None:
    """Translates the action name into an exact-allowlist argv. None = denied."""
    if acao == "deploy":
        d = (man.get("repos", {}).get(repo) or {}).get("deploy")
        return (list(d["argv"]), int(d.get("timeout", 900))) if d else None
    if acao.startswith("script:"):
        p = acao[7:]
        if _script_ok(p, man.get("scripts", [])):
            return (["/bin/bash", p], 1800)
        return None
    if acao.startswith("extra:"):
        e = man.get("acoes_extras", {}).get(acao[6:])
        return (list(e["argv"]), int(e.get("timeout", 300))) if e else None
    return None


_usados: dict[str, float] = {}
_lock = threading.Lock()


def publicar(cfg: Config, req: dict, chave: bytes, manifesto: dict | None = None, agora: float | None = None) -> dict:
    agora = agora if agora is not None else time.time()
    try:
        repo, commit, acao = str(req["repo"]), str(req["commit"]), str(req["acao"])
        nonce, exp, mac = str(req["nonce"]), int(req["exp"]), str(req["mac"])
    except (KeyError, ValueError, TypeError):
        return {"ok": False, "erro": "malformed request"}
    if not chave or not hmac.compare_digest(mac, assinar(chave, repo, commit, acao, nonce, exp)):
        _auditar(cfg, {"op": "publicar", "repo": repo, "acao": acao, "resultado": "invalid signature"})
        return {"ok": False, "erro": "invalid signature"}
    if exp < agora:
        return {"ok": False, "erro": "request expired"}
    with _lock:
        for k in [k for k, v in _usados.items() if v < agora]:
            _usados.pop(k)
        if nonce in _usados:
            return {"ok": False, "erro": "request already used"}
        _usados[nonce] = exp + 60
    man = manifesto if manifesto is not None else carregar_manifesto(cfg.publish_manifest)
    if repo not in cfg.repos:
        return {"ok": False, "erro": "repo not allowed"}
    r = resolver_acao(man, repo, acao)
    if not r:
        _auditar(cfg, {"op": "publicar", "repo": repo, "acao": acao, "resultado": "action not in allowlist"})
        return {"ok": False, "erro": "action not in allowlist"}
    argv, tmo = r
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=tmo, cwd="/",
                           env={"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "HOME": "/root", "LC_ALL": "C.UTF-8"})
        ok, tail = p.returncode == 0, (p.stdout + p.stderr)[-600:]
    except subprocess.TimeoutExpired:
        ok, tail = False, "timeout"
    _auditar(cfg, {"op": "publicar", "repo": repo, "commit": commit, "acao": acao, "argv0": argv[0],
                   "resultado": "ok" if ok else "failed"})
    return {"ok": ok, "saida": tail}


# ---------------------------------------------------------------- push (root's git credential)
def _limpar(texto: str) -> str:
    """Removes anything that looks like a token before logging/answering."""
    return RX_SEGREDO.sub("[removed]", texto or "")


def _env_push(cfg: Config) -> dict:
    # HOME=/root: root's global git credential helper provides the credential (only inside git; never read here).
    return {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/root", "LC_ALL": "C.UTF-8", "GIT_TERMINAL_PROMPT": "0",
            "GIT_AUTHOR_NAME": cfg.autor_nome, "GIT_AUTHOR_EMAIL": cfg.autor_email,
            "GIT_COMMITTER_NAME": cfg.autor_nome, "GIT_COMMITTER_EMAIL": cfg.autor_email}


def _git(cfg: Config, args: list[str], cwd: str | Path | None = None, timeout: int = 180) -> subprocess.CompletedProcess:
    """git in ROOT's own bare clone (root_state, 0700): root's global config applies (credential helper)."""
    return gitseguro.git_confiavel(args, cwd=cwd, env=_env_push(cfg), timeout=timeout)


def _git_do_workspace(cfg: Config, args: list[str], cwd: str | Path | None = None, timeout: int = 180) -> subprocess.CompletedProcess:
    """git that reads the service-owned workspace (fetch from it): no system/global config, every exec-capable key
    overridden, local transport only. Nothing from the workspace .git/config can run code as root."""
    from contextlib import ExitStack
    with ExitStack() as stack:
        safe = list(args)
        for i, value in enumerate(safe):
            p = Path(value)
            if p.is_dir() and (p / "HEAD").is_file() and p != Path(cwd or "/"):
                safe[i] = str(stack.enter_context(gitseguro.repositorio_isolado(p)))
        return gitseguro.git_endurecido(safe, cwd=cwd, env=_env_push(cfg), timeout=timeout)


def url_do_repo(cfg: Config, repo: str) -> str | None:
    """URL decided ONLY by the daemon: GitHub https for a repo whose slug is in publish.push_allowlist."""
    spec = cfg.repos.get(repo)
    if not spec:
        return None
    if cfg.permitir_url_local and spec.get("github_url"):
        return str(spec["github_url"])
    gh = spec.get("github")
    return f"https://github.com/{gh}.git" if gh and gh in set(cfg.push_allowlist) else None


def assinatura_ok(chave: bytes, req: dict, acao: str, agora: float) -> str | None:
    """None = ok; otherwise the error message. Checks HMAC, expiry and nonce (single use)."""
    try:
        repo, commit, nonce, exp, mac = (str(req["repo"]), str(req["commit"]), str(req["nonce"]), int(req["exp"]), str(req["mac"]))
    except (KeyError, ValueError, TypeError):
        return "malformed request"
    if not chave or not hmac.compare_digest(mac, assinar(chave, repo, commit, acao, nonce, exp)):
        return "invalid signature"
    if exp < agora:
        return "request expired"
    with _lock:
        for k in [k for k, v in _usados.items() if v < agora]:
            _usados.pop(k)
        if nonce in _usados:
            return "request already used"
        _usados[nonce] = exp + 60
    return None


def acao_push(branch: str, destino: str, tid: int) -> str:
    return "push-base:" + json.dumps([branch, destino, tid], separators=(",", ":"))


def push(cfg: Config, req: dict, chave: bytes = b"", agora: float | None = None) -> dict:
    """`push` operation. req: repo (key in `repos`), tid (int, agent workspace), branch (`agent/...`),
    modo ("branch" | "base"). "base" signs the exact commit, repo, branch, tid and destination and merges
    the branch --no-ff on top of the remote's CURRENT base (fast-forward required). Never force, tag or delete."""
    agora = agora if agora is not None else time.time()
    modo = str(req.get("modo", "branch"))
    repo, branch = str(req.get("repo", "")), str(req.get("branch", ""))

    def negar(motivo: str) -> dict:
        _auditar(cfg, {"op": "push", "repo": repo, "ref": branch[:90], "modo": modo, "resultado": "denied: " + motivo})
        return {"ok": False, "erro": motivo}

    if modo not in ("branch", "base"):
        return negar("invalid mode")
    url = url_do_repo(cfg, repo)
    if not url:
        return negar("repo not allowed")
    if not RX_BRANCH.match(branch) or ".." in branch or branch.endswith((".lock", ".", "/")):
        return negar("branch outside the allowlist (agent/* only)")
    try:
        tid = int(req["tid"])
    except (KeyError, ValueError, TypeError):
        return negar("invalid tid")
    ws = (cfg.workspaces / repo / str(tid)).resolve()
    if ws.parent != (cfg.workspaces / repo).resolve() or not (ws / ".git").exists():
        return negar("invalid workspace")
    base = str(cfg.repos[repo].get("branch_base", "main"))
    if not re.match(r"^[A-Za-z0-9._-]+$", base):
        return negar("invalid base")

    if modo == "base":
        commit = str(req.get("commit", ""))
        destino_aprovado = str(req.get("destino", ""))
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit):
            return negar("invalid approved commit")
        erro = assinatura_ok(chave, req, acao_push(branch, destino_aprovado, tid), agora)
        if erro:
            return negar(erro)
        if destino_aprovado != base:
            return negar("approved destination changed")

    root_git = cfg.root_state / f"{repo}.git"
    try:
        os.close(arquivos_root.abrir_dir(cfg.root_state, modo=0o700))
        if not root_git.exists():
            _git(cfg, ["init", "--bare", "-q", str(root_git)])
        # 1) fetch the workspace commit into root's clone (objects only; nothing from the workspace .git/config applies)
        inc = f"refs/incoming/{tid}"
        f = _git_do_workspace(cfg, ["fetch", "-q", "--no-tags", "--no-write-fetch-head", str(ws / ".git"),
                                    f"+refs/heads/{branch}:{inc}"], root_git)
        if f.returncode != 0:
            return negar("branch not found in the workspace")
        sha = _git(cfg, ["rev-parse", inc], root_git).stdout.strip()
        if modo == "base" and sha != commit:
            return negar("approved commit changed")
        destino, alvo = f"refs/heads/{branch}", sha
        if modo == "base":
            # 2) current base from the remote (authenticated by root); merge without worktree; fast-forward required
            r = _git(cfg, ["fetch", "-q", "--no-tags", "--no-write-fetch-head", url, f"+refs/heads/{base}:refs/remotes/github/{base}"], root_git)
            if r.returncode != 0:
                _auditar(cfg, {"op": "push", "repo": repo, "ref": base, "modo": modo, "resultado": "failed: fetch", "erro": _limpar(r.stderr)[-150:]})
                return {"ok": False, "erro": "could not read the remote base: " + _limpar(r.stderr).strip()[-150:]}
            topo = _git(cfg, ["rev-parse", f"refs/remotes/github/{base}"], root_git).stdout.strip()
            if _git(cfg, ["merge-base", "--is-ancestor", topo, sha], root_git).returncode == 0:
                alvo_arv = _git(cfg, ["rev-parse", f"{sha}^{{tree}}"], root_git).stdout.strip()
                alvo = _git(cfg, ["commit-tree", alvo_arv, "-p", topo, "-p", sha, "-m", f"Merge {branch}"], root_git).stdout.strip()
            else:
                m = _git(cfg, ["merge-tree", "--write-tree", topo, sha], root_git)
                if m.returncode != 0:
                    _auditar(cfg, {"op": "push", "repo": repo, "ref": base, "modo": modo, "resultado": "conflict"})
                    return {"ok": False, "erro": "merge conflict: " + _limpar(m.stdout + m.stderr).strip()[-200:]}
                alvo = _git(cfg, ["commit-tree", m.stdout.split()[0], "-p", topo, "-p", sha, "-m", f"Merge {branch}"], root_git).stdout.strip()
            if not alvo or _git(cfg, ["merge-base", "--is-ancestor", topo, alvo], root_git).returncode != 0:
                return negar("not a fast-forward of the remote base")
            destino = f"refs/heads/{base}"
        # 3) push: fixed URL, refspec without '+', no tags, no delete; credential from root's git helper
        p = _git(cfg, ["push", "--porcelain", "--no-tags", url, f"{alvo}:{destino}"], root_git, timeout=180)
        ok = p.returncode == 0
        _auditar(cfg, {"op": "push", "repo": repo, "ref": destino, "modo": modo, "commit": alvo,
                       "resultado": "ok" if ok else "failed", **({} if ok else {"erro": _limpar(p.stderr)[-150:]})})
        if not ok:
            return {"ok": False, "erro": "push rejected: " + _limpar(p.stderr).strip()[-200:]}
        return {"ok": True, "hash": alvo, "ref": destino, "base": base}
    except subprocess.TimeoutExpired:
        return negar("timeout")


# ---------------------------------------------------------------- server
def processar(cfg: Config, req: dict, peer_uid: int, uid_agente: int | None, chave: bytes) -> dict:
    if peer_uid not in (0, uid_agente):
        return {"ok": False, "erro": "uid not authorized"}
    op = req.get("op")
    if op == "espelhar":
        hook = req.get("hook") if isinstance(req.get("hook"), dict) else None
        return espelho.espelhar(cfg, hook_payload=hook, com_repos=bool(req.get("repos", True)),
                                cauda_msgs=int(req.get("cauda_msgs", 60)))
    if op == "espelhar_repo":
        nome = str(req.get("repo", ""))
        if nome not in cfg.repos:
            return {"ok": False, "erro": "repo not allowed"}
        st = espelho.atualizar_repos(cfg, [nome])
        return {"ok": bool(st.get(nome, {}).get("ok")), "status": st.get(nome)}
    if op == "acordar_claude":
        if not cfg.claude_wakeup:
            return {"ok": False, "erro": "claude wake-up disabled (claude.wakeup_enabled = false)"}
        corpo = texto_acordar(cfg, req)
        if corpo is None:
            return {"ok": False, "erro": "invalid wake-up request"}
        return acordar_claude(corpo, nome=cfg.claude_session_name or None, sessoes=cfg.claude_sessions_dir)
    if op == "publicar":
        return publicar(cfg, req, chave)
    if op == "push":
        return push(cfg, req, chave)
    if op == "info":
        d = (carregar_manifesto(cfg.publish_manifest).get("repos", {}).get(str(req.get("repo"))) or {}).get("deploy")
        return {"ok": True, "deploy": bool(d)}
    return {"ok": False, "erro": "unknown operation"}


def servir(cfg: Config, caminho: str | None = None, checar_codigo: bool = True) -> None:  # pragma: no cover
    caminho = caminho or str(cfg.root_socket)
    if os.geteuid() == 0 and checar_codigo:
        ruim = config.codigo_inseguro()
        if ruim:  # the agent may edit this project only in a workspace; what runs as root comes from a root-owned copy
            raise SystemExit("refused: root daemon code is not root-owned / writable only by root: " + ", ".join(ruim))
    from . import seguranca
    seguranca.proteger_processo()
    try:
        uid_agente = pwd.getpwnam(cfg.service_user).pw_uid
    except KeyError:
        uid_agente = None
    try:
        chave = bytes.fromhex(cfg.publish_key_file.read_text().strip())
    except (OSError, ValueError):
        chave = b""
    Path(caminho).parent.mkdir(parents=True, exist_ok=True)
    tmp = caminho + ".tmp"
    for p in (tmp, caminho):
        if os.path.exists(p):
            os.unlink(p)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(tmp)  # bind + listen + permissions on a temporary name, then publish it atomically
    srv.listen(8)
    try:
        import grp
        os.chown(tmp, 0, grp.getgrnam(cfg.service_user).gr_gid)
    except (KeyError, OSError):
        pass
    os.chmod(tmp, 0o660)
    os.rename(tmp, caminho)
    while True:
        c, _ = srv.accept()
        try:
            c.settimeout(30)
            peer = struct.unpack("3i", c.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
            dados = b""
            while not dados.endswith(b"\n") and len(dados) < MAX_LINHA:
                parte = c.recv(65536)
                if not parte:
                    break
                dados += parte
            try:
                req = json.loads(dados.decode())
                resp = processar(cfg, req, peer[1], uid_agente, chave)
            except ValueError:
                resp = {"ok": False, "erro": "invalid json"}
            except Exception as e:  # never brings the daemon down
                resp = {"ok": False, "erro": type(e).__name__}
            c.sendall((json.dumps(resp, ensure_ascii=False, default=str) + "\n").encode())
        except OSError:
            pass
        finally:
            c.close()


class RootClient:
    """Client used by the service (unprivileged user)."""

    def __init__(self, caminho: str | None = None, chave_hex: str = ""):
        self.caminho = caminho or str(Config().root_socket)
        self.chave = bytes.fromhex(chave_hex) if chave_hex else b""

    def chamar(self, req: dict, timeout: float = 600) -> dict:
        try:
            c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            c.settimeout(timeout)
            c.connect(self.caminho)
            c.sendall((json.dumps(req) + "\n").encode())
            dados = b""
            while not dados.endswith(b"\n"):
                p = c.recv(65536)
                if not p:
                    break
                dados += p
            c.close()
            return json.loads(dados.decode())
        except (OSError, ValueError) as e:
            return {"ok": False, "erro": f"root daemon unavailable ({type(e).__name__})"}

    def espelhar(self, hook: dict | None = None) -> dict:
        return self.chamar({"op": "espelhar", "hook": hook})

    def espelhar_repo(self, repo: str) -> dict:
        """Refreshes only one repo's bare mirror (current GitHub state) before creating a workspace."""
        return self.chamar({"op": "espelhar_repo", "repo": repo}, 330)

    def acordar_claude(self, tipo: str, tid: int | None = None, estado: str | None = None) -> dict:
        """tipo "fim" (task tid ended in `estado`) or "volta" (usage limit renewed). The daemon writes the text."""
        return self.chamar({"op": "acordar_claude", "tipo": tipo, "tid": tid, "estado": estado}, 30)

    def info(self, repo: str) -> dict:
        return self.chamar({"op": "info", "repo": repo}, 10)

    def push_branch(self, repo: str, tid: int, branch: str) -> dict:
        return self.chamar({"op": "push", "repo": repo, "tid": tid, "branch": branch, "modo": "branch"}, 200)

    def push_base(self, repo: str, tid: int, branch: str, *, commit: str, destino: str, agora: float | None = None) -> dict:
        """Merge the branch into the base + push; signed (HMAC) because only the [Publish] button may trigger it."""
        agora = agora if agora is not None else time.time()
        nonce, exp = uuid.uuid4().hex, int(agora + 300)
        return self.chamar({"op": "push", "repo": repo, "tid": tid, "branch": branch, "modo": "base", "nonce": nonce, "exp": exp,
                            "commit": commit, "destino": destino,
                            "mac": assinar(self.chave, repo, commit, acao_push(branch, destino, tid), nonce, exp)}, 200)

    def publicar(self, repo: str, commit: str, acao: str, agora: float | None = None) -> dict:
        agora = agora if agora is not None else time.time()
        nonce, exp = uuid.uuid4().hex, int(agora + 300)
        return self.chamar({"op": "publicar", "repo": repo, "commit": commit, "acao": acao, "nonce": nonce, "exp": exp,
                            "mac": assinar(self.chave, repo, commit, acao, nonce, exp)}, 1900)
