import re
from pathlib import Path

from ai_agents_orchestrator.engines import Resultado
from tests.helpers import Base, sh


def edita(nome="mudanca.py", conteudo="print('chat')\n", texto="Feito: ajustei."):
    def f(prompt, cwd):
        (Path(cwd) / nome).write_text(conteudo)
        return Resultado(ok=True, texto=texto, thread_id="TC1", segundos=2)
    return f


def so_responde(prompt, cwd):
    return Resultado(ok=True, texto="Nada a mudar.", thread_id="TC1", segundos=1)


class ChatEditavel(Base):
    def setUp(self):
        super().setUp()
        self.gh = self.criar_repo()

    def test_edicao_gera_commit_branch_push_e_botoes(self):
        self.codex.respostas = [edita()]
        self.bot.processar(self.msg("ajusta o app do web-app"))
        c = self.codex.chamadas[0]
        self.assertEqual(c["sandbox"], "workspace-write")
        self.assertIn("/workspaces/web-app/", c["cwd"])
        t = self.quadro.listar()[0]
        self.assertEqual((t["kind"], t["origem"], t["estado"]), ("chat", "dono", "em_revisao"))
        self.assertRegex(t["branch"], r"^agent/chat-\d{8}-1$")
        self.assertEqual(t["alterados"], ["mudanca.py"])
        ws = Path(t["workspace"])
        msg = sh("git", "log", "-1", "--format=%B%an", cwd=ws)
        for proibido in ("Co-Authored-By", "Claude", "Codex", "OpenAI"):
            self.assertNotIn(proibido, msg)
        self.assertIn(t["branch"], sh("git", "--git-dir", str(self.gh), "branch"))  # push do branch (daemon root)
        self.assertIn(("push_branch", "web-app", t["id"], t["branch"]), self.root.chamadas)
        self.assertEqual([r for r, _ in self.botoes_enviados()], ["Publish", "Review first", "Discard"])
        self.assertIn("mudanca.py", self.tr.textos()[-1])
        self.assertFalse(any("Starting" in x for x in self.tr.textos()))

    def test_mesma_sessao_reaproveita_branch_e_novo_abre_outro(self):
        self.codex.respostas = [edita("a.py"), edita("b.py"), edita("c.py")]
        self.bot.processar(self.msg("faz a no web-app"))
        self.bot.processar(self.msg("agora b"))
        ts = self.quadro.listar()
        self.assertEqual(len(ts), 1)
        self.assertEqual(self.codex.chamadas[1]["resume"], "TC1")
        self.assertEqual(ts[0]["alterados"], ["a.py", "b.py"])
        self.bot.processar(self.msg("/novo"))
        self.bot.processar(self.msg("faz c no web-app"))
        ts = self.quadro.listar()
        self.assertEqual(len(ts), 2)
        self.assertTrue(ts[0]["branch"].endswith("-2"))

    def test_inatividade_de_6h_abre_branch_novo(self):
        self.codex.respostas = [edita("a.py"), edita("b.py")]
        self.bot.processar(self.msg("faz a no web-app"))
        self.t[0] += 6 * 3600 + 5
        self.bot.processar(self.msg("faz b"))
        self.assertEqual(len(self.quadro.listar()), 2)

    def test_sem_mudanca_nao_gera_botoes(self):
        self.codex.respostas = [so_responde]
        self.bot.processar(self.msg("o que faz o app do web-app?"))
        self.assertEqual(self.botoes_enviados(), [])
        self.assertTrue(self.tr.textos()[-1].startswith("Nada a mudar."))
        self.assertEqual(self.quadro.listar()[0]["estado"], "concluida")
        self.assertNotIn("push_branch", [c[0] for c in self.root.chamadas])

    def test_repo_ambiguo_pergunta_em_somente_leitura(self):
        sh("git", "clone", "-q", "--mirror", str(self.cfg.repos_dir / "web-app.git"), str(self.cfg.repos_dir / "site.git"))
        self.cfg.repos["site"] = {"descricao": "y", "branch_base": "main", "github_url": str(self.gh)}
        self.codex.respostas = [Resultado(ok=True, texto="Qual repositorio?", thread_id="R1", segundos=1)]
        self.bot.processar(self.msg("muda a cor do botao"))
        c = self.codex.chamadas[0]
        self.assertEqual(c["sandbox"], "read-only")
        self.assertIn("site", c["prompt"])
        self.assertIn("ONE short sentence", c["prompt"])
        self.assertEqual(self.botoes_enviados(), [])
        self.assertEqual(self.quadro.listar(), [])
        # o dono responde o repo: o pedido anterior volta no prompt e a sessao editavel comeca
        self.codex.respostas = [edita()]
        self.bot.processar(self.msg("site"))
        c = self.codex.chamadas[1]
        self.assertEqual(c["sandbox"], "workspace-write")
        self.assertIn("muda a cor do botao", c["prompt"])
        self.assertEqual(self.quadro.listar()[0]["repo"], "site")

    def test_pedido_de_publicar_nao_executa(self):
        self.codex.respostas = [so_responde]
        self.bot.processar(self.msg("faz merge e push disso em producao no web-app"))
        self.assertIn("do NOT publish", self.codex.chamadas[0]["prompt"])
        self.assertFalse([c for c in self.root.chamadas if c[0] in ("push_base", "publicar")])
        sh("git", "--git-dir", str(self.gh), "log", "-1", "--format=%s")
        self.assertEqual(sh("git", "--git-dir", str(self.gh), "log", "--format=%s").split(), ["inicial"])

    def test_aprovacao_curta_mostra_botoes_do_chat_em_vez_de_publicar(self):
        self.codex.respostas = [edita()]
        self.bot.processar(self.msg("ajusta no web-app"))
        n = len(self.codex.chamadas)
        self.bot.processar(self.msg("pode publicar"))
        self.assertEqual(len(self.codex.chamadas), n)
        self.assertFalse([c for c in self.root.chamadas if c[0] == "push_base"])
        self.assertIn("Publish", [r for r, _ in self.botoes_enviados()])

    def test_botao_publicar_faz_push_base_e_encerra_sessao(self):
        self.codex.respostas = [edita(), edita("outra.py")]
        self.bot.processar(self.msg("ajusta no web-app"))
        self.bot._bg = lambda fn: fn()
        self.apertar("Publish")
        self.assertTrue([c for c in self.root.chamadas if c[0] == "push_base"])
        self.assertIn("mudanca.py", sh("git", "--git-dir", str(self.gh), "ls-tree", "--name-only", "main"))
        self.assertIsNone(self.estado.get("chat_sessao"))
        self.bot.processar(self.msg("outra no web-app"))
        self.assertEqual(len(self.quadro.listar()), 2)

    def test_descartar_encerra_sem_publicar(self):
        self.codex.respostas = [edita()]
        self.bot.processar(self.msg("ajusta no web-app"))
        self.apertar("Discard")
        self.assertEqual(self.quadro.listar()[0]["estado"], "cancelada")
        self.assertIsNone(self.estado.get("chat_sessao"))
        self.assertFalse([c for c in self.root.chamadas if c[0] == "push_base"])

    def test_segredo_no_diff_bloqueia_o_commit(self):
        chave = "sk-" + "a1B2c3D4e5F6g7H8i9J0"
        self.codex.respostas = [edita("config.py", f"API = '{chave}'\n", "Feito.")]
        self.bot.processar(self.msg("poe a chave no web-app"))
        t = self.quadro.listar()[0]
        ws = Path(t["workspace"])
        self.assertEqual(sh("git", "log", "--format=%s", "origin/main..HEAD", cwd=ws).strip(), "")
        self.assertEqual(sh("git", "diff", "--cached", "--name-only", cwd=ws).strip(), "")
        self.assertEqual(self.botoes_enviados(), [])
        self.assertIn("BLOCKED", self.tr.textos()[-1])
        self.assertNotIn(chave, " ".join(self.tr.textos()))
        self.assertNotIn("push_branch", [c[0] for c in self.root.chamadas])

    def test_arquivo_env_bloqueia_o_commit(self):
        self.codex.respostas = [edita(".env", "X=1\n")]
        self.bot.processar(self.msg("cria env no web-app"))
        self.assertIn("BLOCKED", self.tr.textos()[-1])
        self.assertEqual(self.botoes_enviados(), [])

    def test_notas_na_raiz_ficam_fora_do_commit(self):
        self.codex.respostas = [edita("CONTEXTO.md", "notas\n")]
        self.bot.processar(self.msg("anota no web-app"))
        self.assertEqual(self.botoes_enviados(), [])
        self.assertNotIn("push_branch", [c[0] for c in self.root.chamadas])
