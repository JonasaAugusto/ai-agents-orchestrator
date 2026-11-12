"""Reliability regressions, using disposable state and event-driven synchronization."""
import io
import json
import socket
import threading
from pathlib import Path
from unittest import mock

from ai_agents_orchestrator import cli, doctor, engines, prompts, watch, workspace
from tests.helpers import Base
from tests.test_doctor_setup import AmbienteFalso, estados


class Recados(Base):
    def proposta(self):
        return self.quadro.criar("Synthetic proposal", dono="claude", origem="codex")

    def test_primeira_entrega_consume_apenas_o_delta(self):
        a = self.proposta()
        self.assertIn(f"#{a['id']} state=pending", self.svc.recado_para_hook())
        self.assertEqual(self.quadro.ver(a['id'])["visto_claude"], 1)
        b = self.proposta()
        texto = self.svc.recado_para_hook()
        self.assertNotIn(f"#{a['id']} ", texto)
        self.assertIn(f"#{b['id']} state=pending", texto)
        self.assertNotIn("Useful commands", texto)
        self.assertEqual(self.svc.recado_para_hook(), "")

    def test_geracao_so_inclui_nao_vistos_e_tem_teto(self):
        antiga = self.quadro.criar("SEEN SENTINEL", estado="bloqueada")
        nova = self.proposta()
        self.quadro.atualizar(nova['id'], aviso="a" * 20000)
        texto = self.svc.gerar_recado().read_text()
        self.assertNotIn("SEEN SENTINEL", texto)
        self.assertIn(f"#{nova['id']}", texto)
        self.assertLessEqual(len(texto), 12000)
        self.assertEqual(self.quadro.ver(antiga['id'])["visto_claude"], 1)
        self.assertEqual(self.quadro.ver(nova['id'])["visto_claude"], 0)

    def test_teto_nao_perde_tarefas_nao_entregues(self):
        ids = [self.proposta()['id'] for _ in range(12)]
        texto = self.svc.recado_para_hook(300)
        vistos = [i for i in ids if self.quadro.ver(i)['visto_claude']]
        self.assertGreater(len(vistos), 0)
        self.assertLess(len(vistos), len(ids))
        self.assertLessEqual(len(texto), 300)
        for tid in vistos:
            self.assertIn(f"#{tid} state=pending; run `aao result {tid}`.", texto)
        self.assertEqual(len(self.quadro.recado_pendente()), len(ids) - len(vistos))
        self.svc.recado_para_hook(1000000)
        self.assertEqual(self.quadro.recado_pendente(), [])

    def test_teto_pequeno_nao_marca_e_fallback_so_e_entregue_uma_vez(self):
        t = self.proposta()
        self.assertEqual(self.svc.recado_para_hook(-1), "")
        self.assertEqual(self.quadro.ver(t['id'])['visto_claude'], 0)
        self.svc.recado_para_hook()
        self.estado.set("claude_limite", {"ativo": True, "reset_epoch": 2000})
        self.assertTrue(self.svc.recado_para_hook())
        self.assertEqual(self.svc.recado_para_hook(), "")
        self.estado.set("claude_limite", {"ativo": True, "reset_epoch": 3000})
        self.assertTrue(self.svc.recado_para_hook())

    def test_hook_nao_marca_recado_que_chegou_depois(self):
        t = self.proposta()
        def chamar(cfg, req, **kw):
            if req['cmd'] == 'recado':
                r = self.svc.comando(req)
                self.quadro.atualizar(t['id'], estado='bloqueada', visto_claude=0)
                return r
            return self.svc.comando(req)
        with mock.patch.object(cli, 'chamar', side_effect=chamar), mock.patch('sys.stdout', io.StringIO()):
            cli._hook_prompt(self.cfg, 'UserPromptSubmit')
        self.assertEqual(self.quadro.ver(t['id'])['visto_claude'], 0)
        self.estado.set('claude_limite', {'ativo': True, 'reset_epoch': 2000})
        with mock.patch.object(cli, 'chamar', side_effect=chamar), mock.patch('sys.stdout', io.StringIO()):
            cli._hook_prompt(self.cfg, 'UserPromptSubmit')
        self.assertEqual(self.quadro.ver(t['id'])['visto_claude'], 0)
        self.assertFalse(json.loads((self.cfg.para_claude / 'estado.json').read_text())['lido'])

    def test_entregas_concorrentes_nao_repetem_o_delta(self):
        self.proposta()
        barreira = threading.Barrier(3)
        entregas = []
        def receber():
            barreira.wait(5)
            entregas.append(self.svc.recado_para_hook())
        threads = [threading.Thread(target=receber) for _ in range(2)]
        for th in threads:
            th.start()
        barreira.wait(5)
        for th in threads:
            th.join(5)
            self.assertFalse(th.is_alive())
        self.assertEqual(sum(bool(t) for t in entregas), 1)
        self.assertEqual(self.quadro.recado_pendente(), [])


class Retomadas(Base):
    def preparar(self, revisao=False):
        self.criar_repo()
        self.cfg.rules_file = self.dir / 'rules.md'
        self.cfg.rules_file.write_text('BASE RULES SENTINEL')
        return self.quadro.criar('Synthetic task', 'DESCRIPTION SENTINEL', 'web-app',
                                 kind='revisao' if revisao else 'dev', alvo='main' if revisao else None)

    def test_resume_so_envia_delta_inclusive_revisao(self):
        for kind in ('dev', 'revisao'):
            with self.subTest(kind=kind):
                t = {'id': 7, 'repo': 'r', 'titulo': 'TITLE SENTINEL', 'descricao': 'DESCRIPTION SENTINEL',
                     'kind': kind, 'instrucoes': 'OWNER SENTINEL', 'alvo': 'main'}
                with mock.patch.object(prompts, 'regras_base', return_value='RULES SENTINEL'):
                    p = prompts.prompt_tarefa(t, branch='b', base='main', briefing_tarefa=True,
                                              retomada=True, snapshot='SNAPSHOT SENTINEL')
                for antigo in ('RULES SENTINEL', 'DESCRIPTION SENTINEL', 'TITLE SENTINEL'):
                    self.assertNotIn(antigo, p)
                for delta in ('OWNER SENTINEL', 'SNAPSHOT SENTINEL', 'AGENTS.md', '7.md'):
                    self.assertIn(delta, p)

    def test_timeout_retoma_review_na_mesma_thread_com_delta(self):
        t = self.preparar(revisao=True)
        self.codex.respostas = [engines.Resultado(timeout=True, thread_id='review-thread'),
                                engines.Resultado(ok=True, texto='Review done')]
        self.svc.ciclo_worker()
        self.svc.ciclo_worker()
        chamada = self.codex.chamadas[-1]
        self.assertEqual(chamada['resume'], 'review-thread')
        self.assertIn('finish and report', chamada['prompt'].lower())
        self.assertNotIn('BASE RULES SENTINEL', chamada['prompt'])
        self.assertEqual(self.quadro.ver(t['id'])['estado'], 'concluida')

    def test_workspace_com_thread_ausente_recebe_prompt_completo(self):
        t = self.preparar()
        self.codex.respostas = [engines.Resultado(timeout=True), engines.Resultado(ok=True, texto='Status: completed')]
        self.svc.ciclo_worker()
        self.svc.ciclo_worker()
        self.assertIsNone(self.codex.chamadas[-1]['resume'])
        self.assertIn('DESCRIPTION SENTINEL', self.codex.chamadas[-1]['prompt'])
        self.assertEqual(self.quadro.ver(t['id'])['estado'], 'em_revisao')
        self.assertIn('BASE RULES SENTINEL', (Path(self.quadro.ver(t['id'])['workspace']) / 'AGENTS.md').read_text())

    def test_base_rules_chegam_ao_agents_sem_template_e_sao_atualizadas(self):
        self.preparar()
        ws, _, _ = workspace.preparar_workspace(self.cfg, 'web-app', 100, 'test')
        self.assertIn('BASE RULES SENTINEL', (ws / 'AGENTS.md').read_text())
        self.cfg.rules_file.write_text('NEW RULES SENTINEL')
        workspace.instalar_agents_md(self.cfg, ws, 'web-app')
        self.assertIn('NEW RULES SENTINEL', (ws / 'AGENTS.md').read_text())
        self.assertNotIn('BASE RULES SENTINEL', (ws / 'AGENTS.md').read_text())
        victim = self.dir / 'synthetic-victim.md'
        victim.write_text('preserve')
        (ws / 'AGENTS.md').unlink()
        (ws / 'AGENTS.md').symlink_to(victim)
        workspace.instalar_agents_md(self.cfg, ws, 'web-app')
        self.assertFalse((ws / 'AGENTS.md').is_symlink())
        self.assertIn('NEW RULES SENTINEL', (ws / 'AGENTS.md').read_text())
        self.assertEqual(victim.read_text(), 'preserve')


class Espera(Base):
    def test_cli_usa_uma_chamada_bloqueante_sem_polling(self):
        t = self.quadro.criar('Task', estado='em_revisao')
        with mock.patch.object(cli, '_cfg', return_value=self.cfg), \
                mock.patch.object(cli, 'chamar', return_value={'ok': True, 'tarefa': t}) as chamar, \
                mock.patch.object(cli.time, 'sleep', side_effect=AssertionError('polling')), \
                mock.patch('sys.stdout', io.StringIO()):
            self.assertEqual(cli.main(['wait', str(t['id'])]), 0)
        self.assertEqual(chamar.call_args.args[1], {'cmd': 'esperar', 'id': t['id'], 'timeout': 0})
        self.assertIsNone(chamar.call_args.kwargs['timeout'])

    def test_servidor_notifica_sem_travar_comandos_e_acorda_na_parada(self):
        self.cfg.socket = self.dir / 'cli.sock'
        t = self.quadro.criar('Waiting')
        pronto, esperando = threading.Event(), threading.Event()
        renomear = __import__('os').rename
        def rename(a, b):
            renomear(a, b)
            pronto.set()
        def wait(*a, **kw):
            esperando.set()
            return original(*a, **kw)
        # Holding the condition lock makes the transition deterministic, without sleeps.
        original = self.quadro.mudou.wait
        respostas = []
        with mock.patch('os.rename', side_effect=rename), mock.patch.object(self.quadro.mudou, 'wait', side_effect=wait):
            server = threading.Thread(target=self.svc.run_socket)
            server.start()
            self.assertTrue(pronto.wait(5))
            cliente = threading.Thread(target=lambda: respostas.append(cli.chamar(
                self.cfg, {'cmd': 'esperar', 'id': t['id'], 'timeout': 0}, timeout=5)))
            cliente.start()
            try:
                self.assertTrue(esperando.wait(5))
                self.assertTrue(cli.chamar(self.cfg, {'cmd': 'status'}, timeout=5)['ok'])
                self.quadro.atualizar(t['id'], estado='em_revisao', resumo='done')
                cliente.join(5)
                self.assertFalse(cliente.is_alive())
                self.assertEqual(respostas[0]['tarefa']['resumo'], 'done')
                esperando.clear()
                self.quadro.atualizar(t['id'], estado='pendente')
                cliente = threading.Thread(target=lambda: respostas.append(cli.chamar(
                    self.cfg, {'cmd': 'esperar', 'id': t['id'], 'timeout': 0}, timeout=5)))
                cliente.start()
                self.assertTrue(esperando.wait(5))
            finally:
                self.svc.parar_suave()
                cliente.join(5)
                server.join(5)
            self.assertFalse(server.is_alive())
            self.assertFalse(cliente.is_alive())
            self.assertFalse(respostas[-1]['ok'])

    def test_espera_timeout_sem_relogio_real_e_id_inexistente(self):
        t = self.quadro.criar('Waiting')
        with mock.patch('ai_agents_orchestrator.servico.time.monotonic', side_effect=[10, 10, 12]), \
                mock.patch.object(self.quadro.mudou, 'wait') as esperar:
            r = self.svc.comando({'cmd': 'esperar', 'id': t['id'], 'timeout': 1})
        self.assertFalse(r['ok'])
        self.assertEqual(r['erro'], 'timeout')
        esperar.assert_called_once_with(1)
        self.assertEqual(self.svc.comando({'cmd': 'esperar', 'id': 999})['erro'], 'not found')


class WatchDelta(Base):
    def test_so_consulta_estado_quando_evento_ou_checkpoint_muda(self):
        t = self.quadro.criar('Watch')
        self.quadro.atualizar(t['id'], estado='em_andamento')
        gravar = self.svc.gravador_eventos(t['id'])
        chamadas, intervalos = [], []
        ler = watch.tarefa
        def tarefa(cfg, tid):
            chamadas.append(tid)
            return ler(cfg, tid)
        def avancar(intervalo):
            intervalos.append(intervalo)
            if len(intervalos) == 2:
                gravar({'type': 'turn.completed'})
            if len(intervalos) == 4:
                self.quadro.atualizar(t['id'], estado='bloqueada')
                self.svc.checkpoint(t['id'])
        with mock.patch.object(watch, 'tarefa', side_effect=tarefa):
            watch.main(self.cfg, t['id'], dormir=avancar, saida=lambda _: None, max_ciclos=10)
        self.assertEqual(len(chamadas), 3)
        self.assertEqual(intervalos, [2.5] * 4)


class DoctorConfiabilidade(Base):
    def test_sonda_socket_distingue_arquivo_velho_de_listener(self):
        path = self.dir / 'doctor.sock'
        amb = doctor.Ambiente()
        self.assertFalse(amb.socket_ativo(path))
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(path))
            self.assertFalse(amb.socket_ativo(path))
            server.listen(1)
            self.assertTrue(amb.socket_ativo(path))
        self.assertFalse(amb.socket_ativo(path))

    def test_plugin_instalado_exige_manifesto_atual_e_hooks_reais(self):
        settings = self.dir / 'settings.json'
        plugin = self.dir / 'plugin'
        key = 'ai-agents-orchestrator@aao'
        hooks = json.loads((Path(__file__).parents[1] / 'claude-plugin/hooks/hooks.json').read_text())
        amb = AmbienteFalso(arquivos={
            str(settings) + ':conteudo': json.dumps({'enabledPlugins': {key: True}}),
            str(settings.parent / 'plugins/installed_plugins.json') + ':conteudo': json.dumps(
                {'plugins': {key: [{'installPath': str(plugin)}]}}),
            str(plugin / '.claude-plugin/plugin.json') + ':conteudo': json.dumps(
                {'name': 'ai-agents-orchestrator', 'version': '1.0.1'}),
            str(plugin / 'hooks/hooks.json') + ':conteudo': json.dumps(hooks),
        })
        def status():
            return estados(doctor.checar_claude(self.cfg, amb, [settings]))['claude hooks']
        self.assertEqual(status(), doctor.AVISO)
        amb.arquivos[str(plugin / '.claude-plugin/plugin.json') + ':conteudo'] = json.dumps(
            {'name': 'ai-agents-orchestrator', 'version': '1.1.0'})
        self.assertEqual(status(), doctor.OK)
        del hooks['hooks']['Stop']
        amb.arquivos[str(plugin / 'hooks/hooks.json') + ':conteudo'] = json.dumps(hooks)
        self.assertEqual(status(), doctor.AVISO)

    def test_runtime_briefing_e_hooks_novos_sao_verificados(self):
        amb = AmbienteFalso()
        amb.socket_ativo = lambda path: False
        itens = doctor.diagnosticar(self.cfg, amb, [self.dir / 'settings.json'])
        e = estados(itens)
        self.assertEqual(e['service socket'], doctor.FALHA)
        self.assertEqual(e['root daemon socket'], doctor.FALHA)
        self.assertEqual(e['live briefing'], doctor.AVISO)
        amb.socket_ativo = lambda path: True
        amb.arquivos[str(self.cfg.para_codex / 'BRIEFING.md')] = (0o644, 1000)
        e = estados(doctor.diagnosticar(self.cfg, amb, [self.dir / 'settings.json']))
        self.assertEqual(e['service socket'], doctor.OK)
        self.assertEqual(e['root daemon socket'], doctor.OK)
        self.assertEqual(e['live briefing'], doctor.OK)

    def test_plugin_nome_sozinho_nao_prova_hooks_e_hooks_completos_passam(self):
        settings = self.dir / 'settings.json'
        amb = AmbienteFalso(arquivos={str(settings) + ':conteudo': '{"enabledPlugins":{"ai-agents-orchestrator@aao":true}}'})
        self.assertEqual(estados(doctor.checar_claude(self.cfg, amb, [settings]))['claude hooks'], doctor.AVISO)
        hooks = json.loads((Path(__file__).parents[1] / 'claude-plugin/hooks/hooks.json').read_text())
        amb.arquivos[str(settings) + ':conteudo'] = json.dumps(hooks)
        self.assertEqual(estados(doctor.checar_claude(self.cfg, amb, [settings]))['claude hooks'], doctor.OK)

    def test_fix_cria_so_diretorios_e_rules_sem_sobrescrever(self):
        self.cfg.state_dir = self.dir / 'fresh'
        self.cfg.rules_file = self.dir / 'config/BASE_RULES.md'
        self.cfg.division_file = self.dir / 'config/DIVISION.md'
        saida = []
        doctor.main(self.cfg, fix=True, amb=AmbienteFalso(), saida=saida.append)
        self.assertTrue(self.cfg.para_codex.is_dir())
        self.assertTrue(self.cfg.resultados.is_dir())
        self.assertIn('review', self.cfg.rules_file.read_text().lower())
        self.assertEqual(self.cfg.rules_file.stat().st_mode & 0o777, 0o644)
        self.assertEqual(self.cfg.rules_file.parent.stat().st_mode & 0o777, 0o755)
        self.assertEqual(self.cfg.state_dir.stat().st_mode & 0o777, 0o700)
        self.cfg.rules_file.write_text('OWNER RULES')
        doctor.main(self.cfg, fix=True, amb=AmbienteFalso(), saida=saida.append)
        self.assertEqual(self.cfg.rules_file.read_text(), 'OWNER RULES')
        self.assertFalse((self.cfg.para_codex / 'BRIEFING.md').exists())

