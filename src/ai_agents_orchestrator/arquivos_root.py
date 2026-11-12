"""Descriptor-relative privileged writes. Never traverse service-owned parents or symlinks."""
from __future__ import annotations

import os
import secrets
import stat
from pathlib import Path


def abrir_dir(path: Path, gid: int | None = None, modo: int = 0o750) -> int:
    """Walk from / using O_NOFOLLOW; only the effective uid may control a parent.

    Root-owned sticky directories (e.g. /tmp in disposable tests) may contain our own
    protected directory; their sticky bit prevents another uid replacing that entry.
    Existing untrusted directories are refused, never 'repaired' with chmod/chown.
    """
    path = Path(os.path.abspath(path))
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            parent = os.fstat(fd)
            if parent.st_uid != os.geteuid() or (parent.st_mode & 0o022 and not parent.st_mode & stat.S_ISVTX):
                raise PermissionError('untrusted privileged state parent')
            try:
                os.mkdir(part, modo, dir_fd=fd)
            except FileExistsError:
                pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
            st = os.fstat(fd)
            if st.st_uid != os.geteuid() or (st.st_mode & 0o022 and not st.st_mode & stat.S_ISVTX):
                raise PermissionError('untrusted privileged state directory')
        if os.fstat(fd).st_mode & 0o022:
            raise PermissionError('privileged destination is writable by others')
        if gid is not None and os.geteuid() == 0:
            os.fchown(fd, 0, gid)
        os.fchmod(fd, modo)
        return fd
    except BaseException:
        os.close(fd)
        raise


def gravar(path: Path, texto: str, gid: int | None = None, modo: int = 0o640) -> None:
    parent = abrir_dir(path.parent, gid)
    name = '.' + path.name + '-' + secrets.token_hex(16)
    try:
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, modo, dir_fd=parent)
        try:
            os.fchmod(fd, modo)
            if gid is not None and os.geteuid() == 0:
                os.fchown(fd, 0, gid)
            with os.fdopen(fd, 'w', encoding='utf-8', closefd=False) as stream:
                stream.write(texto)
                stream.flush()
                os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(name, path.name, src_dir_fd=parent, dst_dir_fd=parent)
    finally:
        try:
            os.unlink(name, dir_fd=parent)
        except FileNotFoundError:
            pass
        os.close(parent)


def abrir_append(path: Path) -> int:
    parent = abrir_dir(path.parent, modo=0o700)
    try:
        fd = os.open(path.name, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                     0o600, dir_fd=parent)
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_uid != os.geteuid() or st.st_mode & 0o077:
            os.close(fd)
            raise PermissionError('untrusted privileged append file')
        return fd
    finally:
        os.close(parent)
