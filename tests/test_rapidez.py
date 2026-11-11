import socket
import threading
import time
import unittest
from unittest import mock

from ai_agents_orchestrator import engines, telegram
from tests.helpers import Base, CHAT


class IPv4Forcado(unittest.TestCase):
    def test_resolve_so_ipv4_e_nunca_tenta_v6(self):
        visto = []

        def gai(host, port, fam=0, tipo=0, *a, **k):
            visto.append(fam)
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.10", 443))]

        class Sock:
            def __init__(self, *a): pass
            def settimeout(self, t): pass
            def connect(self, addr): self.addr = addr
            def setsockopt(self, *a): pass
            def close(self): pass

        con = telegram._ConexaoIPv4("api.telegram.org", 443, timeout=5)
        con._context = mock.Mock()
        with mock.patch("socket.getaddrinfo", gai), mock.patch("socket.socket", Sock):
            con.connect()
        self.assertEqual(visto, [socket.AF_INET])

    def test_timeouts_padrao(self):
        tempos = []
        tg = telegram.Telegram("1:x" * 5, CHAT, transporte=lambda m, p, t: tempos.append((m, t)) or {"ok": True, "result": []})
        tg.chamar("sendMessage")
        tg.atualizacoes(0)
        self.assertEqual(tempos, [("sendMessage", 10), ("getUpdates", 35)])


class Rapidez(Base):
    def esperar(self, cond, t=3):
        fim = time.time() + t
        while time.time() < fim and not cond():
            time.sleep(0.01)
        return cond()

    def test_comando_lento_nao_trava_o_polling_nem_os_rapidos(self):
        liberar = threading.Event()
        with mock.patch.object(engines, "uso_codex", lambda *a, **k: liberar.wait(5) and {"ok": True, "plano": "x"}):
            t0 = time.time()
            self.bot.despachar(self.msg("/usage"))   # volta na hora
            self.assertLess(time.time() - t0, 0.5)
            self.bot.despachar(self.msg("/ajuda"))
            self.assertTrue(self.esperar(lambda: any("Commands" in t for t in self.tr.textos())))
            self.assertTrue(any("checking" in t for t in self.tr.textos()))  # resposta imediata
            self.assertFalse(any("Plan" in p.get("text", "") for m, p in self.tr.chamadas if m == "editMessageText"))
            liberar.set()
            self.assertTrue(self.esperar(lambda: any(m == "editMessageText" for m, _ in self.tr.chamadas)))

    def test_usage_edita_a_mensagem_de_espera(self):
        with mock.patch.object(engines, "uso_codex", lambda *a, **k: {"ok": True, "plano": "plus"}):
            self.bot.processar(self.msg("/usage"))
        envio = [i for i, (m, _p) in enumerate(self.tr.chamadas) if m == "sendMessage"]
        ed = [p for m, p in self.tr.chamadas if m == "editMessageText"]
        self.assertEqual(len(envio), 1)
        self.assertIn("Plus", ed[0]["text"])

    def test_cache_de_60s_para_usage_e_model(self):
        n = []
        with mock.patch.object(engines, "uso_codex", lambda *a, **k: n.append(1) or {"ok": True}), \
                mock.patch.object(engines, "modelos_codex", lambda *a, **k: n.append(2) or [{"slug": "m", "nome": "M"}]):
            for _ in range(2):
                self.bot.processar(self.msg("/usage"))
                self.bot.processar(self.msg("/model"))
            self.assertEqual(n, [1, 2])
            self.t[0] += 61
            self.bot.processar(self.msg("/usage"))
        self.assertEqual(n, [1, 2, 1])

    def test_erro_no_uso_nao_fica_em_cache(self):
        n = []
        with mock.patch.object(engines, "uso_codex", lambda *a, **k: n.append(1) or {"ok": False, "erro": "x"}):
            self.bot.processar(self.msg("/usage"))
            self.bot.processar(self.msg("/usage"))
        self.assertEqual(len(n), 2)

    def test_faixa_rapida_mantem_a_ordem(self):
        self.bot.despachar(self.msg("/pausar"))
        self.bot.despachar(self.msg("/retomar"))
        self.assertTrue(self.esperar(lambda: len(self.botoes_enviados()) >= 4))
        self.assertEqual([t for t in self.tr.textos()][:2], ["Pause the queue (the current task finishes)?", "Resume the Codex queue?"])

    def test_callback_responde_antes_de_executar(self):
        self.bot.processar(self.msg("/pausar"))
        data = dict(self.botoes_enviados())["Confirm"]
        n = len(self.tr.chamadas)
        self.bot.processar(self.clique(data))
        self.assertEqual(self.tr.chamadas[n][0], "answerCallbackQuery")

    def test_edicao_que_falha_envia_mensagem_nova(self):
        antigo = self.tr.__call__

        def t(m, p, to):
            if m == "editMessageText":
                raise telegram.TelegramErro("x")
            return antigo(m, p, to)
        self.tg._t = t
        self.svc.editar(7, "oi")
        self.assertEqual(self.tr.textos(), ["oi"])


if __name__ == "__main__":
    unittest.main()
