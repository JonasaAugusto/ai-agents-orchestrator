"""Test suite. The tests run in a non-UTC timezone on purpose (day boundaries, briefing timestamps)."""
import os

os.environ.pop("AAO_CONFIG", None)
os.environ["AAO_CONFIG"] = os.devnull  # never read a real /etc config during tests
os.environ["AAO_SERVICE_TIMEZONE"] = "America/Montevideo"  # also for code paths that call config.carregar()

from ai_agents_orchestrator import config  # noqa: E402

config.definir_fuso("America/Montevideo")
