"""Claude Code plugin shipped in claude-plugin/: manifest, skill, hooks and commands stay consistent with the CLI."""
import json
import re
import shlex
import unittest
from pathlib import Path

from ai_agents_orchestrator import cli

RAIZ = Path(__file__).resolve().parents[1]
PLUGIN = RAIZ / "claude-plugin"


def frontmatter(texto: str) -> dict:
    m = re.match(r"^---\n(.*?)\n---\n", texto, re.S)
    assert m, "missing YAML frontmatter"
    campos = {}
    for linha in m.group(1).splitlines():
        k, _, v = linha.partition(":")
        campos[k.strip()] = v.strip()
    return campos


class Plugin(unittest.TestCase):
    def test_manifestos(self):
        m = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())
        self.assertEqual(m["name"], "ai-agents-orchestrator")
        mk = json.loads((RAIZ / ".claude-plugin" / "marketplace.json").read_text())
        fontes = {p["name"]: p["source"] for p in mk["plugins"]}
        self.assertEqual(fontes["ai-agents-orchestrator"], "./claude-plugin")
        for fonte in fontes.values():
            self.assertTrue((RAIZ / fonte / ".claude-plugin" / "plugin.json").is_file(), fonte)

    def test_hooks_chamam_comandos_que_o_cli_conhece(self):
        hooks = json.loads((PLUGIN / "hooks" / "hooks.json").read_text())["hooks"]
        self.assertEqual(set(hooks), {"StopFailure", "UserPromptSubmit", "SessionStart", "Stop"})
        self.assertEqual(hooks["StopFailure"][0]["matcher"], "rate_limit")
        for evento, grupos in hooks.items():
            for g in grupos:
                for h in g["hooks"]:
                    argv = [h["command"], *h["args"]]
                    self.assertEqual(argv[0], "aao")
                    cli.parser().parse_args(argv[1:])  # raises SystemExit if the CLI does not accept it
        self.assertEqual(json.loads((RAIZ / "examples" / "claude-hooks.json").read_text())["hooks"], hooks)

    def test_skill(self):
        texto = (PLUGIN / "skills" / "orchestrator-workflow" / "SKILL.md").read_text()
        fm = frontmatter(texto)
        self.assertEqual(fm["name"], "orchestrator-workflow")
        self.assertLess(len(fm["description"]), 200)
        self.assertTrue(fm["description"])
        corpo = texto.lower()
        for trecho in ("--briefing", "backticks", "do not poll", "fails without the change", "full suite",
                       "partial", "blocked", "human", "40 lines", "secrets", "serialize", "open codex", "abre o codex"):
            with self.subTest(trecho=trecho):
                self.assertIn(trecho, corpo)
        self.assertLess(len(texto.splitlines()), 500)

    def test_comandos(self):
        nomes = {p.stem for p in (PLUGIN / "commands").glob("*.md")}
        self.assertEqual(nomes, {"delegate", "orchestrator-status", "review-task", "codex"})
        for p in (PLUGIN / "commands").glob("*.md"):
            fm = frontmatter(p.read_text())
            self.assertTrue(fm.get("description"), p.name)
            for cmd in re.findall(r"Bash\(aao ([a-z-]+)", fm.get("allowed-tools", "")):
                self.assertIn(cmd, cli.parser()._subparsers._group_actions[0].choices, p.name)

    def test_painel_experimental_e_somente_leitura(self):
        mod = (RAIZ / "claude-plugin-pane" / "hooks" / "register.tsx").read_text()
        self.assertIn("EXPERIMENTAL", mod)
        self.assertIn("'--no-follow'", mod)
        for proibido in ("cancel', ", "'publish'", "'delegate'", "fs.write"):
            self.assertNotIn(proibido, mod)


if __name__ == "__main__":
    unittest.main()
