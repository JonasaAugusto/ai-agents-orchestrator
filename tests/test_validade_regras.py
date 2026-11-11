"""Expiry warns without hiding other problems; dates are always injected."""
import contextlib
import io
import tempfile
import unittest
import warnings
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import mock

from ai_agents_orchestrator import cli, config, garantias, prompts
from ai_agents_orchestrator.config import Config
from tests.helpers import Base
from tests.test_regras_consistencia import problemas_regras

REGRAS = "# Rules\nValid from: 2026-11-09; review by: 2026-12-09.\n"
AVISO = "rules expired, please review"


class ValidadeConsistencia(unittest.TestCase):
    def test_vencimento_so_avisa_apos_o_dia_da_revisao(self):
        with tempfile.TemporaryDirectory() as tmp:
            for hoje in (date(2026, 12, 8), date(2026, 12, 9), date(2026, 12, 10)):
                with self.subTest(hoje=hoje), warnings.catch_warnings(record=True) as avisos:
                    warnings.simplefilter("always")
                    self.assertEqual(problemas_regras(REGRAS, Path(tmp), hoje), [])
                    self.assertEqual(len(avisos), int(hoje > date(2026, 12, 9)))
                    if avisos:
                        self.assertIn(AVISO, str(avisos[0].message))
                        self.assertIn("2026-12-09", str(avisos[0].message))

    def test_vencimento_nao_oculta_outros_erros(self):
        with tempfile.TemporaryDirectory() as tmp:
            for extra, erro in (("rule\n" * 61, "more than 60 lines"),
                                ("a" * (32 * 1024), "more than 32 KiB"),
                                ("ausente.py", "path does not exist: ausente.py"),
                                ("Full access to /root. Never read /root.", "contradictory access: /root")):
                with self.subTest(erro=erro), self.assertWarnsRegex(UserWarning, AVISO):
                    self.assertEqual(problemas_regras(REGRAS + extra, Path(tmp), date(2026, 12, 10)), [erro])


class ValidadeComando(unittest.TestCase):
    def test_comando_real_avisa_e_preserva_falhas_de_consistencia(self):
        with tempfile.TemporaryDirectory() as tmp:
            arquivo = Path(tmp) / "BASE_RULES.md"
            texto = REGRAS.replace("2026-12-09", "2000-01-01")
            for hoje, inconsistente, codigo in ((date(2000, 1, 1), False, 0),
                                                (date(2000, 1, 2), False, 0),
                                                (date(2000, 1, 2), True, 1)):
                with self.subTest(hoje=hoje, inconsistente=inconsistente):
                    arquivo.write_text(texto + ("See inexistente.py.\n" if inconsistente else ""), encoding="utf-8")
                    saida = io.StringIO()
                    with contextlib.redirect_stdout(saida):
                        retorno = cli.verificar_regras(arquivo, hoje=hoje, templates=Path(tmp) / "none")
                    self.assertEqual(retorno, codigo)
                    if hoje > date(2000, 1, 1):
                        self.assertIn(AVISO, saida.getvalue())
                        self.assertIn("2000-01-01", saida.getvalue())
                    else:
                        self.assertNotIn(AVISO, saida.getvalue())

    def test_templates_longos_falham_no_comando(self):
        with tempfile.TemporaryDirectory() as tmp:
            arquivo = Path(tmp) / "BASE_RULES.md"
            arquivo.write_text(REGRAS, encoding="utf-8")
            (Path(tmp) / "t").mkdir()
            (Path(tmp) / "t" / "AGENTS.x.md").write_text("l\n" * 41)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.verificar_regras(arquivo, hoje=date(2026, 11, 9), templates=Path(tmp) / "t"), 1)


class ValidadeSnapshot(unittest.TestCase):
    def test_snapshot_avisa_na_virada_do_dia_no_fuso_configurado(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(prompts, "regras_base", return_value=REGRAS):
            cfg = Config(state_dir=Path(tmp), repos={})
            for hora, minuto, segundo, vencidas in ((2, 59, 59, False), (3, 0, 0, True)):  # 03:00 UTC = 00:00 at UTC-3
                agora = datetime(2026, 12, 10, hora, minuto, segundo, tzinfo=timezone.utc).timestamp()
                with self.subTest(vencidas=vencidas):
                    snap = garantias.montar_snapshot(cfg, agora, sandbox="read-only", modelo=None, esforco=None,
                                                     briefing_texto="Updated: 2026-12-09 23:00")
                    self.assertEqual(any(AVISO in a for a in snap["avisos"]), vencidas)
                    self.assertEqual(AVISO in snap["texto"], vencidas)
                    self.assertEqual(snap["briefing"]["avisos"], [])
                    if vencidas:
                        self.assertIn("2026-12-09", snap["texto"])


class ValidadePromptServico(Base):
    def test_servico_injeta_aviso_em_cada_turno_sem_bloquear(self):
        self.t[0] = datetime(2026, 12, 10, tzinfo=config.fuso()).timestamp()
        with mock.patch.object(prompts, "regras_base", return_value=REGRAS):
            for texto in ("oi", "continue"):
                self.bot.processar(self.msg(texto))
                self.assertIn(AVISO, self.codex.chamadas[-1]["prompt"])
        self.assertEqual(len(self.codex.chamadas), 2)


if __name__ == "__main__":
    unittest.main()
