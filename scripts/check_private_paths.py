"""Fail when a tracked file carries what looks like private paths, personal mailboxes or chat ids.

Generic patterns only. A list of project-specific private names belongs in a local pre-commit hook, never here.
Usage: python scripts/check_private_paths.py
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = {
    "home of root": re.compile(r"(?<![\w.~-])/root/(?!\.)"),  # /root/.claude and the like are generic full-mode defaults
    "private checkout under /srv/projetos": re.compile(r"/srv/projetos\b"),
    "windows or WSL user profile": re.compile(r"(?i)(/mnt/c/users/|[a-z]:\\users\\)(?!<|\{|%|you\b|user\b|name\b|test\b)\w+"),
    "personal mailbox": re.compile(r"(?i)[\w.+-]+@(gmail|hotmail|outlook|yahoo|proton(mail)?|icloud)\.[a-z.]+"),
    "numeric chat id": re.compile(r"(?i)(chat[_ -]?id\W{0,4})-?\d{8,10}\b"),
}
SKIP = {"scripts/check_private_paths.py"}


def tracked() -> list[str]:
    out = subprocess.run(["git", "-c", "safe.directory=*", "-C", str(ROOT), "ls-files", "-z"], capture_output=True, check=True).stdout
    return [n for n in out.decode().split("\0") if n and n not in SKIP]


def main() -> int:
    found = []
    for name in tracked():
        try:
            text = (ROOT / name).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for number, line in enumerate(text.splitlines(), 1):
            for label, pattern in PATTERNS.items():
                if pattern.search(line):
                    found.append(f"{name}:{number}: {label}")
    for item in found:
        print(item)
    if found:
        print(f"{len(found)} private-looking value(s) in tracked files")
        return 1
    print("no private paths found")
    return 0


if __name__ == "__main__":
    sys.exit(main())
