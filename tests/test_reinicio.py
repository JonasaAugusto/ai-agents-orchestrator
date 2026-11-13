import threading
import time

from ai_agents_orchestrator import engines, historico
from ai_agents_orchestrator.engines import Resultado
from tests.helpers import Base, needs_root
from tests.test_chat import edita


class Reinicio(Base):
    def setUp(self):
        super().setUp()
        self.gh = self.criar_repo()

    def textos_hist(self):
        return [r["texto"] for r in historico.ler(self.svc.hist.caminho)]

    def test_mensagem_de_sinal_explica_o_motivo(self):
        m = engines.motivo_sinal(-15)
        self.assertIn("SIGTERM", m)
        self.assertIn("service restart", m)

    @needs_root
    def test_sigterm_durante_execucao_drena_e_conclui(self):
        iniciou, solta = threading.Event(), threading.Event()

        def lento(prompt, cwd):
            iniciou.set()
            solta.wait(10)
            return edita()(prompt, cwd)
        self.codex.respostas = [lento]
        th = threading.Thread(target=lambda: self.bot.processar(self.msg("ajusta o app do web-app")))
        th.start()
        self.assertTrue(iniciou.wait(5))
        self.svc.parar_suave()
        self.assertTrue(self.svc.parando)
        self.assertFalse(self.codex.cancelado)  # nao matou o Codex
        r = []
        d = threading.Thread(target=lambda: r.append(self.svc.drenar(limite=10, passo=0.05)))
        d.start()
        time.sleep(0.3)
        self.assertTrue(d.is_alive())  # esperando
        solta.set()
        th.join(10); d.join(10)
        self.assertEqual(r, [True])
        self.assertIsNone(self.estado.get("retomar"))
        self.assertTrue(any("Feito" in t for t in self.tr.textos()))

    @needs_root
    def test_morte_forcada_retoma_uma_vez(self):
        morto = Resultado(ok=False, exit_code=-15, thread_id="TC1", erro="")
        self.codex.respostas = [morto]
        self.bot.processar(self.msg("ajusta o app do web-app"))
        self.assertFalse(any("exit code -15" in t for t in self.tr.textos()))  # sem o erro cru
        self.assertIsNotNone(self.estado.get("retomar"))
        self.assertTrue(any("[interruption]" in t for t in self.textos_hist()))
        # servico sobe de novo
        self.codex.respostas = [edita()]
        self.svc.retomar_interrompidos()
        self.assertIn("A restart interrupted Codex; resuming.", self.tr.textos())
        self.assertEqual(len(self.codex.chamadas), 2)
        self.assertEqual(self.codex.chamadas[1]["resume"], "TC1")
        self.assertIsNone(self.estado.get("retomar"))
        self.assertTrue(any("[resume]" in t for t in self.textos_hist()))

    def test_segunda_morte_nao_retoma_de_novo(self):
        self.codex.respostas = [Resultado(ok=False, exit_code=-9, thread_id="TC1"),
                                Resultado(ok=False, exit_code=-9, thread_id="TC1")]
        self.bot.processar(self.msg("ajusta o app do web-app"))
        self.svc.retomar_interrompidos()
        self.assertEqual(len(self.codex.chamadas), 2)
        self.svc.retomar_interrompidos()  # marcador com tentativas=1: so avisa
        self.assertEqual(len(self.codex.chamadas), 2)
        self.assertTrue(any("again" in t for t in self.tr.textos()))

    def test_pedido_durante_parada_e_guardado(self):
        self.svc.parar_suave()
        self.bot.processar(self.msg("ajusta o app do web-app"))
        self.assertEqual(self.codex.chamadas, [])
        self.assertEqual(self.estado.get("retomar")["texto"], "ajusta o app do web-app")
