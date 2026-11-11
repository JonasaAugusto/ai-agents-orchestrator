"""Process hardening at service start.

The service and the engines (Codex) run under the same uid. Without this, an engine could read the service's
secrets from `/proc/<service pid>/environ` (the initial environment block stays in memory even after `os.environ`
is changed) or attach to it with ptrace. `PR_SET_DUMPABLE = 0` makes `/proc/<pid>/environ`, `/proc/<pid>/mem` and
ptrace require CAP_SYS_PTRACE, so a same-uid process can no longer read them. The secrets are then removed from
`os.environ` so no child process can inherit them.

This narrows, but does not remove, what a same-uid process can do (it can still read every file the service user
can read). The recommended hardening is to run the engines under a separate uid (see SECURITY.md).
"""
from __future__ import annotations

import os
import sys

SEGREDOS = ("AAO_TELEGRAM_TOKEN", "AAO_PUBLISH_KEY")
PR_SET_DUMPABLE = 4


def _prctl_padrao(*args) -> int:
    if not sys.platform.startswith("linux"):
        return -1
    import ctypes
    libc = ctypes.CDLL(None, use_errno=True)
    return libc.prctl(*[ctypes.c_ulong(a) for a in args])


def proteger_processo(env=None, prctl=None) -> dict:
    """Marks the process non-dumpable (Linux) and removes the secrets from the environment. Returns the removed
    values (read them into memory BEFORE calling this, or use the returned dict)."""
    env = os.environ if env is None else env
    try:
        (prctl or _prctl_padrao)(PR_SET_DUMPABLE, 0, 0, 0, 0)
    except OSError:
        pass
    lidos = {}
    for k in list(env):
        if k in SEGREDOS:
            lidos[k] = env.pop(k)
    return lidos
