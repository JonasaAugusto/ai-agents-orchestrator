"""Test suite. The tests run in a non-UTC timezone on purpose (day boundaries, briefing timestamps)."""
import os

os.environ.pop("AAO_CONFIG", None)
os.environ["AAO_CONFIG"] = os.devnull  # never read a real /etc config during tests
os.environ["AAO_SERVICE_TIMEZONE"] = "America/Montevideo"  # also for code paths that call config.carregar()

from ai_agents_orchestrator import config  # noqa: E402

config.definir_fuso("America/Montevideo")

import unittest  # noqa: E402

_ZONA_DOS_TESTES = "America/Montevideo"
_run_original = unittest.TestCase.run


def _run_com_fuso_fixo(self, result=None):
    """Every test starts and ends in the suite zone: config.carregar()/aplicar() changes the process-wide zone, so a
    test that loads a config would otherwise leak UTC into whichever test runs next."""
    config.definir_fuso(_ZONA_DOS_TESTES)
    try:
        return _run_original(self, result)
    finally:
        config.definir_fuso(_ZONA_DOS_TESTES)


unittest.TestCase.run = _run_com_fuso_fixo
