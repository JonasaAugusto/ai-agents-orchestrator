"""codex.default_effort: --effort > runtime state > config key > Codex default."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ai_agents_orchestrator import config, engines
from ai_agents_orchestrator.config import Config
from tests.helpers import Base

MODS = [{"slug": "m", "nome": "M", "niveis": ["low", "medium", "high", "xhigh"], "padrao": "medium"}]


class Chave(unittest.TestCase):
    def carregar(self, toml):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.toml"
            p.write_text(toml)
            return config.carregar({"AAO_CONFIG": str(p)})

    def test_padrao_e_medium(self):
        self.assertEqual(Config().codex_default_effort, "medium")

    def test_le_do_toml_e_normaliza(self):
        self.assertEqual(self.carregar('[codex]\ndefault_effort = " HIGH "\n').codex_default_effort, "high")

    def test_valor_invalido_volta_para_medium(self):
        self.assertEqual(self.carregar('[codex]\ndefault_effort = "turbo"\n').codex_default_effort, "medium")


class Precedencia(Base):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(engines, "modelos_codex", lambda *a, **k: MODS)
        p.start()
        self.addCleanup(p.stop)

    def test_config_vale_sem_estado(self):
        self.assertEqual(self.svc.esforco_atual(), "medium")
        self.cfg.codex_default_effort = "high"
        self.assertEqual(self.svc.esforco_atual(), "high")

    def test_estado_vence_a_config(self):
        self.cfg.codex_default_effort = "high"
        self.estado.set("esforco", "low")
        self.assertEqual(self.svc.esforco_atual(), "low")

    def test_nivel_que_o_modelo_nao_aceita_cai_no_padrao_do_codex(self):
        self.cfg.codex_default_effort = "max"
        self.assertIsNone(self.svc.esforco_atual())

    def test_tarefa_com_effort_vence_tudo(self):
        self.criar_repo()
        self.cfg.repos["web-app"]["fonte"] = str(self.dir / "src")
        self.cfg.codex_default_effort = "high"
        self.estado.set("esforco", "low")
        self.svc.delegar("web-app", "change", esforco="xhigh")
        self.assertTrue(self.svc.ciclo_worker())
        self.assertEqual(self.codex.chamadas[0]["esforco"], "xhigh")

    def test_tarefa_sem_effort_usa_a_chave(self):
        self.criar_repo()
        self.cfg.repos["web-app"]["fonte"] = str(self.dir / "src")
        self.cfg.codex_default_effort = "high"
        self.svc.delegar("web-app", "change")
        self.assertTrue(self.svc.ciclo_worker())
        self.assertEqual(self.codex.chamadas[0]["esforco"], "high")


if __name__ == "__main__":
    unittest.main()
