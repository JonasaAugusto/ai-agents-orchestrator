"""Release consistency: pyproject, package, plugin.json and CHANGELOG must name the same version.

    python scripts/check_release.py              # the four sources agree with each other
    python scripts/check_release.py --tag v1.5.1 # ...and with the tag being released
    python scripts/check_release.py --notes 1.5.1 # print that CHANGELOG section (for `gh release --notes-file`)
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SECTION = re.compile(r"^## \[(?P<v>\d+\.\d+\.\d+)\](?: - (?P<d>\d{4}-\d{2}-\d{2}))?\s*$", re.M)


def versions() -> dict[str, str]:
    init = re.search(r'^__version__ = "([^"]+)"', (ROOT / "src/ai_agents_orchestrator/__init__.py").read_text(encoding="utf-8"), re.M)
    log = SECTION.search((ROOT / "CHANGELOG.md").read_text(encoding="utf-8"))
    return {
        "pyproject.toml": tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"],
        "__init__.py": init.group(1) if init else "?",
        "plugin.json": json.loads((ROOT / "claude-plugin/.claude-plugin/plugin.json").read_text(encoding="utf-8"))["version"],
        "CHANGELOG.md (latest section)": log.group("v") if log else "?",
    }


def notes(version: str) -> str:
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    for m in SECTION.finditer(text):
        if m.group("v") == version:
            nxt = SECTION.search(text, m.end())
            body = text[m.end():nxt.start() if nxt else len(text)].strip()
            if not body:
                raise SystemExit(f"CHANGELOG section [{version}] is empty")
            return body + "\n"
    raise SystemExit(f"CHANGELOG has no section [{version}]")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag")
    ap.add_argument("--notes", metavar="VERSION")
    args = ap.parse_args()
    if args.notes:
        sys.stdout.write(notes(args.notes))
        return 0
    found = versions()
    expected = args.tag.removeprefix("v") if args.tag else found["pyproject.toml"]
    bad = {k: v for k, v in found.items() if v != expected}
    for k, v in found.items():
        print(f"{k}: {v}")
    if bad:
        print(f"version mismatch, expected {expected}: " + ", ".join(bad))
        return 1
    notes(expected)  # the section must exist and not be empty
    if args.tag:
        log = SECTION.search((ROOT / "CHANGELOG.md").read_text(encoding="utf-8"))
        if not (log and log.group("d")):
            print("latest CHANGELOG section has no date")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
