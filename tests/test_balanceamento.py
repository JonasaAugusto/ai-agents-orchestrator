"""Usage/balance contracts: disposable SQLite/git, injected clocks, no network or credentials."""
import contextlib
import io
import json
import os
import sqlite3
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from ai_agents_orchestrator import cli, config, doctor, engines, espelho, root_daemon
from ai_agents_orchestrator.db import Estado
from tests.helpers import Base, needs_root
from tests.test_engines import fake_runner
from tests.test_retomada import ClaudeFalso


class Balanceamento(Base):
    def cotas(self):
        self.t[0] = 2_000_000.0
        for motor in ('claude', 'codex'):
            setattr(self.cfg, motor + '_tokens_5h', 1000)
            setattr(self.cfg, motor + '_tokens_7d', 10000)

    def uso(self, claude, codex):
        self.cotas()
        self.estado.registrar_execucao('turno', 'claude', 0, True, agora=self.t[0], tokens_in=claude)
        self.estado.registrar_execucao('tarefa', 'codex', 0, True, agora=self.t[0], tokens_in=codex)
        return self.svc.balanceamento()

    def registro(self, ident='a', entrada=100, saida=20, quando=None):
        return {'papel': 'uso', 'id': ident * 64, 'tokens_in': entrada, 'tokens_out': saida,
                't': datetime.fromtimestamp(quando or self.t[0], timezone.utc).isoformat()}

    def test_codex_soma_turnos_sem_somar_cache_duas_vezes(self):
        run = fake_runner([{'type': 'turn.completed', 'usage': {'input_tokens': 120, 'cached_input_tokens': 80, 'output_tokens': 10}},
                           {'type': 'turn.completed', 'usage': {'input_tokens': 30, 'output_tokens': 5}}], final='done')
        r = engines.CodexRunner(self.cfg.state_dir, runner=run).executar('task', self.dir)
        self.assertTrue(r.ok)
        self.assertEqual((r.tokens_in, r.tokens_out), (150, 15))

    def test_eventos_sem_usage_e_contadores_invalidos_nao_quebram(self):
        r = engines.Resultado()
        for u in (None, [], {'input_tokens': -1, 'output_tokens': True}, {'input_tokens': '12', 'output_tokens': 1.5}):
            engines.CodexRunner._evento({'type': 'turn.completed', 'usage': u}, r)
        self.assertEqual((r.tokens_in, r.tokens_out), (0, 0))

    def test_claude_headless_inclui_tokens_de_cache(self):
        result = {'type': 'result', 'result': 'done', 'usage': {'input_tokens': 10, 'output_tokens': 20,
                  'cache_read_input_tokens': 30, 'cache_creation_input_tokens': 40}}
        c = engines.ClaudeRunner(self.cfg.state_dir, runner=lambda *a: (0, [json.dumps(result)], '', False))
        r = c.executar('task', self.dir)
        self.assertEqual((r.tokens_in, r.tokens_out), (80, 20))

    def test_migracao_aditiva_idempotente_preserva_execucoes_antigas(self):
        path = self.dir / 'legacy.db'
        with sqlite3.connect(path) as c:
            c.execute('CREATE TABLE execucoes (id INTEGER PRIMARY KEY,quando REAL,tipo TEXT,motor TEXT,segundos REAL,ok INTEGER,ref TEXT)')
            c.execute("INSERT INTO execucoes VALUES(1,10,'tarefa','codex',3,1,'7')")
        for _ in range(2):
            e = Estado(path)
            try:
                r = e.execucoes_desde(0)[0]
                self.assertEqual((r['id'], r['ref'], r['tokens_in'], r['tokens_out']), (1, '7', 0, 0))
            finally:
                e.c.close()
        self.estado.registrar_execucao('tarefa', 'codex', 2, False, 'same-task', tokens_in=20, tokens_out=3)
        self.estado.registrar_execucao('tarefa', 'codex', 1, True, 'same-task', tokens_in=5, tokens_out=1)
        self.assertEqual(len(self.estado.execucoes_desde(0)), 2)  # retries remain distinct attempts

    def test_espelho_conta_tool_only_deduplica_blocos_sem_texto_nos_contadores(self):
        p = self.dir / 'session.jsonl'
        msg = {'type': 'assistant', 'timestamp': '1970-01-01T00:16:40Z',
               'message': {'id': 'synthetic-message', 'content': [{'type': 'tool_use', 'id': 't', 'name': 'Read', 'input': {}}],
                           'usage': {'input_tokens': 10, 'cache_read_input_tokens': 30, 'output_tokens': 2}}}
        other = json.loads(json.dumps(msg))
        other['message']['usage']['output_tokens'] = 7
        p.write_text('\n'.join(json.dumps(r) for r in (msg, other, {'type': 'user', 'message': {'content': 'fixture'}})))
        usos = [r for r in espelho.cauda_transcript(p) if r['papel'] == 'uso']
        self.assertEqual(len(usos), 1)
        self.assertEqual((usos[0]['tokens_in'], usos[0]['tokens_out']), (40, 7))
        self.assertEqual(set(usos[0]), {'papel', 't', 'id', 'tokens_in', 'tokens_out'})
        self.assertNotIn('synthetic-message', json.dumps(usos))

    def test_importacao_sobreposta_idempotente_e_persistida_sem_conteudo(self):
        from ai_agents_orchestrator import medicao
        p = self.cfg.mirror / 'transcript-cauda.jsonl'
        p.write_text(json.dumps(self.registro()) + '\n' + json.dumps({'papel': 'usuario', 'texto': 'fixture-private'}) + '\ninvalid')
        medicao.importar_cauda(self.estado, p, self.t[0])
        p.write_text(json.dumps(self.registro(saida=30)))
        medicao.importar_cauda(self.estado, p, self.t[0])
        e = Estado(self.cfg.db_estado)
        self.addCleanup(e.c.close)
        rows = [dict(r) for r in e.execucoes_desde(0)]
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]['tokens_in'], rows[0]['tokens_out']), (100, 30))
        self.assertNotIn('fixture-private', json.dumps(rows))
        self.assertEqual(e.execucoes_desde(0, 'tarefa'), [])

    def test_importacao_recusa_timestamp_futuro_naive_ids_e_tokens_invalidos(self):
        from ai_agents_orchestrator import medicao
        registros = [self.registro(quando=self.t[0] + 1), {**self.registro(), 't': '2026-01-01'},
                     {**self.registro(), 'id': 'conversation'}, self.registro(entrada=-4, saida='100')]
        medicao.importar_registros(self.estado, registros, self.t[0])
        rows = self.estado.execucoes_desde(0)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]['tokens_in'], rows[0]['tokens_out']), (0, 0))

    def test_janelas_5h_7d_usam_contadores_e_relogio(self):
        self.cotas()
        for motor, age, tokens in [('claude', 18000, 100), ('claude', 18001, 200), ('claude', 604801, 999),
                                   ('codex', 0, 50), ('codex', -1, 999)]:
            self.estado.registrar_execucao('turno', motor, 0, True, agora=self.t[0]-age, tokens_in=tokens)
        b = self.svc.balanceamento()['janelas']
        self.assertEqual((b['5h']['claude']['tokens_in'], b['7d']['claude']['tokens_in']), (100, 300))
        self.assertEqual((b['5h']['claude']['usado_pct'], b['5h']['codex']['usado_pct'], b['5h']['razao']), (10, 5, 2))
        self.assertEqual(b['7d']['codex']['tokens_in'], 50)

    def test_limites_codex_prevalecem_casando_duracao_e_expiram(self):
        self.uso(400, 100)
        self.estado.set('codex_uso_balance', {'quando': self.t[0], 'dados': {'ok': True,
            'primaria': {'usado_pct': 70, 'duracao_min': 10080, 'reseta_em': self.t[0]+100},
            'secundaria': {'usado_pct': 20, 'duracao_min': 300, 'reseta_em': self.t[0]+100}}})
        b = self.svc.balanceamento()
        self.assertEqual(b['janelas']['5h']['razao'], 2)
        self.assertEqual(b['janelas']['7d']['codex']['usado_pct'], 70)
        self.assertEqual(b['janelas']['5h']['codex']['tokens_in'], 100)
        self.t[0] += 101
        self.assertEqual(self.svc.balanceamento()['janelas']['5h']['codex']['usado_pct'], 10)
        self.t[0] += self.cfg.balance_max_age_seconds
        self.assertFalse(self.svc.balanceamento()['codex_cache_fresco'])

    def test_dados_ausentes_zero_e_fronteiras_nao_inventam_percentual(self):
        self.assertEqual(self.svc.balanceamento()['modo'], 'unknown')
        self.cotas()
        self.assertEqual(self.svc.balanceamento()['modo'], 'unknown')  # 0/0
        self.estado.registrar_execucao('turno', 'claude', 0, True, agora=self.t[0], tokens_in=1)
        b = self.svc.balanceamento()
        self.assertEqual(b['modo'], 'economico')
        self.assertTrue(b['janelas']['5h']['codex_zero'])
        json.dumps(b, allow_nan=False)
        self.cfg.balance_enabled = False
        self.assertEqual(self.svc.balanceamento()['modo'], 'disabled')

    def test_ratio_nos_limites_07_e_14_e_equilibrado(self):
        b = self.uso(140, 100)
        self.assertEqual(b['modo'], 'equilibrado')
        self.cfg.claude_tokens_5h = 2000
        self.cfg.claude_tokens_7d = 20000
        self.assertEqual(self.svc.balanceamento()['modo'], 'equilibrado')

    def test_auto_economico_roteia_dev_e_revisao_para_codex(self):
        self.uso(400, 100)
        self.estado.set('motor', 'auto')
        self.svc.claude = ClaudeFalso()
        self.criar_repo()
        for kind in ('dev', 'revisao'):
            t = self.quadro.criar('Task', repo='web-app', kind=kind, alvo='main' if kind == 'revisao' else None)
            self.assertTrue(self.svc.ciclo_worker())
            self.assertEqual(self.quadro.ver(t['id'])['motor_execucao'], 'codex')
        self.assertEqual(self.svc.claude.chamadas, [])
        self.assertEqual(len(self.codex.chamadas), 2)

    def test_ratio_baixo_reduz_tasks_hora_e_libera_claude(self):
        b = self.uso(100, 400)
        self.assertEqual((b['modo'], b['tasks_per_hour']), ('claude', 3))
        self.assertIn('review directly', b['instrucao'])
        self.estado.set('motor', 'auto')
        self.svc.claude = ClaudeFalso()
        self.assertEqual(self.svc.motor_efetivo(), 'claude')
        self.criar_repo()
        for _ in range(2):  # one task already recorded by uso()
            self.estado.registrar_execucao('tarefa', 'codex', 0, True, agora=self.t[0])
        t = self.quadro.criar('Task', repo='web-app')
        self.assertFalse(self.svc.ciclo_worker())
        self.assertEqual(self.quadro.ver(t['id'])['estado'], 'pendente')
        self.assertEqual(self.codex.chamadas, [])

    def test_sem_dados_nao_altera_motor_e_optout_preserva_limite(self):
        self.uso(400, 100)
        self.estado.set('motor', 'claude')
        self.svc.claude = ClaudeFalso()
        self.assertEqual(self.svc.motor_efetivo(), 'claude')
        self.estado.set('motor', 'auto')
        self.cfg.balance_enabled = False
        self.assertEqual(self.svc.motor_efetivo(), 'claude')
        self.assertEqual(self.svc.balanceamento()['tasks_per_hour'], self.cfg.tarefas_por_hora)
        self.svc._registrar_claude_limite(engines.LimiteInfo('session', self.t[0]+100), 'test')
        self.assertEqual(self.svc.motor_efetivo(), 'codex')

    def test_alerta_bidirecional_sem_repeticao_e_rearma(self):
        self.uso(900, 100)
        for _ in range(2):
            self.svc.alertar_balanceamento()
        self.assertEqual(len(self.tr.textos()), 1)
        self.assertIn('Claude 90%', self.tr.textos()[0])
        self.cfg.balance_enabled = False
        self.svc.alertar_balanceamento()
        self.assertEqual(len(self.tr.textos()), 1)
        self.cfg.balance_enabled = True
        self.estado.registrar_execucao('turno', 'codex', 0, True, agora=self.t[0], tokens_in=800)
        self.svc.alertar_balanceamento()
        self.cfg.claude_tokens_5h = 10000
        self.svc.alertar_balanceamento()
        self.assertEqual(len(self.tr.textos()), 2)
        self.assertIn('Codex 90%', self.tr.textos()[-1])

    def test_alerta_limites_estritos_70_e_50(self):
        self.uso(700, 100)
        self.svc.alertar_balanceamento()
        self.assertEqual(self.tr.textos(), [])
        self.estado.registrar_execucao('turno', 'claude', 0, True, agora=self.t[0], tokens_in=50)
        self.estado.registrar_execucao('turno', 'codex', 0, True, agora=self.t[0], tokens_in=400)
        self.svc.alertar_balanceamento()
        self.assertEqual(self.tr.textos(), [])

    def test_consulta_codex_cacheada_falha_preserva_amostra_sem_chamada_de_modelo(self):
        self.cotas()
        resposta = {'ok': True, 'primaria': {'usado_pct': 10, 'duracao_min': 300}}
        with mock.patch.object(engines, 'uso_codex', return_value=resposta) as consulta, mock.patch.object(
                self.codex, 'executar', side_effect=AssertionError('must not call a model')):
            self.assertTrue(self.svc.atualizar_uso_codex())
            self.assertFalse(self.svc.atualizar_uso_codex())
            self.assertEqual(consulta.call_count, 1)
            self.svc.status()
            self.svc.balanceamento()
            self.assertEqual(consulta.call_count, 1)
            self.t[0] += self.cfg.balance_refresh_seconds
            consulta.return_value = {'ok': False}
            self.assertFalse(self.svc.atualizar_uso_codex())
            self.assertEqual(self.estado.get('codex_uso_balance')['dados'], resposta)

    def test_hook_prompt_injeta_economia_sem_diffs_ou_consulta_remota(self):
        self.uso(400, 100)
        def chamar(cfg, req, **kw):
            return self.svc.comando(req)
        with mock.patch.object(cli, 'chamar', side_effect=chamar), mock.patch.object(
                engines, 'uso_codex', side_effect=AssertionError('hook must stay local')), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli._hook_prompt(self.cfg, 'UserPromptSubmit'), 0)
        self.assertIn('modo economico: delegue revisao/testes ao Codex, use result --short, nao leia diffs', out.getvalue())

    def test_stop_cli_so_encaminha_id_sempre_exit_zero_sem_conteudo(self):
        entrada = {'session_id': 'session-fixture', 'transcript_path': '/not/allowed', 'last_assistant_message': 'fixture-private'}
        with mock.patch.object(cli, '_cfg', return_value=self.cfg), mock.patch.object(cli, 'chamar', return_value={'ok': False}) as call, mock.patch(
                'sys.stdin', io.StringIO(json.dumps(entrada))), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main(['claude-usage']), 0)
        self.assertEqual(call.call_args.args[1], {'cmd': 'claude_usage', 'session_id': 'session-fixture'})
        self.assertLessEqual(call.call_args.kwargs['timeout'], 4)
        self.assertEqual(out.getvalue(), '')
        with mock.patch.object(cli, '_cfg', return_value=self.cfg), mock.patch('sys.stdin', io.StringIO('bad json')):
            self.assertEqual(cli.main(['claude-usage']), 0)

    def test_stop_servico_importa_contadores_retornados_pelo_espelho_da_sessao(self):
        r = self.registro()
        with mock.patch.object(self.root, 'espelhar_uso', return_value={'ok': True, 'contadores': [r]}):
            self.assertTrue(self.svc.comando({'cmd': 'claude_usage', 'session_id': 'session-fixture'})['ok'])
            self.svc.claude_usage('session-fixture')
        rows = self.estado.execucoes_desde(0)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]['tokens_in'], rows[0]['tokens_out']), (100, 20))

    @needs_root
    def test_root_stop_so_espelha_sessao_sem_repos_memoria_ou_conversa_na_resposta(self):
        self.cfg.fonte_transcripts = self.dir / 'transcripts'
        self.cfg.fonte_transcripts.mkdir()
        self.cfg.fonte_memoria = self.dir / 'absent'
        raw = {'type': 'assistant', 'timestamp': '1970-01-01T00:16:40Z', 'message': {
            'id': 'fixture-message', 'content': 'fixture-private', 'usage': {'input_tokens': 10, 'output_tokens': 2}}}
        (self.cfg.fonte_transcripts / 'fixture-session.jsonl').write_text(json.dumps(raw))
        with mock.patch.object(espelho, 'atualizar_repos', side_effect=AssertionError('no repositories')), mock.patch.object(
                espelho, '_espelhar_memoria', side_effect=AssertionError('no memory')):
            r = root_daemon.processar(self.cfg, {'op': 'claude_usage', 'session_id': 'fixture-session'}, 0, 0, b'fake')
        self.assertTrue(r['ok'])
        self.assertEqual(r['contadores'][0]['tokens_in'], 10)
        self.assertNotIn('fixture-private', json.dumps(r))
        mirror = self.cfg.mirror / 'transcript-cauda.jsonl'
        before = mirror.read_bytes()
        self.assertFalse(root_daemon.processar(self.cfg, {'op': 'claude_usage', 'session_id': '../outside'}, 0, 0, b'fake')['ok'])
        self.assertEqual(mirror.read_bytes(), before)
        self.assertFalse(root_daemon.processar(self.cfg, {'op': 'claude_usage'}, 999, 1000, b'fake')['ok'])

    def test_stop_refresh_falha_nao_importa_cauda_de_outra_sessao(self):
        (self.cfg.mirror / 'transcript-cauda.jsonl').write_text(json.dumps(self.registro()))
        with mock.patch.object(self.root, 'espelhar_uso', return_value={'ok': False}):
            self.assertFalse(self.svc.claude_usage('missing')['ok'])
        self.assertEqual(self.estado.execucoes_desde(0), [])

    def test_result_curto_padrao_e_full_preserva_registro(self):
        t = self.quadro.criar('Task', descricao='long description', repo='r')
        self.quadro.atualizar(t['id'], resumo='\n'.join(f'line {n}' for n in range(15)), branch='agent/test',
                              diffstat='a.py | 2 ++', aviso='review deletions', erro='incomplete')
        for cmd in ('resultado', 'ver'):
            r = self.svc.comando({'cmd': cmd, 'id': t['id'], 'short': True})['tarefa']
            self.assertEqual(len(r['resumo'].splitlines()), 10)
            self.assertEqual((r['branch'], r['diffstat'], r['aviso'], r['erro']),
                             ('agent/test', 'a.py | 2 ++', 'review deletions', 'incomplete'))
            self.assertNotIn('descricao', r)
        self.assertNotIn('descricao', self.svc.comando({'cmd': 'resultado', 'id': t['id']})['tarefa'])
        full = self.svc.comando({'cmd': 'resultado', 'id': t['id'], 'full': True})['tarefa']
        self.assertEqual(full['descricao'], 'long description')
        self.assertEqual(len(full['resumo'].splitlines()), 15)
        self.assertIn('descricao', self.svc.comando({'cmd': 'ver', 'id': t['id']})['tarefa'])

    def test_cli_result_short_default_full_e_status_cinco_linhas(self):
        self.uso(420, 180)
        t = self.quadro.criar('Task')
        self.quadro.atualizar(t['id'], resumo='\n'.join(f'line {n}' for n in range(15)))
        def chamar(cfg, req, **kw):
            return self.svc.comando(req)
        with mock.patch.object(cli, '_cfg', return_value=self.cfg), mock.patch.object(cli, 'chamar', side_effect=chamar):
            for args in (['result', str(t['id'])], ['result', str(t['id']), '--short']):
                with contextlib.redirect_stdout(io.StringIO()) as out:
                    self.assertEqual(cli.main(args), 0)
                self.assertIn('Status:', out.getvalue())
                self.assertNotIn('line 10', out.getvalue())
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(cli.main(['result', str(t['id']), '--full']), 0)
            self.assertIn('line 14', out.getvalue())
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(cli.main(['status', '--brief']), 0)
            self.assertEqual(len(out.getvalue().splitlines()), 5)
            self.assertIn('Claude 42% · Codex 18% · razao 2.3', out.getvalue())

    def test_tarefa_persiste_tokens_diffstat_e_status(self):
        self.criar_repo()
        def executar(prompt, cwd):
            (Path(cwd) / 'counter.py').write_text('value = 7\n')
            return engines.Resultado(ok=True, texto='Estado: concluida', tokens_in=120, tokens_out=30)
        self.codex.respostas = [executar]
        t = self.quadro.criar('Task', repo='web-app')
        self.assertTrue(self.svc.ciclo_worker())
        row = self.estado.execucoes_desde(0, 'tarefa')[0]
        self.assertEqual((row['tokens_in'], row['tokens_out']), (120, 30))
        short = self.svc.comando({'cmd': 'resultado', 'id': t['id']})['tarefa']
        self.assertIn('counter.py', short['diffstat'])
        status = self.svc.status()
        self.assertEqual(status['uso']['janelas']['5h']['codex']['tokens_out'], 30)
        self.assertEqual((status['execucoes'][0]['tokens_in'], status['execucoes'][0]['tokens_out']), (120, 30))

    def test_config_toml_env_alavancas_e_doctor_somente_leitura(self):
        p = self.dir / 'config.toml'
        p.write_text('[balance]\nclaude_tokens_5h=1000\nclaude_tokens_7d=10000\nratio_high=1.6\nlow_ratio_tasks_factor=0.25\n')
        c = config.carregar(env={'AAO_BALANCE_CODEX_TOKENS_5H': '800', 'AAO_SERVICE_TASKS_PER_HOUR': '8',
                                'AAO_SERVICE_HANDOFF_CHECKS_PER_DAY': '2'}, arquivo=p)
        self.assertEqual((c.claude_tokens_5h, c.codex_tokens_5h, c.balance_ratio_high, c.balance_tasks_factor), (1000, 800, 1.6, 0.25))
        self.assertEqual((c.tarefas_por_hora, c.acordadas_por_dia), (8, 2))
        from tests.test_doctor_setup import AmbienteFalso
        antes = self.cfg.db_estado.read_bytes()
        itens = doctor.checar_medicao(self.cfg, AmbienteFalso())
        self.assertEqual(self.cfg.db_estado.read_bytes(), antes)
        por_nome = {i.nome: i.estado for i in itens}
        self.assertEqual(por_nome['usage schema'], doctor.OK)
        self.assertEqual(por_nome['usage quotas'], doctor.AVISO)
        self.cfg.balance_ratio_low = float('nan')
        self.assertEqual(doctor.checar_medicao(self.cfg, AmbienteFalso())[0].estado, doctor.FALHA)
        self.assertEqual(self.svc.balanceamento()['modo'], 'invalid')

    def test_concorrencia_stop_duplo_e_alerta_unico(self):
        self.uso(900, 100)
        barrier = threading.Barrier(4)
        r = self.registro(entrada=0, saida=1)
        def hook():
            barrier.wait(timeout=5)
            return self.svc.claude_usage('same-session')
        with mock.patch.object(self.root, 'espelhar_uso', return_value={'ok': True, 'contadores': [r]}):
            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(lambda _: hook(), range(4)))
        self.assertTrue(all(r['ok'] for r in results))
        self.assertEqual(len(self.estado.execucoes_desde(0)), 3)
        self.assertEqual(len(self.tr.textos()), 1)

    def test_sessoes_simultaneas_nao_leem_cauda_sobrescrita(self):
        barrier = threading.Barrier(2)
        def refresh(sessao):
            reg = self.registro(ident=sessao, entrada=10 if sessao == 'a' else 20, saida=0)
            # The on-disk shared tail is allowed to change before either consumer runs.
            barrier.wait(timeout=5)
            return {'ok': True, 'contadores': [reg]}
        with mock.patch.object(self.root, 'espelhar_uso', side_effect=refresh), ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(self.svc.claude_usage, ['a', 'b']))
        rows = self.estado.execucoes_desde(0)
        self.assertEqual(len(rows), 2)
        self.assertEqual(sum(r['tokens_in'] for r in rows), 30)


    def test_contadores_sidechain_na_cauda_sem_conversa(self):
        from ai_agents_orchestrator import medicao
        r = medicao.registro_uso({'type': 'assistant', 'isSidechain': True, 'timestamp': '1970-01-01T00:16:40Z',
                                 'message': {'id': 'sub-fixture', 'content': 'fixture-private',
                                             'usage': {'input_tokens': 10, 'output_tokens': 2}}}, 'session')
        self.assertEqual((r['tokens_in'], r['tokens_out']), (10, 2))
        self.assertNotIn('fixture-private', json.dumps(r))

    def processo_rpc(self, dados):
        leitura, escrita = os.pipe()
        os.write(escrita, dados)
        self.addCleanup(os.close, escrita)
        class Processo:
            pid = 987654321  # synthetic; killpg is intercepted below
            stdin = io.BytesIO()
            stdout = os.fdopen(leitura, 'rb')
            wait = mock.Mock(return_value=0)
        self.addCleanup(Processo.stdout.close)
        return Processo()

    def test_rpc_codex_recebe_linhas_e_fecha_processo_sem_rede(self):
        p = self.processo_rpc(b'bad json\n{"id":1,"result":{}}\n{"id":2,"result":{"rateLimits":{"primary":{"usedPercent":42,"windowDurationMins":300}}}}\n')
        with mock.patch.object(engines.subprocess, 'Popen', return_value=p), mock.patch.object(engines.os, 'killpg') as kill:
            r = engines.uso_codex(self.cfg.state_dir)
        self.assertEqual(r['primaria']['usado_pct'], 42)
        kill.assert_called_once_with(p.pid, engines.signal.SIGTERM)
        self.assertTrue(p.stdin.closed and p.stdout.closed)
        p.wait.assert_called_once_with(timeout=1)

    def test_rpc_linha_incompleta_expira_sem_readline_bloqueante(self):
        p = self.processo_rpc(b'{"id":2')
        seletor = mock.Mock()
        seletor.select.side_effect = [[object()], []]  # readiness followed by the injected timeout event
        with mock.patch.object(engines.subprocess, 'Popen', return_value=p), mock.patch.object(engines.os, 'killpg'), mock.patch.object(
                engines.selectors, 'DefaultSelector', return_value=seletor), mock.patch.object(
                engines.time, 'monotonic', side_effect=[0, 0, 0]):
            r = engines.uso_codex(self.cfg.state_dir, timeout=3)
        self.assertFalse(r['ok'])
        self.assertEqual(r['erro'], 'no answer')
        self.assertEqual(seletor.select.call_args_list, [mock.call(3), mock.call(3)])
        self.assertTrue(p.stdout.closed)


if __name__ == '__main__':
    unittest.main()
