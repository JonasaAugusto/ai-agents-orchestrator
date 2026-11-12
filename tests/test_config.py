"""Single config file (TOML) + env overrides, safe defaults, explicit opt-ins."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from ai_agents_orchestrator import config, engines, garantias
from ai_agents_orchestrator.config import Config

RAIZ = Path(__file__).resolve().parents[1]


class Padroes(unittest.TestCase):
    def test_padroes_seguros(self):
        c = Config()
        self.assertFalse(c.codex_full_access)
        self.assertFalse(c.codex_network)
        self.assertFalse(c.claude_wakeup)
        self.assertFalse(c.whatsapp_accept_risk)
        self.assertTrue(c.mask_dev_secrets)
        self.assertEqual(c.push_allowlist, [])
        self.assertEqual(c.chat_id, 0)
        self.assertEqual(c.channel, "none")
        self.assertEqual(c.repos, {})

    def test_politica_de_gasto_padroes(self):
        c = Config()
        self.assertEqual(c.max_codex_simultaneos, 1)
        self.assertEqual(c.balance_alert_high, 70.0)
        self.assertEqual(config.carregar(env={}).max_codex_simultaneos, 1)

    def test_skill_e_readmes_documentam_politica_de_gasto(self):
        skill = (RAIZ / "claude-plugin/skills/orchestrator-workflow/SKILL.md").read_text()
        self.assertIn("Spend policy", skill)
        self.assertTrue((RAIZ / "claude-plugin/skills/orchestrator-workflow/spend-policy.md").exists())
        self.assertIn("## Spend policy", (RAIZ / "README.md").read_text())
        self.assertIn("## Política de gasto", (RAIZ / "README.pt-BR.md").read_text())

    def test_comando_do_codex_por_padrao_tem_sandbox_e_sem_rede(self):
        r = engines.CodexRunner(Path("/tmp"))
        cmd = r.comando("/w", "workspace-write", "/o")
        self.assertEqual(cmd[cmd.index("-s") + 1], "workspace-write")
        self.assertIn("sandbox_workspace_write.network_access=false", cmd)
        self.assertNotIn("danger-full-access", " ".join(cmd))
        cap = garantias.capacidades("workspace-write", None, None)
        self.assertEqual((cap["sandbox"], cap["rede"]), ("workspace-write", "no"))

    def test_opt_ins_explicitos(self):
        r = engines.CodexRunner(Path("/tmp"), rede=True, acesso_total=True)
        self.assertIn("danger-full-access", " ".join(r.comando("/w", "workspace-write", "/o")))
        self.assertNotIn("danger-full-access", " ".join(r.comando("/w", "read-only", "/o")))
        r = engines.CodexRunner(Path("/tmp"), rede=True)
        self.assertIn("sandbox_workspace_write.network_access=true", r.comando("/w", "workspace-write", "/o"))
        cap = garantias.capacidades("workspace-write", None, None, rede_config=True)
        self.assertEqual(cap["rede"], "yes")


@unittest.skipIf(os.name == "nt", "legacy full-mode TOML/channel examples; native lite config is covered in test_windows")
class Carregar(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = Path(self.tmp.name)
        self.addCleanup(config.definir_fuso, "America/Montevideo")

    def toml(self, texto):
        p = self.d / "config.toml"
        p.write_text(texto, encoding="utf-8")
        return p

    def test_exemplo_documentado_carrega(self):
        c = config.carregar({"AAO_CONFIG": str(RAIZ / "examples" / "config.example.toml")})
        self.assertEqual(c.channel, "telegram")
        self.assertFalse(c.codex_full_access)
        self.assertTrue(c.repos)
        for spec in c.repos.values():
            self.assertIn("branch_base", spec)

    def test_toml_e_env(self):
        p = self.toml('[service]\nchannel = "telegram"\ntimezone = "Europe/Lisbon"\ntasks_per_hour = 3\n'
                      '[telegram]\nchat_id = 99\n[codex]\nnetwork_access = true\n'
                      '[publish]\npush_allowlist = ["o/a"]\n[repos.app]\ngithub = "o/a"\nbase_branch = "dev"\n')
        c = config.carregar({"AAO_CONFIG": str(p), "AAO_TELEGRAM_TOKEN": "1:x", "AAO_SERVICE_TASKS_PER_HOUR": "4"})
        self.assertEqual((c.channel, c.chat_id, c.token, c.tarefas_por_hora), ("telegram", 99, "1:x", 4))
        self.assertTrue(c.codex_network)
        self.assertEqual(c.push_allowlist, ["o/a"])
        self.assertEqual(c.repos["app"]["branch_base"], "dev")
        self.assertEqual(config.nome_fuso(), "Europe/Lisbon")

    def test_valores_invalidos_caem_no_padrao(self):
        p = self.toml('[service]\nchannel = "irc"\ntimezone = "Not/AZone"\ntasks_per_hour = "x"\n')
        config.definir_fuso("UTC")
        c = config.carregar({"AAO_CONFIG": str(p)})
        self.assertEqual(c.channel, "none")
        self.assertEqual(c.tarefas_por_hora, 6)
        self.assertEqual(config.nome_fuso(), "UTC")

    def test_whatsapp_usa_o_numero_do_dono_como_id(self):
        p = self.toml('[service]\nchannel = "whatsapp"\n[whatsapp]\nowner_number = "+1 (555) 010-0000"\n')
        c = config.carregar({"AAO_CONFIG": str(p)})
        self.assertEqual(c.chat_id, 15550100000)
        self.assertFalse(c.whatsapp_accept_risk)

    def test_arquivo_ausente_ou_quebrado_usa_padroes(self):
        p = self.toml("isto nao e toml = = =")
        self.assertEqual(config.carregar({"AAO_CONFIG": str(p)}).repos, {})
        self.assertEqual(config.carregar({"AAO_CONFIG": str(self.d / "nao-existe.toml")}).channel, "none")


class Empacotamento(unittest.TestCase):
    def test_versao_e_console_script(self):
        from ai_agents_orchestrator import __version__
        texto = (RAIZ / "pyproject.toml").read_text()
        self.assertIn(f'version = "{__version__}"', texto)
        self.assertIn('aao = "ai_agents_orchestrator.cli:main"', texto)
        self.assertIn(f"## [{__version__}]", (RAIZ / "CHANGELOG.md").read_text())

    def test_help_do_cli_em_ingles(self):
        env = dict(os.environ, PYTHONPATH=str(RAIZ / "src"))
        r = subprocess.run([os.sys.executable, "-m", "ai_agents_orchestrator", "--help"], capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0)
        for cmd in ("delegate", "watch", "doctor", "setup", "publish", "review"):
            self.assertIn(cmd, r.stdout)


if __name__ == "__main__":
    unittest.main()
