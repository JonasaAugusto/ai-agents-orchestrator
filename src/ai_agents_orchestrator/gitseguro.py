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

ENDURECIDO = ("-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", "-c", "credential.helper=",
              "-c", "core.gitProxy=", "-c", "protocol.ext.allow=never", "-c", "protocol.git.allow=never",
              "-c", "protocol.ssh.allow=never", "-c", "protocol.http.allow=never", "-c", "protocol.https.allow=never",
              "-c", "protocol.file.allow=always", "-c", "core.sshCommand=false", "-c", "log.showSignature=false")
CONFIAVEL = ("-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", "-c", "protocol.ext.allow=never")


def env_endurecido(base: dict | None = None) -> dict:
    e = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/nonexistent", "LC_ALL": "C.UTF-8", "GIT_TERMINAL_PROMPT": "0"}
    e.update(base or {})
    for k in ("GIT_DIR", "GIT_WORK_TREE", "GIT_CONFIG", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT", "GIT_SSH_COMMAND",
              "GIT_SSH", "GIT_PROXY_COMMAND", "GIT_EXEC_PATH", "GIT_ASKPASS", "SSH_ASKPASS"):
        e.pop(k, None)
    e.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null")
    return e


def git_endurecido(args: list[str], cwd=None, env: dict | None = None, timeout: int = 300, **kw) -> subprocess.CompletedProcess:
    kw.setdefault("capture_output", "stdout" not in kw)
    return subprocess.run(["git", *ENDURECIDO, *args], cwd=cwd, env=env_endurecido(env), text=kw.pop("text", True),
                          timeout=timeout, **kw)


def git_confiavel(args: list[str], cwd=None, env: dict | None = None, timeout: int = 300) -> subprocess.CompletedProcess:
    e = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/root", "LC_ALL": "C.UTF-8", "GIT_TERMINAL_PROMPT": "0"}
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
