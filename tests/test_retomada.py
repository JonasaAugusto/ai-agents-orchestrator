"""Persistent relay integration: local disposable git/SQLite, fake engines and clock."""
import contextlib
import io
import json
import threading
from pathlib import Path
from unittest import mock

from ai_agents_orchestrator import cli, config, doctor, engines
from ai_agents_orchestrator.db import Estado, Quadro
from ai_agents_orchestrator.servico import Servico
from ai_agents_orchestrator.testing import CodexFalso
from tests.helpers import Base


class ClaudeFalso(CodexFalso):
    def disponivel(self):
        return True

    def executar(self, prompt, cwd, timeout=0, agora=None, resume=None, on_event=None):
        return super().executar(prompt, cwd, timeout=timeout, resume=resume, on_event=on_event)


class Retomada(Base):
    def setUp(self):
        super().setUp()
        self.criar_repo()
        config.definir_fuso("UTC")
        self.cfg.armado_flag.touch()
        self.claude = ClaudeFalso()
        self.svc.claude = self.claude

    def tarefa(self, titulo="Develop"):
        return self.quadro.criar(titulo, repo="web-app")

    def limitar_codex(self, ate=2000):
        self.estado.set("codex_limite", {"ate": ate, "desde": self.t[0]})

    def limitar_claude(self, ate=3000):
        self.svc._registrar_claude_limite(engines.LimiteInfo("session limit", ate), "test")

    def reiniciar(self):
        estado, quadro = Estado(self.cfg.db_estado), Quadro(self.cfg.db_quadro)
        self.addCleanup(estado.c.close)
        self.addCleanup(quadro.c.close)
        self.estado, self.quadro = estado, quadro
        self.svc = Servico(self.cfg, estado, quadro, self.tg, self.codex, self.claude,
                           self.root, relogio=lambda: self.t[0])
        self.svc.recuperar_tarefas()

    def test_codex_limita_pausa_e_retoma_thread_workspace_checkpoint(self):
        t = self.tarefa()
        self.codex.respostas = [engines.Resultado(limite=engines.LimiteInfo("usage", 2000), thread_id="cx")]
        self.svc.ciclo_worker()
        saved = self.quadro.ver(t["id"])
        checkpoint = self.cfg.resultados / str(t["id"]) / "HANDOFF.md"
        self.assertTrue(checkpoint.exists())
        self.assertIn("cx", checkpoint.read_text())
        self.assertFalse(self.svc.ciclo_worker())
        self.reiniciar()
        self.t[0] = 2000
        self.assertTrue(self.svc.ciclo_worker())
        call = self.codex.chamadas[-1]
        self.assertEqual((call["resume"], call["cwd"]), ("cx", saved["workspace"]))
        self.assertIn("continue where you stopped and report", call["prompt"].lower())

    def test_claude_limita_codex_assume_claude_volta_sem_sessao(self):
        self.estado.set("motor", "claude")
        self.cfg.claude_wakeup = False
        t = self.tarefa()
        self.claude.respostas = [engines.Resultado(limite=engines.LimiteInfo("limit", 2000), thread_id="cl")]
        self.codex.respostas = [engines.Resultado(ok=True, texto="Done", thread_id="cx")]
        self.svc.ciclo_worker()
        self.assertEqual(len(self.codex.chamadas), 1)
        self.t[0] = 2000
        self.svc.tick_retomada()
        self.assertEqual(self.svc.motor_efetivo(), "claude")
        self.assertIn("Done", (self.cfg.para_claude / "HANDOFF-PARA-CLAUDE.md").read_text())
        self.assertEqual(self.quadro.ver(t["id"])["thread_id"], "cx")

    def test_ambos_primeiro_reset_codex_prioridade_persistida(self):
        self.limitar_claude(3000)
        a, b = self.tarefa("Normal"), self.tarefa("Urgent")
        self.quadro.atualizar(b["id"], prioridade=10)
        self.limitar_codex(2000)
        self.assertFalse(self.svc.ciclo_worker())
        self.assertTrue(any("both at their limits" in s.lower() and "00:33" in s for s in self.tr.textos()))
        self.reiniciar()
        self.t[0] = 2000
        self.assertTrue(self.svc.ciclo_worker())
        self.assertEqual(self.quadro.ver(b["id"])["estado"], "em_revisao")
        self.assertEqual(self.quadro.ver(a["id"])["estado"], "pendente")
        self.assertIn("continue where you stopped and report", self.codex.chamadas[-1]["prompt"].lower())

    def test_ambos_primeiro_reset_claude_com_equilibrio(self):
        self.cfg.claude_dev_fallback = True
        self.estado.registrar_execucao("tarefa", "codex", 1, False, agora=self.t[0])
        self.limitar_claude(2000)
        self.limitar_codex(3000)
        self.tarefa()
        self.assertFalse(self.svc.ciclo_worker())
        self.reiniciar()
        self.t[0] = 2000
        self.assertTrue(self.svc.ciclo_worker())
        self.assertEqual(len(self.claude.chamadas), 1)
        self.assertEqual(self.codex.chamadas, [])
        self.tarefa("wait for balance")
        self.assertFalse(self.svc.ciclo_worker())
        self.t[0] = 3000
        self.assertTrue(self.svc.ciclo_worker())
        self.assertEqual(len(self.codex.chamadas), 1)

    def test_fallback_claude_requer_opt_in_e_nunca_executa_revisao(self):
        self.limitar_codex()
        t = self.quadro.criar("Review", repo="web-app", kind="revisao", alvo="main")
        self.cfg.claude_dev_fallback = True
        self.estado.registrar_execucao("tarefa", "codex", 1, False, agora=self.t[0])
        self.assertFalse(self.svc.ciclo_worker())
        self.quadro.atualizar(t["id"], kind="dev")
        self.cfg.claude_dev_fallback = False
        self.assertFalse(self.svc.ciclo_worker())
        self.cfg.claude_dev_fallback = True
        self.assertTrue(self.svc.ciclo_worker())
        self.assertEqual(len(self.claude.chamadas), 1)

    def test_timeout_retoma_uma_vez_termina_e_relate_depois_bloqueia(self):
        t = self.tarefa()
        self.codex.respostas = [engines.Resultado(timeout=True, thread_id="cx"),
                                engines.Resultado(timeout=True, thread_id="cx")]
        self.svc.ciclo_worker()
        self.assertEqual(self.quadro.ver(t["id"])["estado"], "pendente")
        self.reiniciar()
        self.t[0] += 3600
        self.svc.ciclo_worker()
        self.assertEqual(self.codex.chamadas[-1]["resume"], "cx")
        self.assertIn("finish and report", self.codex.chamadas[-1]["prompt"].lower())
        self.assertEqual(self.quadro.ver(t["id"])["estado"], "bloqueada")

    def test_crash_persiste_thread_antes_de_terminar_runner(self):
        t = self.tarefa()
        self.codex.eventos = [{"type": "thread.started", "thread_id": "live-cx"}]

        def crash(prompt, cwd):
            self.assertEqual(self.quadro.ver(t["id"])["thread_id"], "live-cx")
            raise RuntimeError("simulated crash")
        self.codex.respostas = [crash]
        with self.assertRaisesRegex(RuntimeError, "simulated crash"):
            self.svc.ciclo_worker()
        saved = self.quadro.ver(t["id"])
        self.reiniciar()
        self.svc.ciclo_worker()
        self.assertEqual(self.codex.chamadas[-1]["resume"], "live-cx")
        self.assertEqual(self.codex.chamadas[-1]["cwd"], saved["workspace"])

    def test_reinicio_recupera_so_claude_headless_e_preserva_threads(self):
        self.estado.set("motor", "claude")
        t = self.tarefa()
        interactive = self.quadro.criar("Interactive", repo="other", dono="claude", estado="em_andamento")
        self.claude.eventos = [{"type": "system", "subtype": "init", "session_id": "cl-live"}]
        self.claude.respostas = [lambda p, c: (_ for _ in ()).throw(RuntimeError("crash"))]
        with self.assertRaisesRegex(RuntimeError, "crash"):
            self.svc.ciclo_worker()
        self.reiniciar()
        self.assertEqual(self.quadro.ver(interactive["id"])["estado"], "em_andamento")
        self.svc.ciclo_worker()
        self.assertEqual(self.claude.chamadas[-1]["resume"], "cl-live")
        self.assertIsNone(self.quadro.ver(t["id"])["thread_id"])

    def test_hook_ambos_limitam_guarda_payload_retenta_so_apos_reset(self):
        payload = {"last_assistant_message": "You've hit your session limit · resets 1am (UTC)"}
        self.codex.respostas = [engines.Resultado(limite=engines.LimiteInfo("usage", 2000))]
        self.svc.claude_limite(payload)
        self.assertTrue(self.estado.get("claude_limite")["ativo"])
        self.assertIsNotNone(self.estado.get("contexto_retomar"))
        self.reiniciar()
        self.svc.tick_retomada()
        self.assertEqual(len(self.codex.chamadas), 1)
        from tests.test_servico import contexto
        self.codex.respostas = [engines.Resultado(ok=True, texto=contexto("sim", []))]
        self.t[0] = 2000
        self.svc.tick_retomada()
        self.assertEqual(len(self.codex.chamadas), 2)
        self.assertIsNone(self.estado.get("contexto_retomar"))

    def test_reset_desconhecido_sonda_periodica_persistida(self):
        self.svc._registrar_claude_limite(engines.LimiteInfo("limit"), "test")
        self.claude.respostas = [engines.Resultado(limite=engines.LimiteInfo("limit")), engines.Resultado(ok=True)]
        self.t[0] += self.cfg.claude_sonda_minutos * 60
        self.svc.tick_retomada()
        self.assertEqual(len(self.claude.chamadas), 1)
        self.reiniciar()
        self.svc.tick_retomada()
        self.assertEqual(len(self.claude.chamadas), 1)
        self.t[0] += self.cfg.claude_sonda_minutos * 60
        self.svc.tick_retomada()
        self.assertEqual(self.svc.motor_efetivo(), "codex")
        self.assertFalse(self.estado.get("claude_limite").get("ativo"))

    def test_codex_coordenador_nao_devolve_fila_ao_claude_ausente(self):
        self.cfg.orchestrator = "codex"
        t = self.quadro.criar("Relay", repo="web-app", origem="handoff")
        self.svc.claude_assumiu()
        self.assertEqual(self.quadro.ver(t["id"])["dono"], "codex")

    def test_status_brief_e_doctor_validam_agenda_persistida(self):
        self.limitar_claude(3000)
        self.limitar_codex(2000)
        self.svc.tick_retomada()
        status = self.svc.status()
        self.assertEqual(status["retomada_agendada"]["proxima"], 2000)
        with mock.patch.object(cli.config, "carregar", return_value=self.cfg), mock.patch.object(
                cli, "chamar", return_value={"ok": True, **status}), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main(["status", "--brief"]), 0)
        self.assertIn("retomada agendada: Claude 00:50 / Codex 00:33", out.getvalue())
        self.assertEqual(doctor.checar_agendamento(self.cfg)[0].estado, doctor.OK)
        self.estado.set("codex_limite", {"ate": "broken"})
        self.assertEqual(doctor.checar_agendamento(self.cfg)[0].estado, doctor.FALHA)

    def test_timer_acorda_claude_enquanto_worker_esta_ocupado(self):
        iniciou, liberar = threading.Event(), threading.Event()
        self.addCleanup(liberar.set)
        self.limitar_claude(2000)
        self.tarefa()

        def worker(prompt, cwd):
            iniciou.set()
            if not liberar.wait(5):
                raise RuntimeError("test worker not released")
            return engines.Resultado(ok=True)
        self.codex.respostas = [worker]
        th = threading.Thread(target=self.svc.ciclo_worker)
        th.start()
        try:
            self.assertTrue(iniciou.wait(5))
            self.t[0] = 2100
            self.svc.tick_retomada()
            self.assertTrue(any(c[0] == "acordar_claude" for c in self.root.chamadas))
        finally:
            liberar.set()
            th.join(5)
        self.assertFalse(th.is_alive())

    def test_revisao_timeout_duplo_bloqueia(self):
        t = self.quadro.criar("Review", repo="web-app", kind="revisao", alvo="main")
        self.codex.respostas = [engines.Resultado(timeout=True, thread_id="review"),
                                engines.Resultado(timeout=True, thread_id="review")]
        self.svc.ciclo_worker()
        self.svc.ciclo_worker()
        self.assertEqual(self.quadro.ver(t["id"])["estado"], "bloqueada")

    def test_sonda_usa_novo_reset_explicito_sem_antecipar(self):
        self.svc._registrar_claude_limite(engines.LimiteInfo("limit"), "test")
        self.claude.respostas = [engines.Resultado(limite=engines.LimiteInfo("limit", 5000))]
        self.t[0] += self.cfg.claude_sonda_minutos * 60
        self.svc.tick_retomada()
        self.assertEqual(self.svc.agendamento()["claude"], 5000)
        self.assertTrue(self.svc.claude_limitado())

    def test_hook_aguarda_limite_ja_conhecido_sem_perder_contexto(self):
        self.limitar_codex(2000)
        payload = {"last_assistant_message": "You've hit your session limit · resets 1am (UTC)"}
        self.svc.claude_limite(payload)
        self.assertEqual(self.codex.chamadas, [])
        self.assertEqual(self.estado.get("contexto_retomar")["apos"], 2000)

    def test_prioridade_disponivel_na_delegacao(self):
        r = self.svc.comando({"cmd": "delegar", "repo": "web-app", "titulo": "Urgent", "prioridade": 7})
        self.assertEqual(self.quadro.ver(r["id"])["prioridade"], 7)

    def test_sonda_nao_pode_cancelar_worker_claude_em_andamento(self):
        self.svc._registrar_claude_limite(engines.LimiteInfo("limit"), "test")
        self.claude.atual = object()
        self.t[0] += self.cfg.claude_sonda_minutos * 60
        self.svc.tick_retomada()
        self.assertEqual(self.claude.chamadas, [])

    def test_limite_sem_thread_reutiliza_workspace_sujo(self):
        t = self.tarefa()

        def parcial(prompt, cwd):
            (Path(cwd) / "partial.py").write_text("value = 7\n")
            return engines.Resultado(limite=engines.LimiteInfo("usage", 2000))
        self.codex.respostas = [parcial]
        self.svc.ciclo_worker()
        ws = self.quadro.ver(t["id"])["workspace"]
        self.reiniciar()
        self.t[0] = 2000
        self.svc.ciclo_worker()
        self.assertEqual(self.codex.chamadas[-1]["cwd"], ws)
        self.assertEqual((Path(ws) / "partial.py").read_text(), "value = 7\n")

    def test_reinicio_com_limite_nao_descarta_chat_interrompido(self):
        self.codex.respostas = [engines.Resultado(exit_code=-15, thread_id="chat-thread")]
        self.svc.conversar("continue", repo_citado="web-app")
        self.limitar_codex(8000)
        self.reiniciar()
        self.t[0] = 8000
        self.svc.retomar_interrompidos()
        self.assertEqual(len(self.codex.chamadas), 2)
        self.assertEqual(self.codex.chamadas[-1]["resume"], "chat-thread")
        self.assertIsNone(self.estado.get("retomar"))

    def test_chat_aguardando_reset_preserva_pedido(self):
        self.codex.respostas = [engines.Resultado(exit_code=-15, thread_id="chat-thread")]
        self.svc.conversar("continue", repo_citado="web-app")
        self.limitar_codex(2000)
        self.svc.retomar_interrompidos()
        self.assertEqual(self.estado.get("retomar")["texto"], "continue")
        self.assertEqual(len(self.codex.chamadas), 1)

    def test_chat_limita_timer_preserva_thread_e_retoma_pedido(self):
        self.codex.respostas = [engines.Resultado(limite=engines.LimiteInfo("usage", 8000), thread_id="chat-limit")]
        self.svc.conversar("continue", repo_citado="web-app")
        self.assertIsNotNone(self.estado.get("retomar"))
        self.reiniciar()
        self.t[0] = 8000
        self.svc.ciclo_worker()
        self.assertEqual(len(self.codex.chamadas), 2)
        self.assertEqual(self.codex.chamadas[-1]["resume"], "chat-limit")

    def test_chat_timeout_worker_retoma_uma_vez(self):
        self.codex.respostas = [engines.Resultado(timeout=True, thread_id="chat-timeout")]
        self.svc.conversar("continue", repo_citado="web-app")
        self.assertIsNotNone(self.estado.get("retomar"))
        self.svc.ciclo_worker()
        self.assertEqual(self.codex.chamadas[-1]["resume"], "chat-timeout")
        self.assertIn("finish and report", self.codex.chamadas[-1]["prompt"].lower())
        self.assertIsNone(self.estado.get("retomar"))

    def test_chat_limite_longo_preserva_sessao_alem_da_inatividade(self):
        self.codex.respostas = [engines.Resultado(limite=engines.LimiteInfo("usage", 30000), thread_id="long-limit")]
        self.svc.conversar("continue", repo_citado="web-app")
        ws = self.estado.get("chat_sessao")["ws"]
        self.reiniciar()
        self.t[0] = 30000
        self.svc.ciclo_worker()
        self.assertEqual(len(self.codex.chamadas), 2)
        self.assertEqual(self.codex.chamadas[-1]["resume"], "long-limit")
        self.assertEqual(self.codex.chamadas[-1]["cwd"], ws)

    def test_cancelamento_do_chat_pendente_impede_retomada(self):
        self.codex.respostas = [engines.Resultado(limite=engines.LimiteInfo("usage", 2000), thread_id="cancel")]
        self.svc.conversar("continue", repo_citado="web-app")
        tid = self.estado.get("chat_sessao")["tid"]
        self.assertIsNotNone(self.estado.get("retomar"))
        self.svc.cancelar_tarefa(tid)
        self.t[0] = 2000
        self.svc.ciclo_worker()
        self.assertEqual(len(self.codex.chamadas), 1)
        self.assertEqual(self.quadro.ver(tid)["estado"], "cancelada")


class Contratos(Base):
    def test_timeout_subprocesso_com_relogio_injetado(self):
        class Processo:
            def __init__(self):
                self.stdin = io.StringIO()
                self.stdout = io.StringIO("")
                self.stderr = io.StringIO("")
                self.returncode = None

            def poll(self):
                return self.returncode

        class Controle(engines._Proc):
            def terminar(self, grace=0):
                self.cancel.set()
                self.p.returncode = -15

        agora, esperas = [0], []

        def esperar(seg):
            esperas.append(seg)
            agora[0] += 1

        with mock.patch.object(engines.subprocess, "Popen", return_value=Processo()):
            code, _, _, timeout = engines._rodar(["fake"], "input", None, {}, 1, Controle(),
                                               relogio=lambda: agora[0], esperar=esperar)
        self.assertTrue(timeout)
        self.assertEqual(code, -15)
        self.assertEqual(len(esperas), 3)

    def test_timeout_padrao_e_alias_legado(self):
        self.assertEqual(config.Config().task_timeout, 3600)
        self.assertEqual(config.carregar(env={"FALLBACK_TASK_TIMEOUT": "91"}, arquivo=self.dir / "absent.toml").task_timeout, 91)

    def test_claude_resume_e_thread_inicial_stream(self):
        events, calls = [], []

        def runner(cmd, stdin, cwd, env, timeout, ctl, on_line):
            calls.append(cmd)
            on_line(json.dumps({"type": "system", "subtype": "init", "session_id": "cl-live"}))
            on_line(json.dumps({"type": "result", "result": "done", "session_id": "cl-live"}))
            return 0, [], "", False
        res = engines.ClaudeRunner(self.cfg.state_dir, runner=runner).executar(
            "continue", self.dir, resume="cl-live", on_event=events.append)
        self.assertTrue(res.ok)
        self.assertEqual(res.thread_id, "cl-live")
        self.assertIn("--resume", calls[0])
        self.assertIn("stream-json", calls[0])
        self.assertEqual(events[0]["session_id"], "cl-live")
