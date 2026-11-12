"""Agent git: isolated workspaces, branch, commit, patch, HANDOFF.md, guarded push/merge and audit log."""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
import unicodedata
from contextlib import ExitStack
from pathlib import Path

from .config import Config
from . import arquivos_root, gitseguro

RX_PERIGO = re.compile(r"(?i)^(--force(-with-lease)?|-f|--delete|-d|--mirror|--tags|--prune|\+.*|:.*)(=.*)?$")


class GitErro(Exception):
    pass


# Workspace git runs OUTSIDE Codex's sandbox while Codex can write the workspace's .git/config: never honour hooks,
# fsmonitor, credential helpers, proxies or filters from it. Git sees a private, fixed configuration.
ENDURECER_WS = ("-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-c", "credential.helper=",
                "-c", "core.gitProxy=", "-c", "protocol.ext.allow=never", "-c", "log.showSignature=false")
# Keys a workspace .git/config may contain. Anything else (filter/diff/merge drivers, include*, core.* commands,
# url.*.insteadOf, extensions.worktreeConfig...) is reported as tampering. This diagnostic is not a security boundary.
RX_CONFIG_OK = re.compile(
    r"^(core\.(repositoryformatversion|filemode|bare|logallrefupdates|ignorecase|precomposeunicode|symlinks|autocrlf|"
    r"eol|safecrlf)|extensions\.(objectformat|refstorage)|remote\.(origin|github)\.(url|fetch)|"
    r"branch\..+\.(remote|merge|rebase)|user\.(name|email)|commit\.gpgsign|gc\.auto|pull\.(rebase|ff)|"
    r"init\.defaultbranch|index\.(version|threads)|color\.[a-z.]+|advice\.[a-z]+)$")


def verificar_config_ws(cfg: Config, ws: Path) -> None:
    """Raises GitErro if the workspace's local config holds a key outside the allowlist (planted by the agent)."""
    # Diagnostic only: Git commands below never use this file, even if it changes now.
    r = git(cfg, ["config", "--file", str(Path(ws) / ".git" / "config"), "--no-includes", "--name-only", "--list"], ws, check=False)
    ruins = sorted({k for k in r.stdout.splitlines() if k and not RX_CONFIG_OK.match(k.lower())})
    if ruins:
        raise GitErro("unsafe workspace .git/config (keys that can run code outside the sandbox): " + ", ".join(ruins[:6]))


def slug(texto: str, n: int = 30) -> str:
    t = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode().lower()
    return (re.sub(r"[^a-z0-9]+", "-", t).strip("-")[:n].strip("-")) or "task"


def env_git(cfg: Config, repo: str | None = None) -> dict:
    e = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(cfg.state_dir), "LC_ALL": "C.UTF-8", "GIT_TERMINAL_PROMPT": "0", "GIT_NO_REPLACE_OBJECTS": "1",
         "GIT_AUTHOR_NAME": cfg.autor_nome, "GIT_AUTHOR_EMAIL": cfg.autor_email,
         "GIT_COMMITTER_NAME": cfg.autor_nome, "GIT_COMMITTER_EMAIL": cfg.autor_email}
    if cfg.mode == "lite":
        e["PATH"] = os.environ.get("PATH", e["PATH"])
    if repo:
        chave = cfg.state_dir / ".ssh" / f"deploy-{repo}"
        e["GIT_SSH_COMMAND"] = (f"ssh -i {chave} -o IdentitiesOnly=yes -o BatchMode=yes "
                                f"-o UserKnownHostsFile={cfg.state_dir}/.ssh/known_hosts -o StrictHostKeyChecking=yes")
    e.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_SYSTEM="/dev/null", GIT_CONFIG_GLOBAL="/dev/null")
    return e


def git(cfg: Config, args: list[str], cwd: Path | str | None = None, repo: str | None = None,
        check: bool = True, timeout: int = 300) -> subprocess.CompletedProcess:
    if any(RX_PERIGO.match(a) for a in args if a[:1] in "-+:"):
        raise GitErro(f"forbidden git argument: {' '.join(args)[:80]}")
    # Engines may write their workspace and /tmp. Keep service Git config outside both.
    # When called as root (CLI), its temporary parent must also be outside service control.
    if os.geteuid() == 0 and cfg.mode != "lite":
        temporarios = cfg.shared_state / "private" / "git"
        os.close(arquivos_root.abrir_dir(temporarios, modo=0o700))
    else:
        temporarios = cfg.spool / "git"
        temporarios.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(temporarios, 0o700)
    with ExitStack() as stack:
        private_args = list(args)
        prefix = []
        directory = Path(cwd).absolute() if cwd else None
        if directory and (directory / ".git").is_dir():
            spec_name = repo or directory.parent.name
            spec = cfg.repos.get(spec_name, {})
            remotes = {"origin": cfg.repos_dir / f"{spec_name}.git"}
            url = spec.get("github_url") or (f"git@github.com:{spec['github']}.git" if spec.get("github") else None)
            if url:
                remotes["github"] = url
            private = stack.enter_context(gitseguro.repositorio_isolado(directory / ".git", directory,
                                                                       remotes, escrever=True, temporarios=temporarios))
            prefix = ["--git-dir", str(private), "--work-tree", str(directory)]
        elif directory and (directory / "HEAD").is_file() and (directory / "objects").is_dir():
            private = stack.enter_context(gitseguro.repositorio_isolado(directory, escrever=True, temporarios=temporarios))
            prefix = ["--git-dir", str(private)]
        elif directory:
            raise GitErro("unsupported repository layout; refusing local configuration")
        if prefix and (directory / ".git").is_dir() and private_args[0] == "config" and "--file" not in private_args:
            private_args[1:1] = ["--file", str(directory / ".git" / "config")]
        # A local clone/fetch must not let upload-pack load the source's config either.
        if private_args and private_args[0] in ("clone", "fetch"):
            for i, value in enumerate(private_args[1:], 1):
                source = Path(value)
                if not source.is_absolute() and directory:
                    source = directory / source
                if source.is_dir() and ((source / "HEAD").is_file() or (source / ".git" / "HEAD").is_file()):
                    gd = source / ".git" if (source / ".git").is_dir() else source
                    private_args[i] = str(stack.enter_context(gitseguro.repositorio_isolado(gd, temporarios=temporarios)))
                    break
        if private_args[0] == "clone":
            private_args.insert(1, "--no-local")
        r = subprocess.run(["git", *ENDURECER_WS, *prefix, *private_args], cwd=cwd or "/", env=env_git(cfg, repo),
                           capture_output=True, text=True, timeout=timeout)
    if check and r.returncode != 0:
        raise GitErro((r.stderr or r.stdout).strip()[-300:])
    return r


def auditar(cfg: Config, acao: str, repo: str, ref: str = "", hash_: str = "", resultado: str = "ok", extra: str = "") -> None:
    """Append-only log (O_APPEND). Optionally protect it with `chattr +a`."""
    linha = json.dumps({"quando": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "acao": acao, "repo": repo, "ref": ref,
                        "hash": hash_, "resultado": resultado, "extra": extra[:200]}, ensure_ascii=False) + "\n"
    if os.geteuid() == 0 and cfg.mode != "lite":
        fd = arquivos_root.abrir_append(cfg.shared_state / "private" / "auditoria-workspace.jsonl")
    else:
        cfg.logs.mkdir(parents=True, exist_ok=True)
        fd = os.open(cfg.logs / "auditoria.jsonl", os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, linha.encode())
    finally:
        os.close(fd)


def repo_permitido(cfg: Config, repo: str) -> bool:
    return repo in cfg.repos


def sem_remoto(cfg: Config, repo: str) -> bool:
    """Local repo without GitHub: nothing goes to a remote; delivery is the workspace branch + patch in resultados/."""
    return bool(cfg.repos.get(repo, {}).get("local"))


def base_do_repo(cfg: Config, repo: str) -> str:
    return cfg.repos.get(repo, {}).get("branch_base", "main")


def preparar_workspace(cfg: Config, repo: str, tid: int, titulo: str, base_ref: str | None = None,
                       branch: str | None = None) -> tuple[Path, str, str]:
    """Local clone (no hardlinks) of the bare mirror + work branch. Never touches the original checkout."""
    if not repo_permitido(cfg, repo):
        raise GitErro(f"repo not allowed: {repo}")
    bare = cfg.repos_dir / f"{repo}.git"
    if not bare.exists():
        raise GitErro(f"repo mirror missing: {repo} (run `aao mirror`)")
    base = base_ref or base_do_repo(cfg, repo)
    ws = cfg.workspaces / repo / str(tid)
    branch = branch or f"agent/{tid}-{slug(titulo)}"
    if not ws.exists():
        ws.parent.mkdir(parents=True, exist_ok=True)
        git(cfg, ["clone", "--no-hardlinks", "--quiet", str(bare), str(ws)])
        git(cfg, ["config", "remote.origin.url", str(bare)], ws)
        git(cfg, ["config", "user.name", cfg.autor_nome], ws)
        git(cfg, ["config", "user.email", cfg.autor_email], ws)
        git(cfg, ["config", "commit.gpgsign", "false"], ws)
        spec = cfg.repos[repo]
        url = spec.get("github_url") or (f"git@github.com:{spec['github']}.git" if spec.get("github") else None)
        if url:
            git(cfg, ["config", "remote.github.url", url], ws)
            git(cfg, ["config", "remote.github.fetch", "refs/heads/*:refs/remotes/github/*"], ws)
        git(cfg, ["checkout", "-q", "-b", branch, f"origin/{base}"], ws)
    instalar_agents_md(cfg, ws, repo)
    return ws, branch, base


def instalar_agents_md(cfg: Config, ws: Path, repo: str, templates: Path | None = None) -> bool:
    """Per-repo instructions: copies `<templates_dir>/AGENTS.<repo>.md` to `AGENTS.md` at the workspace root (Codex reads
    the AGENTS.md of its cwd). It stays OUT of the commit (.git/info/exclude) and never overwrites an AGENTS.md the
    repo itself versions. Includes the live base rules, even without a per-repo template. True if written."""
    origem = (templates or cfg.templates_dir) / f"AGENTS.{repo}.md"
    if git(cfg, ["ls-files", "--error-unmatch", "--", "AGENTS.md"], ws, check=False).returncode == 0:
        return False
    texto = origem.read_text(encoding="utf-8") if origem.is_file() else ""
    try:
        base = cfg.rules_file.read_text(encoding="utf-8")
    except FileNotFoundError:
        base = ""
    except OSError as e:
        raise GitErro("cannot read the configured base rules") from e
    if base:
        texto = base.rstrip() + ("\n\n" + texto if texto else "\n")
    if not texto:
        return False
    destino = ws / "AGENTS.md"
    if destino.is_symlink():
        destino.unlink()  # never follow an agent-controlled link while copying service rules
    if not destino.is_file() or destino.read_text(encoding="utf-8") != texto:
        destino.write_text(texto, encoding="utf-8")
    excl = ws / ".git" / "info" / "exclude"
    atual = excl.read_text(encoding="utf-8") if excl.is_file() else ""
    if "/AGENTS.md" not in atual.splitlines():
        excl.parent.mkdir(parents=True, exist_ok=True)
        excl.write_text(atual + ("" if not atual or atual.endswith("\n") else "\n") + "/AGENTS.md\n", encoding="utf-8")
    return True


NOTAS_SOLTAS = ("CONTEXTO.md", "HANDOFF.md", "NOTAS.md", "BRIEFING.md")
REMOCAO_MAX_PCT = 0.40  # a .md losing more than this share of lines, or more than REMOCAO_MAX_LINHAS, needs review
REMOCAO_MAX_LINHAS = 30
RX_ARQ_TESTE = re.compile(r"(?i)(^|/)(tests?|__tests__|spec)/|(^|/)test_[^/]*$|_test\.[a-z]+$|\.(test|spec)\.[a-z]+$")
RX_TEMPO = re.compile(r"\bsleep\(|\btime\.time\(\)|\bperf_counter")


def _excluir_notas(cfg: Config, ws: Path, base: str, out: Path) -> list[str]:
    """Removes stray notes (CONTEXTO.md etc.) in ANY directory that do not exist in the base from the commit. Keeps a copy in resultados."""
    excluidos = []
    alvo = {n.upper() for n in NOTAS_SOLTAS}
    for caminho in sorted(git(cfg, ["ls-files", "--cached", "-z"], ws, check=False).stdout.split("\0")):
        if not caminho or Path(caminho).name.upper() not in alvo:
            continue
        if git(cfg, ["cat-file", "-e", f"origin/{base}:{caminho}"], ws, check=False).returncode == 0:
            continue  # the repo already versions this file: leave it alone
        git(cfg, ["rm", "-q", "--cached", "--ignore-unmatch", "--", caminho], ws, check=False)
        dest = out / "excluidos"
        dest.mkdir(parents=True, exist_ok=True)
        (ws / caminho).replace(dest / caminho.replace("/", "__"))
        excluidos.append(caminho)
    return excluidos


def avisos_de_entrega(cfg: Config, ws: Path, base: str) -> dict:
    """Delivery gate over the base...HEAD diff: large deletions in documents (REVIEW DELETIONS) and time-dependent
    tests. Does not block: returns text for the summary. {"remocoes": [...], "tempo": [...], "avisos": [...]}"""
    remocoes, tempo, avisos = [], [], []
    num = git(cfg, ["diff", "--numstat", "-z", f"origin/{base}...HEAD"], ws, check=False).stdout
    # with -z each record is "add\tdel\tpath\0" (renames use 3 fields; treated as a new path)
    for reg in [r for r in num.split("\0") if r.strip()]:
        partes = reg.split("\t")
        if len(partes) < 3 or not partes[0].isdigit() or not partes[1].isdigit():
            continue
        removidas, caminho = int(partes[1]), partes[2]
        if not caminho.lower().endswith(".md") or not removidas:
            continue
        antes = git(cfg, ["show", f"origin/{base}:{caminho}"], ws, check=False)
        total = len(antes.stdout.splitlines()) if antes.returncode == 0 else 0
        if removidas > REMOCAO_MAX_LINHAS or (total and removidas / total > REMOCAO_MAX_PCT):
            remocoes.append(f"{caminho}: -{removidas} of {total} lines" + (f" ({removidas * 100 // total}%)" if total else ""))
    if remocoes:
        avisos.append("REVIEW DELETIONS: document(s) with large deletions (> 40% of lines or > 30 lines): "
                      + "; ".join(remocoes[:6]) + ". Confirm nothing still valid was lost before publishing.")
    atual = ""
    for linha in git(cfg, ["diff", "-U0", "--no-color", f"origin/{base}...HEAD"], ws, check=False).stdout.splitlines():
        if linha.startswith("+++ "):
            atual = linha[6:] if linha.startswith("+++ b/") else ""
        elif linha.startswith("+") and atual and RX_ARQ_TESTE.search(atual) and RX_TEMPO.search(linha[1:]):
            if atual not in tempo:
                tempo.append(atual)
    if tempo:
        avisos.append("Warning: test(s) with sleep/time.time()/perf_counter added (time-dependent, may be flaky): "
                      + ", ".join(tempo[:6]) + ".")
    return {"remocoes": remocoes, "tempo": tempo, "avisos": avisos}


def finalizar(cfg: Config, ws: Path, repo: str, tid: int, titulo: str, base: str, narrativa: str,
              estado: str = "ok", extras_resumo: str = "") -> dict:
    """Commit (if anything changed), patches, file summary and HANDOFF.md in resultados/<id>/."""
    out = cfg.resultados / str(tid)
    out.mkdir(parents=True, exist_ok=True)
    verificar_config_ws(cfg, ws)
    branch = git(cfg, ["rev-parse", "--abbrev-ref", "HEAD"], ws).stdout.strip()
    sujo = git(cfg, ["status", "--porcelain"], ws).stdout.strip()
    excluidos: list[str] = []
    if sujo:
        git(cfg, ["add", "-A"], ws)
        excluidos = _excluir_notas(cfg, ws, base, out)
    if sujo and git(cfg, ["diff", "--cached", "--quiet"], ws, check=False).returncode != 0:
        git(cfg, ["commit", "-q", "-m", (titulo.lstrip("-+: ")[:70] or "Changes by the backup agent")], ws)
    arquivos = [l for l in git(cfg, ["diff", "--name-only", f"origin/{base}...HEAD"], ws, check=False).stdout.splitlines() if l]
    commits = git(cfg, ["log", "--oneline", f"origin/{base}..HEAD"], ws, check=False).stdout.strip()
    stat = git(cfg, ["diff", "--stat", f"origin/{base}...HEAD"], ws, check=False).stdout.strip()
    pdir = out / "patches"
    pdir.mkdir(exist_ok=True)
    for velho in pdir.glob("*.patch"):
        velho.unlink()
    patch = None
    if commits:
        git(cfg, ["format-patch", "-q", f"origin/{base}..HEAD", "-o", str(pdir)], ws, check=False)
        ps = sorted(pdir.glob("*.patch"))
        patch = str(pdir) if ps else None
    if excluidos:
        narrativa = (narrativa or "") + ("\n\nWarning: note files kept out of the commit (they do not belong to the repo): "
                                         + ", ".join(excluidos) + f". Copy in {out / 'excluidos'}.")
    gate = avisos_de_entrega(cfg, ws, base)
    if gate["avisos"]:
        narrativa = (narrativa or "") + "\n\n" + "\n".join(gate["avisos"])
    if extras_resumo:
        narrativa = (narrativa or "") + "\n\n" + extras_resumo
    ultimos = git(cfg, ["log", "-5", "--oneline"], ws, check=False).stdout.strip()
    (out / "resumo.txt").write_text(narrativa, encoding="utf-8")
    handoff = f"""# HANDOFF for task #{tid}: {titulo}

- Estado (state): {estado}
- Repository: {repo}
- Branch: `{branch}` (base `{base}`), workspace: `{ws}`
- Patches: {patch or '(no changes)'}
- Changed files ({len(arquivos)}): {', '.join(arquivos[:30]) or '-'}

## Latest commits
```
{ultimos}
```

## Changes (diffstat)
```
{stat or '(none)'}
```

## Codex report
{narrativa or '(no report)'}

## How to pick it up (Claude)
```
git -C {ws} log --oneline {base}..{branch}
git -C {ws} diff {base}...{branch}
git -C {ws} format-patch {base}..{branch}   # or apply the patches in {pdir}
```
Nothing was published. Merge and push only via the owner's [Publish] button or by Claude with explicit approval.
"""
    (out / "HANDOFF.md").write_text(handoff, encoding="utf-8")
    return {"branch": branch, "arquivos": arquivos, "commits": commits, "patch": patch, "diffstat": stat,
            "handoff": str(out / "HANDOFF.md"), "excluidos": excluidos, "narrativa": narrativa,
            "remocoes": gate["remocoes"], "tempo": gate["tempo"]}


def _tid_do_ws(ws: Path) -> int:
    try:
        return int(Path(ws).name)
    except ValueError:
        raise GitErro("invalid workspace")


def merge_e_push(cfg: Config, repo: str, ws: Path, branch: str, root=None, commit: str | None = None, destino: str | None = None) -> dict:
    """Merges the branch into the base and pushes to GitHub. Guards: allowed repo, no force, remote fast-forward only,
    `agent/*` branches only, audit. Called ONLY from the validated [Publish] callback (or the approved CLI publish).
    "root" mode (default): the root daemon merges and pushes with root's credential (the service has no token).
    "deploy-key" mode (legacy): the service pushes directly with a per-repo deploy key."""
    if not repo_permitido(cfg, repo) or not branch.startswith("agent/"):
        raise GitErro("merge/push denied by the guards")
    base = base_do_repo(cfg, repo)
    if sem_remoto(cfg, repo):
        raise GitErro("repo without remote: Claude reviews the workspace branch and integrates it by hand")
    current = git(cfg, ["rev-parse", f"refs/heads/{branch}"], ws).stdout.strip()
    commit = commit or current
    if current != commit or (destino is not None and destino != base):
        raise GitErro("approved commit or destination changed")
    if cfg.push_modo == "root":
        if root is None:
            raise GitErro("root daemon unavailable for the push")
        r = root.push_base(repo, _tid_do_ws(ws), branch, commit=commit, destino=base)
        if not r.get("ok"):
            auditar(cfg, "merge", repo, branch, resultado="conflict" if "conflict" in str(r.get("erro")) else "failed",
                    extra=str(r.get("erro")))
            raise GitErro(str(r.get("erro")))
        auditar(cfg, "merge+push", repo, f"{branch}->{base}", r["hash"], extra="via root daemon")
        return {"hash": r["hash"], "base": base}
    git(cfg, ["fetch", "-q", "github", base], ws, repo=repo, timeout=120)
    git(cfg, ["checkout", "-q", "-B", f"integra-{base}", f"github/{base}"], ws)
    r = git(cfg, ["merge", "--no-ff", "-m", f"Merge {branch}", commit], ws, check=False)
    if r.returncode != 0:
        git(cfg, ["merge", "--abort"], ws, check=False)
        auditar(cfg, "merge", repo, branch, resultado="conflict")
        raise GitErro("merge conflict: " + (r.stdout + r.stderr).strip()[-200:])
    h = git(cfg, ["rev-parse", "HEAD"], ws).stdout.strip()
    p = git(cfg, ["push", "github", f"HEAD:refs/heads/{base}"], ws, repo=repo, check=False, timeout=180)
    if p.returncode != 0:
        auditar(cfg, "push", repo, base, h, "failed", p.stderr.strip()[-150:])
        raise GitErro("push rejected: " + p.stderr.strip()[-200:])
    auditar(cfg, "merge+push", repo, f"{branch}->{base}", h)
    return {"hash": h, "base": base}


def push_branch(cfg: Config, repo: str, ws: Path, branch: str, root=None) -> str:
    if not repo_permitido(cfg, repo) or not branch.startswith("agent/"):
        raise GitErro("branch push denied by the guards")
    h = git(cfg, ["rev-parse", branch], ws).stdout.strip()
    if sem_remoto(cfg, repo):
        auditar(cfg, "push-branch", repo, branch, h, "local", extra="repo without remote: workspace/patch only")
        return h
    if cfg.push_modo == "root":
        if root is None:
            raise GitErro("root daemon unavailable for the push")
        r = root.push_branch(repo, _tid_do_ws(ws), branch)
        auditar(cfg, "push-branch", repo, branch, h, "ok" if r.get("ok") else "failed", extra="via root daemon")
        if not r.get("ok"):
            raise GitErro(str(r.get("erro")))
        return h
    p = git(cfg, ["push", "github", f"refs/heads/{branch}:refs/heads/{branch}"], ws, repo=repo, check=False, timeout=180)
    auditar(cfg, "push-branch", repo, branch, h, "ok" if p.returncode == 0 else "failed")
    if p.returncode != 0:
        raise GitErro(p.stderr.strip()[-200:])
    return h


# ---------------------------------------------------------------- chat commit (free text with editing)
ARQUIVOS_SENSIVEIS = re.compile(
    r"(?i)(^|/)(\.env(\..*)?|auth\.json|credentials(\.json)?|id_(rsa|ed25519|ecdsa|dsa)|\.netrc|\.npmrc|\.pypirc|"
    r"publish\.(env|key|json)|.*\.(pem|key|p12|pfx|sqlite3?|db|dump|sql\.gz))$")
# sanitize patterns that apply to code: other services' credentials + typical keys; "pii" and the generic token are
# left out (legitimate e-mails/phones/hashes in code would block commits for no reason).
_NOMES_SEGREDO = {"chave-privada", "token-telegram", "aws", "b2", "restic", "jwt", "sk", "github", "slack", "google-api",
                  "url-credencial"}
_ATRIB_LITERAL = re.compile(
    r"(?i)\b[A-Z0-9_]*(?:KEY|TOKEN|PASSWORD|PASSWD|SECRET|SENHA|CREDENTIAL)[A-Z0-9_]*[\"']?\s*[=:]\s*[\"']([A-Za-z0-9/+_=.\-]{16,})[\"']")


def achar_segredos(cfg: Config, ws: Path) -> list[str]:
    """Looks at the INDEX (staged): sensitive files and added lines that look like secrets."""
    from . import sanitize
    problemas: list[str] = []
    nomes = git(cfg, ["diff", "--cached", "--name-only"], ws, check=False).stdout.splitlines()
    for n in nomes:
        if ARQUIVOS_SENSIVEIS.search(n):
            problemas.append(f"sensitive file: {n}")
    atual = ""
    for linha in git(cfg, ["diff", "--cached", "-U0", "--no-color"], ws, check=False).stdout.splitlines():
        if linha.startswith("+++ b/"):
            atual = linha[6:]
            continue
        if not linha.startswith("+") or linha.startswith("+++"):
            continue
        corpo = linha[1:]
        achou = [nome for grupo, nome, rx, _s in sanitize._PADROES if nome in _NOMES_SEGREDO and rx.search(corpo)]
        if _ATRIB_LITERAL.search(corpo):
            m = _ATRIB_LITERAL.search(corpo).group(1)
            if re.search(r"\d", m) and re.search(r"[A-Za-z]", m):
                achou.append("secret-assignment")
        if achou:
            problemas.append(f"possible secret ({achou[0]}) in {atual or '?'}")
    return problemas[:8]


RX_MSG_IA = re.compile(r"(?i)\b(codex|claude|openai|chatgpt|gpt\w*|anthropic|co-authored\w*|ia|ai)\b")


def mensagem_commit(texto: str) -> str:
    from . import sanitize
    linha = sanitize.mascarar((texto or "").strip().splitlines()[0] if (texto or "").strip() else "", dev=True)
    linha = re.sub(r"\s+", " ", linha).strip(" -+:.\"'")[:60].strip()
    if not linha or RX_MSG_IA.search(linha) or "[" in linha:
        return "Changes from chat"
    return linha[0].upper() + linha[1:]


def commit_chat(cfg: Config, ws: Path, tid: int, base: str, mensagem: str) -> dict:
    """End of a chat turn: commit the changes (no co-author / no AI mention), with the same guards as finalizar
    (stray notes out of the commit) plus a secret scan. {"mudou", "bloqueado": [...], "arquivos", "excluidos"}."""
    out = cfg.resultados / str(tid)
    out.mkdir(parents=True, exist_ok=True)
    try:
        verificar_config_ws(cfg, ws)
    except GitErro as e:
        auditar(cfg, "commit-chat", ws.parent.name, "", resultado="blocked", extra=str(e))
        return {"mudou": False, "bloqueado": [str(e)], "arquivos": [], "excluidos": []}
    if not git(cfg, ["status", "--porcelain"], ws).stdout.strip():
        return {"mudou": False, "bloqueado": [], "arquivos": [], "excluidos": []}
    git(cfg, ["add", "-A"], ws)
    excluidos = _excluir_notas(cfg, ws, base, out)
    if git(cfg, ["diff", "--cached", "--quiet"], ws, check=False).returncode == 0:
        return {"mudou": False, "bloqueado": [], "arquivos": [], "excluidos": excluidos}
    problemas = achar_segredos(cfg, ws)
    if problemas:
        git(cfg, ["reset", "-q"], ws, check=False)  # nothing stays in the index; files remain in the workspace
        auditar(cfg, "commit-chat", ws.parent.name, "", resultado="blocked", extra="; ".join(problemas))
        return {"mudou": False, "bloqueado": problemas, "arquivos": [], "excluidos": excluidos}
    git(cfg, ["commit", "-q", "-m", mensagem or "Changes from chat"], ws)
    arquivos = [l for l in git(cfg, ["diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"], ws, check=False).stdout.splitlines() if l]
    return {"mudou": True, "bloqueado": [], "arquivos": arquivos, "excluidos": excluidos}
