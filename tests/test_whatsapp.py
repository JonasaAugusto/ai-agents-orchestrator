"""Unofficial WhatsApp channel, tested only with fakes (no network, no library)."""
import unittest
import re

from ai_agents_orchestrator.bot import Bot
from ai_agents_orchestrator.servico import Servico
from ai_agents_orchestrator.whatsapp import Backoff, LimiteEnvio, WhatsApp, WhatsAppErro
from tests.helpers import Base, _Sincrono

DONO = "15550100000"


class ClienteFalso:
    def __init__(self):
        self.enviadas = []
        self.conexoes = 0
        self.cb = self.fatal = None
        self.falhar_envio = False
        self.ao_conectar = None

    def on_message(self, cb): self.cb = cb
    def on_fatal(self, cb): self.fatal = cb

    def send(self, numero, texto):
        if self.falhar_envio:
            raise RuntimeError("lib error with +15550100000 inside")
        self.enviadas.append((numero, texto))

    def connect(self):
        self.conexoes += 1
        if self.ao_conectar:
            self.ao_conectar(self.conexoes)

    def disconnect(self): pass

    def chega(self, texto, de=DONO, de_mim=False, grupo=False):
        self.cb([de], texto, de_mim, grupo)


class Relogio:
    def __init__(self): self.t = 1000.0; self.dormidas = []
    def __call__(self): return self.t
    def dormir(self, s): self.dormidas.append(s); self.t += s


class Canal(unittest.TestCase):
    def setUp(self):
        self.cli = ClienteFalso()
        self.rel = Relogio()
        self.wa = WhatsApp(DONO, self.cli, limite=LimiteEnvio(5, 1.0, self.rel, self.rel.dormir), relogio=self.rel,
                           dormir=self.rel.dormir)
        self.wa._fio = object()  # never start the real connection thread in tests

    def test_so_envia_ao_dono(self):
        self.wa.enviar("oi")
        self.assertEqual(self.cli.enviadas, [(DONO, "oi")])
        self.assertTrue(self.wa.permitido(int(DONO)))
        self.assertFalse(self.wa.permitido(123))

    def test_ignora_outros_grupos_e_eco(self):
        self.cli.chega("oi", de="5511999990000")
        self.cli.chega("oi", grupo=True)
        self.cli.chega("oi", de_mim=True)
        self.assertEqual(self.wa.atualizacoes(0, timeout=0), [])
        self.cli.chega("status?")
        u = self.wa.atualizacoes(0, timeout=0)
        self.assertEqual(len(u), 1)
        m = u[0]["message"]
        self.assertEqual((m["chat"]["id"], m["from"]["id"], m["chat"]["type"], m["text"]), (int(DONO), int(DONO), "private", "status?"))

    def test_botoes_viram_opcoes_numeradas_e_resposta_vira_callback(self):
        self.wa.enviar("Publish?", [[("Publish", "a1:mac1"), ("Review first", "a2:mac2")], [("Don't publish", "a3:mac3")]])
        texto = self.cli.enviadas[-1][1]
        self.assertIn("1. Publish", texto)
        self.assertIn("3. Don't publish", texto)
        self.cli.chega(re.search(r"[a-f0-9]{8}:2", texto).group(0))
        u = self.wa.atualizacoes(0, timeout=0)[0]
        self.assertEqual(u["callback_query"]["data"], "a2:mac2")
        self.cli.chega("2")  # options are consumed: a second "2" is plain text
        self.assertIn("message", self.wa.atualizacoes(0, timeout=0)[0])

    def test_limite_de_envio_sem_dormir_de_verdade(self):
        for i in range(7):
            self.wa.enviar(f"m{i}")
        self.assertEqual(len(self.cli.enviadas), 7)
        self.assertTrue(self.rel.dormidas)  # it waited (on the fake clock)
        self.assertGreaterEqual(sum(self.rel.dormidas), 55)  # 6th message waits for the 60 s window

    def test_erro_da_biblioteca_nao_vaza_texto(self):
        self.cli.falhar_envio = True
        with self.assertRaises(WhatsAppErro) as cm:
            self.wa.enviar("x")
        self.assertNotIn("555", str(cm.exception))

    def test_reconexao_com_backoff_e_parada_em_evento_fatal(self):
        self.wa.laco_conexao(max_tentativas=3)
        self.assertEqual(self.cli.conexoes, 3)
        self.assertEqual(self.rel.dormidas[:2], [5, 10])
        self.cli.ao_conectar = lambda n: self.cli.fatal("logged out")
        self.wa.laco_conexao(max_tentativas=10)
        self.assertEqual(self.cli.conexoes, 4)  # stopped right after the fatal event
        with self.assertRaises(WhatsAppErro):
            self.wa.enviar("x")

    def test_backoff_tem_teto(self):
        b = Backoff(5, 60)
        self.assertEqual([b.proximo() for _ in range(6)], [5, 10, 20, 40, 60, 60])

    def test_sem_numero_do_dono_recusa(self):
        with self.assertRaises(WhatsAppErro):
            WhatsApp("", ClienteFalso())


class ServicoPorWhatsApp(Base):
    """The whole bot works over WhatsApp: free text, commands and numbered approvals."""

    def setUp(self):
        super().setUp()
        self.cfg.chat_id = int(DONO)
        self.cfg.channel = "whatsapp"
        self.cli = ClienteFalso()
        rel = Relogio()
        self.wa = WhatsApp(DONO, self.cli, limite=LimiteEnvio(100, 0, rel, rel.dormir))
        self.wa._fio = object()
        self.svc = Servico(self.cfg, self.estado, self.quadro, self.wa, self.codex, None, self.root, relogio=lambda: self.t[0])
        self.bot = Bot(self.svc)
        self.bot._conversa = _Sincrono()

    def processar_tudo(self):
        for u in self.wa.atualizacoes(0, timeout=0):
            self.bot.processar(u)

    def test_comando_e_aprovacao_numerada(self):
        self.cli.chega("/pause")
        self.processar_tudo()
        self.assertIn("1. Confirm", self.cli.enviadas[-1][1])
        self.cli.chega(re.search(r"[a-f0-9]{8}:1", self.cli.enviadas[-1][1]).group(0))
        self.processar_tudo()
        self.assertTrue(self.estado.get("pausado"))
        self.assertEqual(self.cli.enviadas[-1][1], "Queue paused.")

    def test_texto_livre_vai_ao_codex_e_html_vira_texto(self):
        self.cli.chega("explain the board")
        self.processar_tudo()
        self.assertEqual(len(self.codex.chamadas), 1)
        self.svc.notificar("<b>bold</b> a&lt;b", html=True)
        self.assertEqual(self.cli.enviadas[-1][1], "bold a<b")

    def test_estranho_nunca_recebe_nada(self):
        self.cli.chega("/status", de="5511988887777")
        self.processar_tudo()
        self.assertEqual(self.cli.enviadas, [])


if __name__ == "__main__":
    unittest.main()
