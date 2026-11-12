"""Git invocations that never let a less-privileged user's repository configuration execute code.

Repository config (`.git/config`) and hooks can run commands (`core.fsmonitor`, `core.hooksPath`,
`credential.helper=!cmd`, `core.gitProxy`, `url.*.insteadOf` to an `ext::` transport, filter drivers...). Whoever can
write that file chooses what git executes. So:

- `git_endurecido`: for any repository that may be written by someone else. No system/global config, and the
  command-line overrides below (which take precedence over repo config) disable every hook/helper/proxy/transport
  that could execute code. Network transports are disabled: it is meant for local paths only.
- `git_confiavel`: for repositories root itself owns (e.g. its private mirrors under `root_state`), where root's
  global config is needed for its credential helper. Hooks, fsmonitor and the `ext::` transport stay disabled.
- `git_como_dono`: read-only commands in a directory owned by another user. Root drops to that user's uid (so any
  config there runs with the owner's own privileges, never root's); everyone else runs hardened.

No safe.directory wildcard is used anywhere: root never opts into trusting a repository it does not own.
"""
from __future__ import annotations

import os
import pwd
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path

ENDURECIDO = ("-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", "-c", "credential.helper=",
              "-c", "core.gitProxy=", "-c", "protocol.ext.allow=never", "-c", "protocol.git.allow=never",
              "-c", "protocol.ssh.allow=never", "-c", "protocol.http.allow=never", "-c", "protocol.https.allow=never",
              "-c", "protocol.file.allow=always", "-c", "core.sshCommand=false", "-c", "log.showSignature=false")
CONFIAVEL = ("-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", "-c", "protocol.ext.allow=never")


def env_endurecido(base: dict | None = None) -> dict:
    e = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/nonexistent", "LC_ALL": "C.UTF-8", "GIT_TERMINAL_PROMPT": "0", "GIT_NO_REPLACE_OBJECTS": "1"}
    e.update(base or {})
    for k in ("GIT_DIR", "GIT_WORK_TREE", "GIT_CONFIG", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT", "GIT_SSH_COMMAND",
              "GIT_SSH", "GIT_PROXY_COMMAND", "GIT_EXEC_PATH", "GIT_ASKPASS", "SSH_ASKPASS"):
        e.pop(k, None)
    e.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_SYSTEM="/dev/null", GIT_CONFIG_GLOBAL="/dev/null")
    return e


def git_endurecido(args: list[str], cwd=None, env: dict | None = None, timeout: int = 300, **kw) -> subprocess.CompletedProcess:
    kw.setdefault("capture_output", "stdout" not in kw)
    return subprocess.run(["git", *ENDURECIDO, *args], cwd=cwd, env=env_endurecido(env), text=kw.pop("text", True),
                          timeout=timeout, **kw)


def git_confiavel(args: list[str], cwd=None, env: dict | None = None, timeout: int = 300) -> subprocess.CompletedProcess:
    e = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/root", "LC_ALL": "C.UTF-8", "GIT_TERMINAL_PROMPT": "0", "GIT_NO_REPLACE_OBJECTS": "1"}
    e.update(env or {})
    return subprocess.run(["git", *CONFIAVEL, *args], cwd=cwd, env=e, capture_output=True, text=True, timeout=timeout)


def dono(caminho) -> int | None:
    try:
        return os.lstat(caminho).st_uid
    except OSError:
        return None


def git_como_dono(caminho, args: list[str], timeout: int = 60, **kw) -> subprocess.CompletedProcess:
    """Runs `git -C caminho args` without ever letting root honour config written by another user."""
    uid = dono(caminho)
    if os.geteuid() == 0 and uid not in (None, 0):
        try:
            gid = pwd.getpwuid(uid).pw_gid
        except KeyError:
            gid = uid
        kw.update(user=uid, group=gid, extra_groups=[])
    return git_endurecido(["-C", str(caminho), *args], timeout=timeout, **kw)


# Local config cannot be disabled by -c overrides (filter names are unbounded).
# Give Git a fresh gitdir instead: only object/ref storage is shared with the agent.
# The config is authored here, never copied/validated from a mutable .git/config.
@contextmanager
def repositorio_isolado(gitdir, worktree=None, remotes=None, escrever=False, temporarios=None):
    original = Path(gitdir).absolute()
    with tempfile.TemporaryDirectory(prefix='aao-git-', dir=temporarios) as temp:
        private = Path(temp)
        cfg = '[core]\n repositoryformatversion = 0\n bare = ' + ('false' if worktree else 'true') + '\n'
        for name, url in (remotes or {}).items():
            # Config syntax is data; quote values, including paths with spaces or newlines.
            quoted = str(url).replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n')
            cfg += f'[remote "{name}"]\n url = "{quoted}"\n fetch = +refs/heads/*:refs/remotes/{name}/*\n'
        (private/'config').write_text(cfg)
        for name in ('objects', 'refs', 'logs', 'info'):
            if (original/name).is_dir():
                (private/name).symlink_to(original/name, target_is_directory=True)
        files = ('HEAD', 'index', 'packed-refs', 'shallow', 'FETCH_HEAD', 'ORIG_HEAD',
                 'MERGE_HEAD', 'MERGE_MSG', 'MERGE_MODE', 'AUTO_MERGE')
        before = {}
        for name in files:
            source = original/name
            before[name] = source.read_bytes() if source.is_file() else None
            if before[name] is not None:
                (private/name).write_bytes(before[name])
        try:
            yield private
        finally:
            if escrever:
                for name in files:
                    target = private/name
                    after = target.read_bytes() if target.is_file() else None
                    if after == before[name]:
                        continue
                    if after is None:
                        (original/name).unlink(missing_ok=True)
                    else:
                        # Exclusive temp + rename, never truncate an agent-planted symlink.
                        fd, tmp = tempfile.mkstemp(prefix='.aao-', dir=original)
                        try:
                            with os.fdopen(fd, 'wb') as stream:
                                stream.write(after)
                            os.replace(tmp, original/name)
                        finally:
                            if os.path.lexists(tmp):
                                os.unlink(tmp)
