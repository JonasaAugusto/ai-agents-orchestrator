import unittest
from datetime import datetime
from unittest import mock

from ai_agents_orchestrator import cartao, engines
from ai_agents_orchestrator.config import fuso
TZ = fuso()
from tests.helpers import Base

AGORA = datetime(2026, 11, 9, 16, 25, tzinfo=TZ).timestamp()
RESET5H = datetime(2026, 11, 9, 18, 40, tzinfo=TZ).timestamp()
RESET7D = datetime(2026, 11, 15, 9, 5, tzinfo=TZ).timestamp()  # Sunday
COMPLETO = {"ok": True, "plano": "plus", "primaria": {"usado_pct": 32.4, "duracao_min": 300, "reseta_em": RESET5H},
            "secundaria": {"usado_pct": 71, "duracao_min": 10080, "reseta_em": RESET7D}, "limite_atingido": None}


class Cartao(unittest.TestCase):
    def test_completo(self):
        t = cartao.cartao_usage(COMPLETO, AGORA, {"modelo": "gpt-x", "esforco": "high", "acordadas": 2, "teto": 6,
                                                   "tarefas_hora": 1, "teto_tarefas": 6})
        for trecho in ("<b>📊 Codex usage</b>", "Plan: <b>Plus</b>", "5-hour window", "🟢 ▰▰▰▱▱▱▱▱▱▱ <b>32%</b> used · 68% left",
                       "Resets in 2 h 15 min (at 18:40)", "Weekly window", "🟡", "<b>71%</b>",
                       "Resets in 5 d 16 h (Sun, 2026-11-15 at 09:05)", "<b>🤖 Bot</b>", "Model: gpt-x · Effort: high",
                       "Limit checks today: 2/6", "Tasks in the last hour: 1/6"):
            self.assertIn(trecho, t)
        for ruim in ("None", "{", "🔴", "🔵"):
            self.assertNotIn(ruim, t)
        self.assertLess(len(t.splitlines()), 20)

    def test_parcial_omite_linhas_sem_dado(self):
        t = cartao.cartao_usage({"ok": True, "primaria": {"usado_pct": 90, "duracao_min": 300, "reseta_em": None},
                                 "secundaria": None, "plano": None}, AGORA, {})
        self.assertIn("🟠", t)
        self.assertIn("<b>90%</b>", t)
        self.assertNotIn("Resets", t)
        self.assertNotIn("Plan", t)
        self.assertNotIn("Bot", t)
        self.assertNotIn("None", t)

    def test_semaforo_nos_limites(self):
        self.assertEqual([cartao.semaforo(p) for p in (0, 60, 61, 85, 86, 100)], ["🟢", "🟢", "🟡", "🟡", "🟠", "🟠"])

    def test_escapa_html(self):
        t = cartao.cartao_usage({"ok": True, "plano": "<b>x&y</b>"}, AGORA, {"modelo": "a<b"})
        self.assertIn("&lt;b&gt;x&amp;y&lt;/b&gt;", t)
        self.assertIn("a&lt;b", t)

    def test_erro_curto_sem_cru(self):
        t = cartao.cartao_erro("no answer")
        self.assertIn("did not answer", t)
        self.assertNotIn("no answer", t)


class UsageNoBot(Base):
    def botoes_editados(self):
        ed = [p for m, p in self.tr.chamadas if m == "editMessageText"][-1]
        return [(b["text"], b["callback_data"]) for linha in ed["reply_markup"]["inline_keyboard"] for b in linha]

    def test_cartao_html_com_botao_atualizar_e_hmac(self):
        with mock.patch.object(engines, "uso_codex", lambda *a, **k: COMPLETO):
            self.bot.processar(self.msg("/usage"))
        ed = [p for m, p in self.tr.chamadas if m == "editMessageText"][-1]
        self.assertEqual(ed["parse_mode"], "HTML")
        self.assertIn("5-hour window", ed["text"])
        self.assertEqual([r for r, _ in self.botoes_editados()], ["Refresh"])
        self.assertIn(":", ed["reply_markup"]["inline_keyboard"][0][0]["callback_data"])

    def test_atualizar_ignora_cache_e_e_de_uso_unico(self):
        n = []
        with mock.patch.object(engines, "uso_codex", lambda *a, **k: n.append(1) or COMPLETO):
            self.bot.processar(self.msg("/usage"))
            data = dict(self.botoes_editados())["Refresh"]
            self.bot.processar(self.clique(data))
            self.bot._ultimo_lento.result(5)
            self.assertEqual(len(n), 2)  # mesmo dentro dos 60 s
            self.bot.processar(self.clique(data))  # replay
            self.assertEqual(len(n), 2)

    def test_erro_mostra_mensagem_curta_e_tentar_de_novo(self):
        with mock.patch.object(engines, "uso_codex", lambda *a, **k: {"ok": False, "erro": "no answer"}):
            self.bot.processar(self.msg("/usage"))
        ed = [p for m, p in self.tr.chamadas if m == "editMessageText"][-1]
        self.assertIn("could not read the usage", ed["text"])
        self.assertEqual([r for r, _ in self.botoes_editados()], ["Try again"])

    def test_historico_sem_tags(self):
        with mock.patch.object(engines, "uso_codex", lambda *a, **k: COMPLETO):
            self.bot.processar(self.msg("/usage"))
        from ai_agents_orchestrator import historico
        regs = historico.consultar(self.svc.hist.caminho, 10)
        self.assertTrue(any("Codex usage" in r.get("texto", "") for r in regs))
        self.assertFalse(any("<b>" in r.get("texto", "") for r in regs))


if __name__ == "__main__":
    unittest.main()
