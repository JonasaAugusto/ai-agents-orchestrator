"""Adoption checks with disposable paths; no accounts, services or network."""
import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ai_agents_orchestrator import assistente, cli, config

ROOT = Path(__file__).resolve().parents[1]


class UnattendedSetup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dest = Path(self.tmp.name) / "demo"

    def setup_cli(self, *args):
        with contextlib.redirect_stdout(io.StringIO()), mock.patch("builtins.input", side_effect=AssertionError("prompt")):
            return cli.main(["setup", "--non-interactive", *args])

    def test_writes_isolated_restricted_config_without_secrets(self):
        self.assertEqual(self.setup_cli("--output-dir", str(self.dest), "--repo-source", str(ROOT)), 0)
        cfg = config.carregar(env={}, arquivo=self.dest / "config.toml")
        self.assertEqual(cfg.channel, "none")
        self.assertEqual(cfg.push_allowlist, [])
        self.assertFalse(cfg.codex_network)
        self.assertFalse(cfg.codex_full_access)
        self.assertFalse(cfg.codex_full_access_read_only)
        self.assertFalse(cfg.claude_wakeup)
        for attr in ("state_dir", "root_state", "fonte_transcripts", "fonte_memoria", "claude_sessions_dir",
                     "rules_file", "division_file", "templates_dir", "socket", "root_socket",
                     "publish_manifest", "publish_key_file"):
            self.assertTrue(getattr(cfg, attr).is_relative_to(self.dest), attr)
        self.assertEqual(cfg.repos["demo"]["fonte"], str(ROOT))
        self.assertTrue(cfg.repos["demo"]["local"])
        self.assertEqual((self.dest / "config.toml").stat().st_mode & 0o777, 0o600)
        self.assertEqual({p.name for p in self.dest.iterdir()}, {"config.toml"})

    def test_windows_path_needs_neither_pwd_nor_posix_directory_flags(self):
        # Em Windows nativo não existem `pwd`, os.geteuid, O_DIRECTORY nem O_NOFOLLOW.
        with mock.patch.dict("sys.modules", {"pwd": None}), mock.patch.object(assistente.compat, "WINDOWS", True), \
                mock.patch.object(assistente.compat, "private") as private:
            self.assertEqual(self.setup_cli("--output-dir", str(self.dest)), 0)
        private.assert_called()
        self.assertTrue((self.dest / "config.toml").is_file())

    def test_output_directory_required_no_default_write(self):
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors), self.assertRaises(SystemExit) as raised:
            self.setup_cli()
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("--output-dir is required", errors.getvalue())
        self.assertFalse(self.dest.exists())

    def test_refuses_existing_contents_and_symlink(self):
        self.dest.mkdir()
        marker = self.dest / "keep.txt"
        marker.write_text("keep")
        self.assertEqual(self.setup_cli("--output-dir", str(self.dest)), 1)
        self.assertEqual(marker.read_text(), "keep")
        link = self.dest.parent / "link"
        link.symlink_to(self.dest, target_is_directory=True)
        self.assertEqual(self.setup_cli("--output-dir", str(link)), 1)
        self.assertEqual(list(self.dest.iterdir()), [marker])

    def test_refuses_shared_writable_directory(self):
        self.dest.mkdir(mode=0o777)
        self.dest.chmod(0o777)
        self.assertEqual(self.setup_cli("--output-dir", str(self.dest)), 1)
        self.assertEqual(list(self.dest.iterdir()), [])

    def test_never_overwrites_file_inserted_after_empty_check(self):
        target = self.dest.parent / "keep.txt"
        target.write_text("keep")
        original = assistente.gerar_toml

        def insert_file(answers):
            (self.dest / "config.toml").symlink_to(target)
            return original(answers)

        with mock.patch.object(assistente, "gerar_toml", side_effect=insert_file):
            self.assertEqual(self.setup_cli("--output-dir", str(self.dest)), 1)
        self.assertTrue((self.dest / "config.toml").is_symlink())
        self.assertEqual(target.read_text(), "keep")


class PluginValidation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for directory in ("claude-plugin", "claude-plugin-pane", ".claude-plugin"):
            shutil.copytree(ROOT / directory, self.root / directory)

    def validate(self):
        from scripts.clean_install_test import validate_plugin
        validate_plugin(self.root)

    def test_accepts_repository_plugin(self):
        self.validate()

    def test_rejects_missing_skill_frontmatter(self):
        (self.root / "claude-plugin/skills/orchestrator-workflow/SKILL.md").write_text("# No metadata\n")
        with self.assertRaisesRegex(ValueError, "frontmatter"):
            self.validate()

    def test_rejects_broken_quoted_frontmatter(self):
        (self.root / "claude-plugin/skills/orchestrator-workflow/SKILL.md").write_text(
            '---\nname: orchestrator-workflow\ndescription: "unfinished\n---\n# Skill\n')
        with self.assertRaisesRegex(ValueError, "frontmatter"):
            self.validate()

    def test_rejects_unknown_hook_command(self):
        path = self.root / "claude-plugin/hooks/hooks.json"
        hooks = json.loads(path.read_text())
        hooks["hooks"]["Stop"][0]["hooks"][0]["command"] = "aao invented-command"
        path.write_text(json.dumps(hooks))
        with self.assertRaisesRegex(ValueError, "hook command"):
            self.validate()

    def test_rejects_missing_manifest(self):
        (self.root / "claude-plugin/.claude-plugin/plugin.json").unlink()
        with self.assertRaises((ValueError, FileNotFoundError)):
            self.validate()

    def test_rejects_marketplace_escape(self):
        path = self.root / ".claude-plugin/marketplace.json"
        data = json.loads(path.read_text())
        data["plugins"][0]["source"] = "../outside"
        path.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, "marketplace"):
            self.validate()


class CleanEnvironment(unittest.TestCase):
    def test_drops_credentials_and_config_overrides(self):
        from scripts.clean_install_test import isolated_environment
        with mock.patch.dict(os.environ, {"AAO_CONFIG": "/do-not-read", "OPENAI_API_KEY": "fake-key",
                                          "PYTHONPATH": "/do-not-import", "GIT_CONFIG_GLOBAL": "/do-not-read"}):
            env = isolated_environment(Path("/tmp/demo"), Path("/tmp/venv/bin"))
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertNotIn("PYTHONPATH", env)
        self.assertEqual(env["GIT_CONFIG_GLOBAL"], os.devnull)
        self.assertEqual(env["AAO_CONFIG"], "/tmp/demo/config.toml")

    def test_doctor_rejects_unexpected_failure_and_exit(self):
        from scripts.clean_install_test import check_doctor
        items = [{"nome": name, "estado": "FAIL"} for name in
                 ("codex CLI", "service socket", "root daemon socket", "service user", "repositories")]
        for code, data in ((0, items), (1, items + [{"nome": "usage schema", "estado": "FAIL"}])):
            with self.subTest(code=code), mock.patch("subprocess.run", return_value=mock.Mock(returncode=code, stdout=json.dumps(data))):
                with self.assertRaisesRegex(RuntimeError, "unexpected doctor result"):
                    check_doctor("aao", Path("/tmp"), {}, configured=False)

    def test_offline_example_delivers_patch_without_publishing(self):
        from examples.offline_relay import run_demo
        with tempfile.TemporaryDirectory() as temp, contextlib.redirect_stdout(io.StringIO()):
            result = run_demo(Path(temp))
        self.assertEqual(result["tarefa"]["estado"], "em_revisao")
        self.assertIn("fake Codex", result["tarefa"]["resumo"])


if __name__ == "__main__":
    unittest.main()
