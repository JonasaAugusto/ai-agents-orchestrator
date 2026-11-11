import unittest

from ai_agents_orchestrator.botoes import Botoes
from ai_agents_orchestrator.telegram import Telegram, TelegramErro
from ai_agents_orchestrator.engines import Resultado
from tests.helpers import Base, CHAT, TOKEN, FakeTransport


class Allowlist(Base):
    def test_outro_chat_e_ignorado_sem_resposta(self):
        for chat in (111, -100123):
            self.bot.processar(self.msg("/status", chat=chat))
        self.bot.processar(self.msg("/status", tipo="group"))  # mesmo id, mas grupo
        self.bot.processar(self.msg("/status", de=222))  # remetente diferente
        self.assertEqual(self.tr.enviados(), [])

    def test_callback_de_outro_chat_e_ignorado(self):
        self.bot.processar(self.msg("/pausar"))
        data = self.botoes_enviados()[0][1]
        n = len(self.tr.chamadas)
        self.bot.processar(self.clique(data, chat=111))
        self.assertEqual(len(self.tr.chamadas), n)  # nem answerCallbackQuery
        self.assertFalse(self.estado.get("pausado", False))

    def test_mensagem_grande_demais(self):
        self.bot.processar(self.msg("x" * (self.cfg.max_msg_in + 1)))
        self.assertIn("too long", self.tr.textos()[-1])
        self.assertEqual(self.quadro.listar(), [])


class Botoes_(Base):
    def test_confirmar_executa_e_nao_pode_repetir(self):
        self.bot.processar(self.msg("/pausar"))
        self.assertFalse(self.estado.get("pausado", False))  # nada muda antes do botao
        data = dict(self.botoes_enviados())["Confirm"]
        self.bot.processar(self.clique(data))
        self.assertTrue(self.estado.get("pausado"))
        self.estado.set("pausado", False)
        self.bot.processar(self.clique(data))  # replay
        self.assertFalse(self.estado.get("pausado"))
        self.assertTrue(any(p.get("text") == "This button was already used." for m, p in self.tr.chamadas if m == "answerCallbackQuery"))

    def test_cancelar_invalida_o_grupo_inteiro(self):
        self.bot.processar(self.msg("/pausar"))
        b = dict(self.botoes_enviados())
        self.bot.processar(self.clique(b["Cancel"]))
        self.bot.processar(self.clique(b["Confirm"]))
        self.assertFalse(self.estado.get("pausado", False))

    def test_expira_em_10_minutos(self):
        self.bot.processar(self.msg("/pausar"))
        data = dict(self.botoes_enviados())["Confirm"]
        self.t[0] += 601
        self.bot.processar(self.clique(data))
        self.assertFalse(self.estado.get("pausado", False))
        self.assertTrue(any("expired" in p.get("text", "") for m, p in self.tr.chamadas if m == "answerCallbackQuery"))

    def test_assinatura_forjada_e_id_inexistente(self):
        self.bot.processar(self.msg("/pausar"))
        aid, mac = dict(self.botoes_enviados())["Confirm"].split(":")
        for falso in (f"{aid}:{'0' * 12}", "deadbeef:" + mac, "lixo", f"{aid}:"):
            self.bot.processar(self.clique(falso))
        self.assertFalse(self.estado.get("pausado", False))

    def test_assinatura_de_outro_token_nao_vale(self):
        outro = Botoes(self.estado, "999:outro-token", CHAT)
        kb = outro.teclado("pausar", {}, [("Confirm", "sim")])
        self.bot.processar(self.clique(kb[0][0][1]))
        self.assertFalse(self.estado.get("pausado", False))

    def test_nao_existe_botao_de_publicar_em_producao_sem_merge(self):
        # o unico botao de producao nasce depois do merge confirmado e exige HMAC; nenhum comando o cria
        for cmd in ("/ajuda", "/status", "/quadro", "/auto", "/motor", "/model"):
            self.bot.processar(self.msg(cmd))
        rotulos = " ".join(r.lower() for r, _ in self.botoes_enviados())
        self.assertNotIn("producao", rotulos)
        self.assertNotIn("publicar", rotulos)


class Comandos(Base):
    def test_setmycommands_registra_o_menu(self):
        self.tg.registrar_comandos()
        m, p = self.tr.chamadas[-1]
        nomes = {c["command"] for c in p["commands"]}
        self.assertEqual(m, "setMyCommands")
        for c in ("help", "status", "model", "effort", "usage", "engine", "repos", "repo", "task", "context", "resume", "pause",
                  "cancel", "last", "diff", "branches", "logs", "auto", "limit", "board", "history", "cap", "new"):
            self.assertIn(c, nomes)

    def test_tarefa_exige_confirmacao_e_aceita_editar(self):
        self.cfg.repos = {"web-app": {"descricao": "site"}}
        self.bot.processar(self.msg("/tarefa web-app corrigir botao"))
        self.assertEqual(self.quadro.listar(), [])
        self.apertar("Edit")
        self.bot.processar(self.msg("corrigir o botao de login"))
        self.assertEqual(self.quadro.listar(), [])  # ainda precisa confirmar de novo
        self.apertar("Confirm")
        t = self.quadro.listar()[0]
        self.assertEqual((t["repo"], t["dono"], t["estado"], t["origem"]), ("web-app", "codex", "pendente", "dono"))
        self.assertIn("login", t["descricao"])

    def _em_revisao(self, origem):
        t = self.quadro.criar("Muda", "", "web-app", "codex", origem)
        self.quadro.atualizar(t["id"], estado="em_revisao")
        return t["id"]

    def test_texto_livre_vira_conversa_com_codex_sem_tarefa(self):
        self.codex.respostas = [Resultado(ok=True, texto="Oi, dono.", thread_id="T1", segundos=1),
                                Resultado(ok=True, texto="Segunda.", thread_id="T1", segundos=1)]
        self.bot.processar(self.msg("como esta o projeto?"))
        self.assertEqual(self.tr.textos()[-1], "Oi, dono.")
        self.assertEqual(self.botoes_enviados(), [])
        self.assertNotIn("repository", self.tr.textos()[-1])
        self.assertEqual(self.quadro.listar(), [])
        c = self.codex.chamadas[0]
        self.assertEqual(c["sandbox"], "read-only")
        self.assertIn("BRIEFING.md", c["prompt"])
        self.assertIsNone(c["resume"])
        self.bot.processar(self.msg("e depois?"))
        self.assertEqual(self.codex.chamadas[1]["resume"], "T1")  # continuidade
        self.assertTrue(self.codex.chamadas[1]["prompt"].endswith("e depois?"))  # com o snapshot de execucao na frente
        self.assertIn("EXECUTION SNAPSHOT", self.codex.chamadas[1]["prompt"])
        self.assertEqual(self.tr.textos()[-1], "Segunda.")

    def test_texto_de_publicar_vai_ao_codex_com_salvaguarda_e_sem_tarefa(self):
        self.bot.processar(self.msg("Pode subir pra producao"))
        self.assertIn("do NOT publish", self.codex.chamadas[0]["prompt"])
        self.assertEqual(self.quadro.listar(), [])

    def test_conversa_falha_avisa_sem_travar(self):
        self.codex.respostas = [Resultado(ok=False, erro="boom", segundos=1)]
        self.bot.processar(self.msg("oi"))
        self.assertIn("I could not answer", self.tr.textos()[-1])

    def test_tarefa_formal_continua_pedindo_confirmacao(self):
        self.cfg.repos = {"site": {}}
        self.bot.processar(self.msg("/tarefa site ajustar o rodape"))
        self.apertar("Confirm")
        t = self.quadro.listar()[0]
        self.assertEqual((t["origem"], t["descricao"]), ("dono", "ajustar o rodape"))

    def test_aprovacao_curta_mostra_botoes_da_revisao(self):
        tid = self._em_revisao("dono")
        self.bot.processar(self.msg("Pode subir pra produção"))
        self.assertIn("Publish", [r for r, _ in self.botoes_enviados()])
        self.assertEqual(len(self.quadro.listar()), 1)  # nao criou tarefa nova
        self.assertIn(f"#{tid}", self.tr.textos()[-1])

    def test_aprovacao_curta_com_varias_lista_e_nao_cria(self):
        a, b = self._em_revisao("dono"), self._em_revisao("handoff")
        self.bot.processar(self.msg("pode publicar"))
        txt = "\n".join(self.tr.textos())
        self.assertIn("more than one", txt)
        self.assertIn(f"#{a}", txt)
        self.assertIn(f"#{b}", txt)
        self.assertEqual(len(self.quadro.listar()), 2)

    def test_aprovacao_ignora_revisao_de_origem_claude(self):
        self._em_revisao("claude")
        self.cfg.repos = {}
        self.criar_repo("site")
        self.bot.processar(self.msg("/repo site"))
        self.apertar("Confirm")
        self.bot.processar(self.msg("pode publicar"))
        self.assertNotIn("Publish", [r for r, _ in self.botoes_enviados()])
        self.assertEqual(len(self.codex.chamadas), 1)  # foi para a conversa, nao para a revisao do Claude

    def test_repo_padrao_e_tarefa_sem_repo(self):
        self.cfg.repos = {"site": {}, "outro": {}}
        self.bot.processar(self.msg("/tarefa fazer algo"))
        self.assertIn("In which repository", self.tr.textos()[-1])
        self.bot.processar(self.msg("/repo site"))
        self.apertar("Confirm")
        self.bot.processar(self.msg("/tarefa fazer algo"))
        self.apertar("Confirm")
        self.assertEqual(self.quadro.listar()[0]["repo"], "site")

    def test_motor_model_auto(self):
        self.bot.processar(self.msg("/motor"))
        self.apertar("Auto")
        self.assertEqual(self.estado.get("motor"), "auto")
        self.assertTrue(self.svc.auto())  # padrao: ligado
        self.bot.processar(self.msg("/auto"))
        self.apertar("Turn off")
        self.assertFalse(self.svc.auto())

    def test_status_mostra_estado_sem_segredos(self):
        self.bot.processar(self.msg("/status"))
        t = self.tr.textos()[-1]
        self.assertIn("Engine: codex", t)
        self.assertNotIn(TOKEN, t)

    def test_quadro_tem_botoes_ver_atribuir_cancelar(self):
        self.quadro.criar("tarefa x", repo="")
        self.bot.processar(self.msg("/quadro"))
        r = [x for x, _ in self.botoes_enviados()]
        self.assertEqual(r, ["View", "Assign to Codex", "Assign to Claude", "Cancel"])
        self.apertar("Assign to Claude")
        self.assertEqual(self.quadro.listar()[0]["dono"], "claude")

    def test_limite_mostra_evidencia(self):
        self.estado.set("claude_limite", {"ativo": True, "reset_epoch": 1800000000, "tipo": "sessao", "evidencia": "You've hit your session limit"})
        self.bot.processar(self.msg("/limite"))
        self.assertIn("session limit", self.tr.textos()[-1])

    def test_logs_sao_mascarados(self):
        self.svc.log("erro com OPENAI_API_KEY=sk-ABCDEFGHIJKLMNOPQRSTUV e cpf 123.456.789-09")
        self.bot.processar(self.msg("/logs"))
        t = self.tr.textos()[-1]
        self.assertNotIn("sk-ABCDEFGHIJKLMNOPQRSTUV", t)

    def test_comando_desconhecido(self):
        self.bot.processar(self.msg("/rm"))
        self.assertIn("nknown", self.tr.textos()[-1])


class TelegramCliente(unittest.TestCase):
    def test_mensagem_longa_e_dividida_e_botoes_so_no_ultimo(self):
        tr = FakeTransport()
        tg = Telegram(TOKEN, CHAT, max_out=100, transporte=tr)
        tg.enviar("a" * 250, [[("ok", "x:y")]])
        env = tr.enviados()
        self.assertEqual([len(p["text"]) for p in env], [100, 100, 50])
        self.assertNotIn("reply_markup", env[0])
        self.assertIn("reply_markup", env[2])

    def test_erro_nao_vaza_o_token(self):
        def t(m, p, to):
            raise ConnectionError(f"https://api.telegram.org/bot{TOKEN}/x")
        from ai_agents_orchestrator import telegram
        orig = telegram._ConexaoIPv4.connect
        telegram._ConexaoIPv4.connect = lambda self: (_ for _ in ()).throw(OSError(f"falha em {TOKEN}"))
        try:
            with self.assertRaises(TelegramErro) as cm:
                telegram._http(TOKEN, "getMe", {}, 1)
            self.assertNotIn(TOKEN, str(cm.exception))
        finally:
            telegram._ConexaoIPv4.connect = orig


class Polling(Base):
    def test_offset_persistido_e_so_dono_processado(self):
        self.tr.updates = [{**self.msg("/ajuda"), "update_id": 40}, {**self.msg("/ajuda", chat=7), "update_id": 41}]
        self.svc.parar_ev.set()
        # uma rodada manual do laco
        for u in self.tg.atualizacoes(0):
            self.estado.set("offset", u["update_id"] + 1)
            self.bot.processar(u)
        self.assertEqual(self.estado.get("offset"), 42)
        self.assertEqual(len(self.tr.enviados()), 1)


if __name__ == "__main__":
    unittest.main()


class RepoETeto(Base):
    def setUp(self):
        super().setUp()
        self.cfg.repos = {"site": {}, "web-app": {}, "orchestrator": {}}
        self.cfg.self_repo = "orchestrator"  # the orchestrator's own repo (service.self_repo)

    def test_texto_ambiguo_mostra_botoes_e_cria_apos_escolha(self):
        self.bot.processar(self.msg("/tarefa mude a cor do cabecalho"))
        self.assertIn("In which repository", self.tr.textos()[-1])
        self.assertNotIn("Escolha o repositorio", self.tr.textos()[-1])
        self.assertIn("site", [r for r, _ in self.botoes_enviados()])
        self.apertar("site")
        self.assertIn("mude a cor", self.tr.textos()[-1])
        self.apertar("Confirm")
        t = self.quadro.listar()[0]
        self.assertEqual((t["repo"], t["descricao"]), ("site", "mude a cor do cabecalho"))

    def test_texto_sobre_o_bot_escolhe_o_ai_agents_orchestrator_sozinho(self):
        self.estado.set("repo_padrao", "site")
        self.bot.processar(self.msg("/tarefa desativa esse teto diario de acionamento, ta em loop"))
        self.assertIn("'orchestrator'", self.tr.textos()[-1])
        self.assertEqual(self.bot._repo_citado("o comando /effort do bot esta estranho"), "orchestrator")
        self.assertIsNone(self.bot._repo_citado("mude a cor do cabecalho"))

    def test_texto_citando_repo_usa_o_repo(self):
        self.bot.processar(self.msg("/tarefa ajuste o rodape do web-app"))
        self.assertIn("'web-app'", self.tr.textos()[-1])

    def test_repo_unico_e_usado(self):
        self.cfg.repos = {"site": {}}
        self.bot.processar(self.msg("/tarefa mude a cor"))
        self.assertIn("'site'", self.tr.textos()[-1])

    def test_repo_padrao_e_usado(self):
        self.estado.set("repo_padrao", "web-app")
        self.bot.processar(self.msg("/tarefa mude a cor"))
        self.assertIn("'web-app'", self.tr.textos()[-1])

    def test_teto_ver_e_ajustar(self):
        self.bot.processar(self.msg("/teto"))
        self.assertIn(f"{self.cfg.acordadas_por_dia}", self.tr.textos()[-1])
        self.bot.processar(self.msg("/teto 12"))
        self.assertEqual(self.svc.teto_acordadas(), 12)
        self.bot.processar(self.msg("/teto 0"))
        self.assertEqual(self.svc.teto_acordadas(), 12)


class Effort(Base):
    CAT = [{"slug": "m1", "nome": "M1", "niveis": ["low", "medium", "high", "xhigh", "max"], "padrao": "low"},
           {"slug": "m2", "nome": "M2", "niveis": ["low", "medium", "high", "xhigh"], "padrao": "medium"}]

    def setUp(self):
        super().setUp()
        from ai_agents_orchestrator import engines
        self._orig = engines.modelos_codex
        engines.modelos_codex = lambda *a, **k: list(self.CAT)
        self.addCleanup(setattr, engines, "modelos_codex", self._orig)

    def test_mostra_atual_e_botoes_so_validos(self):
        self.bot.processar(self.msg("/effort"))
        self.assertIn("Codex default", self.tr.textos()[-1])
        r = [x for x, _ in self.botoes_enviados()]
        self.assertEqual(r, ["Codex default", "low", "medium", "high", "xhigh", "Cancel"])

    def test_botao_aplica_persiste_e_confirma(self):
        self.bot.processar(self.msg("/effort"))
        self.apertar("high")
        self.assertEqual(self.estado.get("esforco"), "high")
        self.assertIn("Effort: high", self.tr.textos()[-1])
        self.bot.processar(self.msg("/status"))
        self.assertIn("Effort: high", self.tr.textos()[-1])
        self.bot.processar(self.msg("/effort"))
        self.apertar("Codex default")
        self.assertIsNone(self.estado.get("esforco"))

    def test_botao_hmac_uso_unico_e_expira(self):
        self.bot.processar(self.msg("/effort"))
        d = dict(self.botoes_enviados())
        self.bot.processar(self.clique(d["low"]))
        self.bot.processar(self.clique(d["high"]))  # grupo ja consumido
        self.assertEqual(self.estado.get("esforco"), "low")
        self.bot.processar(self.msg("/effort"))
        d = dict(self.botoes_enviados())
        self.t[0] += 601
        self.bot.processar(self.clique(d["xhigh"]))
        self.assertEqual(self.estado.get("esforco"), "low")
        self.bot.processar(self.msg("/effort"))
        aid, _ = dict(self.botoes_enviados())["medium"].split(":")
        self.bot.processar(self.clique(aid + ":000000000000"))
        self.assertEqual(self.estado.get("esforco"), "low")

    def test_texto_valida(self):
        self.bot.processar(self.msg("/effort ULTRA"))  # sem modelo escolhido: so niveis comuns
        self.assertIn("invalid", self.tr.textos()[-1].lower())
        self.assertIsNone(self.estado.get("esforco"))
        self.bot.processar(self.msg("/effort banana"))
        self.assertIsNone(self.estado.get("esforco"))
        self.bot.processar(self.msg("/effort Medium"))
        self.assertEqual(self.estado.get("esforco"), "medium")
        self.bot.processar(self.msg("/effort padrao"))
        self.assertIsNone(self.estado.get("esforco"))

    def test_combina_com_o_modelo(self):
        self.estado.set("modelo", "m1")
        self.bot.processar(self.msg("/effort max"))
        self.assertEqual(self.estado.get("esforco"), "max")
        self.assertIn("m1", self.tr.textos()[-1])

    def test_aplicado_nas_chamadas_ao_codex(self):
        self.estado.set("esforco", "high")
        from tests.test_servico import contexto
        self.criar_repo()
        self.cfg.armado_flag.touch()
        (self.cfg.state_dir / "para-codex").mkdir(exist_ok=True)
        (self.cfg.state_dir / "para-codex" / "BRIEFING.md").write_text("# briefing\n")
        self.svc.codex.respostas = [Resultado(ok=True, texto=contexto(), segundos=5)]
        self.svc.claude_limite({"error": "rate_limit", "last_assistant_message":
                                "You've hit your session limit · resets 2:40pm (America/Montevideo)"})
        self.assertTrue(self.svc.codex.chamadas)
        self.assertTrue(all(c["esforco"] == "high" for c in self.svc.codex.chamadas))
