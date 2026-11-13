"""Documentation stays true to the code: internal links and anchors, documented subcommands and variables, release files."""
import argparse
import re
import subprocess
import sys
import unittest
import unittest.mock
from pathlib import Path
from urllib.parse import unquote

from ai_agents_orchestrator import cli

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def markdown_files() -> list[Path]:
    out = subprocess.run(["git", "-c", "safe.directory=*", "-C", str(ROOT), "ls-files", "*.md"], capture_output=True, text=True)
    if out.returncode != 0:  # not a checkout (sdist): the docs are not shipped, nothing to check
        return []
    return [ROOT / n for n in out.stdout.splitlines()]


def strip_code(text: str) -> str:
    return re.sub(r"^(```|~~~).*?^\1[^\n]*$", "", text, flags=re.S | re.M)


def slug(heading: str) -> str:
    """GitHub's anchor for a heading: lower case, punctuation dropped, spaces to hyphens."""
    heading = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", heading).replace("`", "").strip().lower()
    return re.sub(r"[^\w\- ]", "", heading).replace(" ", "-")


def anchors(path: Path) -> set[str]:
    seen: dict[str, int] = {}
    out = set()
    for line in strip_code(path.read_text(encoding="utf-8")).splitlines():
        m = re.match(r"#{1,6}\s+(.*?)\s*#*\s*$", line)
        if m:
            base = slug(m.group(1))
            n = seen.get(base, 0)
            seen[base] = n + 1
            out.add(base if n == 0 else f"{base}-{n}")
    return out


class InternalLinks(unittest.TestCase):
    def test_every_relative_link_and_anchor_resolves(self):
        files = markdown_files()
        cache: dict[Path, set[str]] = {}
        broken = []
        for md in files:
            text = strip_code(md.read_text(encoding="utf-8"))
            text = re.sub(r"`[^`\n]*`", "", text)
            for target in re.findall(r"\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)", text):
                if re.match(r"[a-z][a-z0-9+.-]*:", target, re.I):
                    continue
                name, _, frag = target.partition("#")
                dest = (md.parent / unquote(name)).resolve() if name else md
                if not dest.exists():
                    broken.append(f"{md.relative_to(ROOT)}: missing file {target}")
                    continue
                if frag and dest.suffix == ".md":
                    if dest not in cache:
                        cache[dest] = anchors(dest)
                    if unquote(frag).lower() not in cache[dest]:
                        broken.append(f"{md.relative_to(ROOT)}: no heading for {target}")
        self.assertEqual(broken, [])

    def test_slug_matches_github(self):
        self.assertEqual(slug("Quickstart: lite mode"), "quickstart-lite-mode")
        self.assertEqual(slug("Início rápido: modo leve"), "início-rápido-modo-leve")
        self.assertEqual(slug("The `aao queue` [script](x.md) mode"), "the-aao-queue-script-mode")


def subcommands() -> dict[str, argparse.ArgumentParser]:
    for action in cli.parser()._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    return {}


class DocsMatchParser(unittest.TestCase):
    def test_documented_aao_commands_exist_and_have_help(self):
        known = subcommands()
        self.assertGreater(len(known), 20)
        used: dict[str, set[str]] = {}
        for md in markdown_files():
            text = md.read_text(encoding="utf-8")
            spans = re.findall(r"`([^`\n]+)`", text) + re.findall(r"^(?:```|~~~).*?^(?:```|~~~)", text, flags=re.S | re.M)
            for span in spans:
                for m in re.finditer(r"(?<![\w/.-])aao ([a-z][a-z-]*)", span):
                    if m.group(1) != "env":  # `sudo -u aao env HOME=...` is a user name, not the CLI
                        used.setdefault(m.group(1), set()).add(str(md.relative_to(ROOT)))
        unknown = {c: sorted(f) for c, f in used.items() if c not in known}
        self.assertEqual(unknown, {})
        for name in sorted(used):
            with self.subTest(name):
                with self.assertRaises(SystemExit) as raised, unittest.mock.patch("sys.stdout"):
                    cli.parser().parse_args([name, "--help"])
                self.assertEqual(raised.exception.code, 0)

    def test_primary_commands_are_in_both_readmes(self):
        for readme in ("README.md", "README.pt-BR.md"):
            text = (ROOT / readme).read_text(encoding="utf-8")
            for name in ("delegate", "status", "show", "result", "review", "publish", "serve", "setup", "doctor", "queue", "mcp"):
                self.assertIn(f"aao {name}", text, f"{name} is missing from {readme}")

    def test_environment_variables_in_the_code_are_documented(self):
        used = set()
        for py in (ROOT / "src").rglob("*.py"):
            used |= set(re.findall(r"AAO_[A-Z0-9_]+", py.read_text(encoding="utf-8")))
        docs = "".join(p.read_text(encoding="utf-8") for p in markdown_files())
        self.assertEqual(sorted(v for v in used if v not in docs), [])

    def test_documented_config_keys_exist_in_the_example(self):
        example = (ROOT / "examples/config.example.toml").read_text(encoding="utf-8")
        self.assertIn("push_url_scheme", example)
        self.assertIn("push_url_scheme", (ROOT / "README.md").read_text(encoding="utf-8"))
        self.assertIn("push_url_scheme", (ROOT / "README.pt-BR.md").read_text(encoding="utf-8"))


class RepositoryScripts(unittest.TestCase):
    def run_script(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, *args], cwd=ROOT, capture_output=True, text=True)

    def test_release_files_agree(self):
        if not (ROOT / ".git").exists():
            self.skipTest("not a checkout")
        done = self.run_script("scripts/check_release.py")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)

    def test_release_notes_come_from_the_changelog(self):
        if not (ROOT / ".git").exists():
            self.skipTest("not a checkout")
        done = self.run_script("scripts/check_release.py", "--notes", "1.5.0")
        self.assertEqual(done.returncode, 0)
        self.assertIn("aao queue", done.stdout)
        self.assertNotEqual(self.run_script("scripts/check_release.py", "--notes", "9.9.9").returncode, 0)

    def test_no_private_paths_in_tracked_files(self):
        if not (ROOT / ".git").exists():
            self.skipTest("not a checkout")
        done = self.run_script("scripts/check_private_paths.py")
        self.assertEqual(done.returncode, 0, done.stdout)


if __name__ == "__main__":
    unittest.main()
