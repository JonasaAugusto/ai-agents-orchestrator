import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from ai_agents_orchestrator import engines
from ai_agents_orchestrator.engines import CodexRunner, ClaudeRunner

SP = ZoneInfo("America/Montevideo")


def epoch(*a):
    return int(datetime(*a, tzinfo=SP).timestamp())


class LimiteClaude(unittest.TestCase):
    # textos reais observados no transcript do Claude Code 2.1.x
    def test_sessao_hoje_e_amanha(self):
        agora = epoch(2026, 11, 5, 13, 0)
        self.assertEqual(engines.parse_reset_claude("You've hit your session limit · resets 2:40pm (America/Montevideo)", agora), epoch(2026, 11, 5, 14, 40))
        agora = epoch(2026, 11, 5, 17, 0)  # 6:20pm ainda hoje
        self.assertEqual(engines.parse_reset_claude("You've hit your session limit · resets 6:20pm (America/Montevideo)", agora), epoch(2026, 11, 5, 18, 20))
        agora = epoch(2026, 11, 5, 19, 0)  # ja passou: amanha
        self.assertEqual(engines.parse_reset_claude("resets 6:20pm (America/Montevideo)", agora), epoch(2026, 11, 6, 18, 20))

    def test_semanal_com_data(self):
        r = engines.parse_reset_claude("You've hit your weekly limit · resets Nov 9, 2am (America/Montevideo)", epoch(2026, 11, 8, 3, 0))
        self.assertEqual(r, epoch(2026, 11, 9, 2, 0))

    def test_detectar(self):
        l = engines.detectar_limite_claude("You've hit your weekly limit · resets Nov 9, 2am (America/Montevideo)", epoch(2026, 11, 8, 3, 0))
        self.assertEqual((l.tipo, l.reset_epoch), ("semanal", epoch(2026, 11, 9, 2, 0)))
        self.assertEqual(engines.detectar_limite_claude("You've hit your session limit · resets 2:40pm (America/Montevideo)").tipo, "sessao")
        self.assertIsNone(engines.detectar_limite_claude("API Error: 500 internal server error"))
        self.assertIsNone(engines.detectar_limite_claude("tudo certo"))


class LimiteCodex(unittest.TestCase):
    def test_variacoes_de_mensagem(self):
        for t in ("You've hit your usage limit. Upgrade to Plus to continue using Codex (https://chatgpt.com/explore/plus), or try again at Jul 13th,2026 6:58 PM.",
                  "Rate limit reached for gpt-5 on tokens per min. Please try again in 2.5s", "stream error: 429 Too Many Requests",
                  "insufficient_quota", "usage_limit_reached"):
            self.assertIsNotNone(engines.detectar_limite_codex(t), t)
        self.assertIsNone(engines.detectar_limite_codex("tests passed, 0 failures"))

    def test_reset_do_codex(self):
        agora = 1_800_000_000
        self.assertEqual(engines.parse_reset_codex("Try again in 1 day 23 hours 37 minutes", agora), agora + 86400 + 23 * 3600 + 37 * 60)
        self.assertEqual(engines.parse_reset_codex("try again at Jul 13th,2026 6:58 PM", agora), epoch(2026, 7, 13, 18, 58))
        self.assertIsNone(engines.parse_reset_codex("sem horario", agora))


def fake_runner(eventos, code=0, stderr="", final="", demora=False):
    def run(cmd, stdin, cwd, env, timeout, ctl, on_line=None):
        run.cmd, run.stdin, run.env, run.cwd = cmd, stdin, env, cwd
        if final:
            Path(cmd[cmd.index("-o") + 1]).write_text(final)
        linhas = [json.dumps(e) + "\n" for e in eventos]
        for l in linhas:
            if on_line:
                on_line(l)
        return code, linhas, stderr, demora
    return run


class RunnerCodex(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sd = Path(self.tmp.name)
        (self.sd / "spool").mkdir()

    def rodar(self, runner, **kw):
        return CodexRunner(self.sd, runner=runner).executar("faca x", self.sd, **kw)

    def test_sucesso_e_flags(self):
        r = fake_runner([{"type": "thread.started", "thread_id": "T1"}, {"type": "turn.completed"}], final="feito")
        res = self.rodar(r, sandbox="workspace-write", modelo="gpt-x")
        self.assertTrue(res.ok)
        self.assertEqual((res.texto, res.thread_id), ("feito", "T1"))
        c = r.cmd
        self.assertEqual(c[:4], ["codex", "-a", "never", "exec"])
        self.assertIn("sandbox_workspace_write.network_access=false", c)
        for flag in ("--json", "--skip-git-repo-check", "-s", "workspace-write", "-C", "-o", "-m", "gpt-x"):
            self.assertIn(flag, c)
        self.assertEqual(c[-1], "-")
        self.assertEqual(r.stdin, "faca x")  # prompt por stdin, nao em argv

    def test_somente_leitura_nao_liga_rede(self):
        r = fake_runner([{"type": "turn.completed"}], final="ok")
        self.rodar(r, sandbox="read-only")
        self.assertFalse(any("network_access" in a for a in r.cmd))

    def test_esforco_vai_em_config_e_tambem_na_retomada(self):
        for kw in ({}, {"resume": "T0"}):
            r = fake_runner([{"type": "turn.completed"}], final="ok")
            self.rodar(r, esforco="xhigh", **kw)
            i = r.cmd.index('model_reasoning_effort="xhigh"')
            self.assertEqual(r.cmd[i - 1], "-c")
        r = fake_runner([{"type": "turn.completed"}], final="ok")
        self.rodar(r)
        self.assertFalse(any("model_reasoning_effort" in a for a in r.cmd))

    def test_niveis_effort(self):
        from ai_agents_orchestrator.engines import niveis_effort
        cat = [{"slug": "a", "niveis": ["low", "high", "max"]}, {"slug": "b", "niveis": ["low", "high"]}]
        self.assertEqual(niveis_effort(cat, "a"), ["low", "high", "max"])
        self.assertEqual(niveis_effort(cat, None), ["low", "high"])
        self.assertEqual(niveis_effort([], None), ["low", "medium", "high", "xhigh"])

    def test_ambiente_dos_filhos_nao_tem_segredos(self):
        import os
        os.environ["AAO_TELEGRAM_TOKEN"] = "123456789:segredo-sintetico"
        os.environ["AAO_PUBLISH_KEY"] = "ab" * 16
        try:
            r = fake_runner([], final="ok")
            self.rodar(r)
            self.assertFalse(any("TELEGRAM" in k or "PUBLISH" in k or "PUBLICAR" in k for k in r.env))
            self.assertEqual(r.env["CODEX_HOME"], str(self.sd / ".codex"))
        finally:
            os.environ.pop("AAO_TELEGRAM_TOKEN"); os.environ.pop("AAO_PUBLISH_KEY")

    def test_resume_usa_config_em_vez_de_flags_ausentes(self):
        r = fake_runner([], final="ok")
        self.rodar(r, sandbox="read-only", resume="THR")
        self.assertEqual(r.cmd[4:6], ["resume", "THR"])
        self.assertNotIn("-C", r.cmd)
        self.assertIn('sandbox_mode="read-only"', r.cmd)

    def test_limite_de_uso(self):
        r = fake_runner([{"type": "error", "message": "You've hit your usage limit. try again at Jul 13th,2026 6:58 PM"},
                         {"type": "turn.failed", "error": {"message": "usage limit"}}], code=1)
        res = self.rodar(r)
        self.assertFalse(res.ok)
        self.assertIsNotNone(res.limite)
        self.assertEqual(res.limite.reset_epoch, epoch(2026, 7, 13, 18, 58))

    def test_erro_transitorio_com_sucesso_nao_e_limite(self):
        r = fake_runner([{"type": "error", "message": "429 retrying"}], code=0, final="ok")
        res = self.rodar(r)
        self.assertTrue(res.ok)
        self.assertIsNone(res.limite)

    def test_sem_login_timeout_e_cancelamento(self):
        res = self.rodar(fake_runner([{"type": "error", "message": "Not logged in, please run codex login"}], code=1))
        self.assertTrue(res.auth_erro)
        res = self.rodar(fake_runner([], code=-15, demora=True))
        self.assertTrue(res.timeout and not res.ok)

    def test_codex_nao_instalado(self):
        def run(*a, **k):
            raise FileNotFoundError
        self.assertEqual(self.rodar(run).erro, "codex not found")

    def test_timeout_real_mata_o_processo(self):
        res = engines._rodar(["sleep", "30"], "", None, {"PATH": "/usr/bin:/bin"}, 1, engines._Proc())
        self.assertTrue(res[3])


class RunnerClaude(unittest.TestCase):
    def test_limite_do_claude_via_headless(self):
        saida = json.dumps({"is_error": True, "result": "You've hit your session limit · resets 2:40pm (America/Montevideo)"})
        c = ClaudeRunner(Path("/tmp"), runner=lambda *a, **k: (1, [saida], "", False))
        res = c.executar("x", Path("/tmp"), agora=epoch(2026, 11, 5, 13, 0))
        self.assertFalse(res.ok)
        self.assertEqual(res.limite.reset_epoch, epoch(2026, 11, 5, 14, 40))

    def test_disponibilidade_exige_credencial(self):
        with tempfile.TemporaryDirectory() as d:
            c = ClaudeRunner(Path(d))
            self.assertFalse(c.disponivel())
            (Path(d) / ".claude").mkdir()
            (Path(d) / ".claude" / ".credentials.json").write_text("{}")
            self.assertTrue(c.disponivel())


class Usage(unittest.TestCase):
    def test_resumo_dos_limites(self):
        r = engines.resumir_limites({"rateLimits": {"planType": "plus", "primary": {"usedPercent": 12, "windowDurationMins": 300, "resetsAt": 1800000000},
                                                    "secondary": {"usedPercent": 40, "windowDurationMins": 10080, "resetsAt": 1800500000}},
                                     "ordinaryUsageAllowed": True})
        self.assertEqual((r["plano"], r["primaria"]["usado_pct"], r["secundaria"]["duracao_min"]), ("plus", 12, 10080))


if __name__ == "__main__":
    unittest.main()
