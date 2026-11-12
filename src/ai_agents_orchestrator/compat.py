"""Small OS boundary for the user runtime. No privilege elevation or shell evaluation."""
from __future__ import annotations

import errno
import os
import re
import shutil
import subprocess
import sys
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

WINDOWS = sys.platform == "win32"


def euid() -> int | None:
    return os.geteuid() if hasattr(os, "geteuid") else None


def user_state_dir(env=None) -> Path:
    e = os.environ if env is None else env
    if WINDOWS:
        return Path(e.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "ai-agents-orchestrator/state"
    return Path(e.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "ai-agents-orchestrator"


def windows_env() -> dict[str, str]:
    # Windows CreateProcess, Node, PowerShell and Git need these even in a minimal env.
    names = ("SystemRoot", "WINDIR", "COMSPEC", "PATHEXT", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
             "TEMP", "TMP", "HOMEDRIVE", "HOMEPATH")
    return {k: os.environ[k] for k in names if k in os.environ} if WINDOWS else {}


def system_tool(name: str) -> str:
    if not WINDOWS:
        return name
    system = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
    if name == "powershell.exe":
        return str(system / "WindowsPowerShell/v1.0/powershell.exe")
    return str(system / name)


def which(prog: str, env=None) -> str | None:
    e = os.environ if env is None else env
    path = e.get("PATH", os.defpath)
    # Prefer native executables, then npm .cmd shims, then PowerShell-only installs.
    found = shutil.which(prog, path=path)
    if found or not WINDOWS:
        return found
    if Path(prog).suffix.lower() == ".ps1" and Path(prog).is_file():
        return str(Path(prog).resolve())
    for suffix in (".exe", ".cmd", ".bat", ".ps1"):
        candidate = shutil.which(prog + suffix, path=path)
        if candidate:
            return candidate
    for directory in path.split(os.pathsep):
        candidate = Path(directory) / (prog if prog.lower().endswith(".ps1") else prog + ".ps1")
        if candidate.is_file():
            return str(candidate.resolve())
    return None


def command(argv, env=None) -> list[str]:
    args = [str(a) for a in argv]
    if not WINDOWS:
        return args
    executable = which(args[0], env) or args[0]
    if executable.lower().endswith(".ps1"):
        # -File preserves arguments as data. Do not bypass the user's execution policy.
        return [system_tool("powershell.exe"), "-NoLogo", "-NoProfile", "-NonInteractive", "-File", executable, *args[1:]]
    if executable.lower().endswith((".cmd", ".bat")):
        # Windows launches batch shims through cmd.exe even with shell=False. Reject
        # metacharacters rather than letting paths/configuration become shell code.
        try:
            shim = Path(executable).read_text(encoding="utf-8")
        except OSError:
            shim = ""
        match = re.search(r'"%dp0%[\\/]([^"\r\n]+\.m?js)"', shim, re.I)
        if match:
            script = Path(executable).parent / Path(match[1].replace("\\", "/"))
            node = Path(executable).with_name("node.exe")
            return [str(node) if node.is_file() else (which("node.exe", env) or "node.exe"), str(script), *args[1:]]
        if any(re.search(r'["%!?^&|<>\r\n]', a) for a in [executable, *args[1:]]):
            raise OSError("unsafe batch shim arguments; configure a native .exe or .ps1 CLI")
    return [executable, *args[1:]]


def process_group() -> dict:
    return {"creationflags": 0x00000200} if WINDOWS else {"start_new_session": True}


def kill_tree(pid: int) -> None:
    # /T includes children (node/npm shims included); /F avoids orphaned descendants.
    r = subprocess.run([system_tool("taskkill.exe"), "/PID", str(int(pid)), "/T", "/F"],
                       capture_output=True, timeout=30)
    if r.returncode not in (0, 128):
        raise OSError("Windows could not terminate the engine process tree")


class ProcessJob:
    """Kill descendants on handle close, including when Windows terminates aao itself."""
    def __init__(self, process):
        self.handle = None
        if not WINDOWS:
            return
        import ctypes
        from ctypes import wintypes
        class Basic(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                        ("flags", wintypes.DWORD), ("min_ws", ctypes.c_size_t), ("max_ws", ctypes.c_size_t),
                        ("active", wintypes.DWORD), ("affinity", ctypes.c_size_t),
                        ("priority", wintypes.DWORD), ("scheduling", wintypes.DWORD)]
        class Counters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in
                        ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]
        class Extended(ctypes.Structure):
            _fields_ = [("basic", Basic), ("io", Counters), ("process_memory", ctypes.c_size_t),
                        ("job_memory", ctypes.c_size_t), ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel.SetInformationJobObject.restype = wintypes.BOOL
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.CreateJobObjectW(None, None)
        limits = Extended()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not handle:
            process.kill()
            process.wait()
            raise ctypes.WinError(ctypes.get_last_error())
        if (not kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits))
                or not kernel.AssignProcessToJobObject(handle, int(process._handle))):
            error = ctypes.get_last_error()
            kernel.CloseHandle(handle)
            process.kill()
            process.wait()
            raise ctypes.WinError(error)
        self.handle, self.kernel = handle, kernel

    def close(self):
        if self.handle is not None:
            self.kernel.CloseHandle(self.handle)
            self.handle = None

    def __del__(self):
        self.close()


@lru_cache(maxsize=1)
def windows_sid() -> str:
    r = subprocess.run([system_tool("whoami.exe"), "/user", "/fo", "csv", "/nh"], capture_output=True, timeout=10)
    match = re.search(rb"S-1-\d+(?:-\d+)+", r.stdout)
    if r.returncode or not match:
        raise OSError("cannot determine current Windows user SID")
    return match[0].decode("ascii")


def private(path: Path) -> None:
    """Replace the DACL with current-user-only access; fail closed before writing secrets."""
    if not WINDOWS:
        path.chmod(0o700 if path.is_dir() else 0o600)
        return
    import ctypes
    from ctypes import wintypes
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    convert = advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    convert.restype = wintypes.BOOL
    set_security = advapi.SetFileSecurityW
    set_security.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
    set_security.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    descriptor = ctypes.c_void_p()
    inherit = "OICI" if path.is_dir() else ""
    if not convert(f"D:P(A;{inherit};FA;;;{windows_sid()})", 1, ctypes.byref(descriptor), None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not set_security(str(path), 0x80000004, descriptor):  # protected DACL
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel.LocalFree(descriptor)


@contextmanager
def file_lock(path: Path, blocking: bool = True):
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    locked = False
    try:
        if WINDOWS:
            import msvcrt
            # The locked byte can extend beyond EOF. Lock release is automatic on close.
            try:
                msvcrt.locking(fd, msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise BlockingIOError("aao file is locked") from exc
                raise
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        locked = True
        yield
    finally:
        if WINDOWS and locked:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        os.close(fd)
