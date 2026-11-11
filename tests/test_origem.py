import io
import os
import sqlite3
import unittest
from contextlib import redirect_stdout
from unittest import mock

from ai_agents_orchestrator import cli
from ai_agents_orchestrator.db import Quadro
from tests.helpers import Base, sh
from tests.test_servico import escreve_arquivo


class SemTelegramParaClaude(Base):
    def setUp(self):
        super().setUp()
        self.gh = self.criar_repo()
        self.codex.respostas = [escreve_arquivo]

    def rodar(self, origem):
        t = self.quadro.criar("Muda coisa", "", "web-app", "codex", origem)
        self.svc.ciclo_worker()
        return self.quadro.ver(t["id"])

    def test_origem_claude_nao_manda_nada_nem_botoes(self):
        t = self.rodar("claude")
        self.assertEqual(t["estado"], "em_revisao")
        self.assertEqual(self.tr.enviados(), [])
        self.assertEqual(self.botoes_enviados(), [])

    def test_origem_claude_bloqueada_e_erro_ficam_mudos(self):
        self.codex.respostas = [lambda p, c: __import__("ai_agents_orchestrator.engines", fromlist=["x"]).Resultado(ok=False, erro="falhou", segundos=1)]
        t = self.rodar("claude")
        self.assertEqual(t["estado"], "bloqueada")
        self.assertEqual(self.tr.enviados(), [])

    def test_origem_claude_revisao_muda(self):
        self.codex.respostas = [lambda p, c: __import__("ai_agents_orchestrator.engines", fromlist=["x"]).Resultado(ok=True, texto="ok", segundos=1)]
        sh("git", "--git-dir", str(self.cfg.repos_dir / "web-app.git"), "branch", "agent/x", "main")
        t = self.quadro.criar("Revisar", "", "web-app", "codex", "claude", kind="revisao", alvo="agent/x")
        self.svc.ciclo_worker()
        self.assertEqual(self.quadro.ver(t["id"])["estado"], "concluida")
        self.assertEqual(self.tr.enviados(), [])

    def test_wakeup_desligado_por_padrao_nunca_toca_o_socket(self):
        self.cfg.claude_wakeup = False  # the default (experimental feature is opt-in)
        self.rodar("claude")
        self.assertEqual(self._acordadas(), [])

    def _acordadas(self):
        return [c[1] for c in self.root.chamadas if c[0] == "acordar_claude"]

    def test_origem_claude_acorda_a_sessao_do_claude_ao_terminar(self):
        t = self.rodar("claude")
        acordadas = self._acordadas()
        self.assertEqual(len(acordadas), 1)
        self.assertIn(f"#{t['id']}", acordadas[0])
        self.assertEqual(acordadas[0], f"Task #{t['id']} finished with status in review. Run `aao result {t['id']}`.")

    def test_origem_claude_bloqueada_tambem_acorda(self):
        self.codex.respostas = [lambda p, c: __import__("ai_agents_orchestrator.engines", fromlist=["x"]).Resultado(ok=False, erro="falhou", segundos=1)]
        self.rodar("claude")
        self.assertEqual(len(self._acordadas()), 1)

    def test_origem_claude_revisao_acorda(self):
        self.codex.respostas = [lambda p, c: __import__("ai_agents_orchestrator.engines", fromlist=["x"]).Resultado(ok=True, texto="ok", segundos=1)]
        sh("git", "--git-dir", str(self.cfg.repos_dir / "web-app.git"), "branch", "agent/x", "main")
        self.quadro.criar("Revisar", "", "web-app", "codex", "claude", kind="revisao", alvo="agent/x")
        self.svc.ciclo_worker()
        self.assertEqual(len(self._acordadas()), 1)

    def test_falha_ao_acordar_nao_derruba_o_worker(self):
        self.root.acordar_ok = False
        t = self.rodar("claude")
        self.assertEqual(t["estado"], "em_revisao")

    def test_origem_dono_nao_acorda_o_claude(self):
        self.rodar("dono")
        self.assertEqual(self._acordadas(), [])

    def test_origem_dono_mantem_avisos_e_botoes(self):
        self.rodar("dono")
        self.assertTrue(any("Starting" in x for x in self.tr.textos()))
        self.assertIn("Publish", [r for r, _ in self.botoes_enviados()])

    def test_origem_handoff_mantem_avisos_e_botoes(self):
        self.rodar("handoff")
        self.assertTrue(any("completed" in x for x in self.tr.textos()))
        self.assertIn("Publish", [r for r, _ in self.botoes_enviados()])

    def test_comando_delegar_marca_origem_claude(self):
        r = self.svc.comando({"cmd": "delegar", "repo": "web-app", "titulo": "x"})
        self.assertEqual(self.quadro.ver(r["id"])["origem"], "claude")

    def test_quadro_continua_mostrando_tarefas_do_claude(self):
        t = self.quadro.criar("do claude", "", "", "codex", "claude")
        self.bot.processar(self.msg("/quadro"))
        self.assertIn(f"#{t['id']}", self.tr.textos()[-1])

    def test_migracao_banco_antigo_sem_coluna_origem_nao_quebra(self):
        c = sqlite3.connect(str(self.dir / "velho.db"))
        c.execute("CREATE TABLE tarefas (id INTEGER PRIMARY KEY AUTOINCREMENT, titulo TEXT NOT NULL, descricao TEXT NOT NULL DEFAULT '', "
                  "repo TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL DEFAULT 'dev', dono TEXT NOT NULL DEFAULT 'codex', "
                  "estado TEXT NOT NULL DEFAULT 'pendente', branch TEXT, patch TEXT, resumo TEXT, testes TEXT, erro TEXT, "
                  "arquivos TEXT NOT NULL DEFAULT '[]', alterados TEXT NOT NULL DEFAULT '[]', depende TEXT NOT NULL DEFAULT '[]', "
                  "alvo TEXT, thread_id TEXT, instrucoes TEXT, visto_claude INTEGER NOT NULL DEFAULT 1, aviso TEXT, workspace TEXT, "
                  "publicado TEXT, criado REAL NOT NULL, atualizado REAL NOT NULL, inicio REAL, fim REAL)")
        c.commit(); c.close()
        c = sqlite3.connect(str(self.dir / "velho.db"))
        c.execute("INSERT INTO tarefas(titulo,criado,atualizado) VALUES('antiga',1,1)"); c.commit(); c.close()
        q = Quadro(self.dir / "velho.db")
        self.addCleanup(q.c.close)
        self.assertEqual(q.ver(1)["origem"], "claude")


class PublicarPelaCli(Base):
    def setUp(self):
        super().setUp()
        self.gh = self.criar_repo()
        self.codex.respostas = [escreve_arquivo]
        self.t_ = self.quadro.criar("Muda coisa", "", "web-app", "codex", "claude")
        self.svc.ciclo_worker()
        self.tid = self.t_["id"]

    def pub(self, tid=None, interativo=True, euid=0):
        """The owner types the task id at an interactive terminal (the only way the CLI publishes)."""
        out = io.StringIO()
        tid = tid or self.tid
        with mock.patch("os.geteuid", return_value=euid), redirect_stdout(out):
            rc = cli._publicar(self.cfg, tid, root=self.root, interativo=interativo, entrada=lambda _p: str(tid))
        return rc, out.getvalue()

    def commits(self):
        return sh("git", "--git-dir", str(self.gh), "log", "--oneline", "main").count("\n")

    def test_sem_terminal_interativo_nao_publica(self):
        rc, _ = self.pub(interativo=False)
        self.assertEqual(rc, 2)
        self.assertEqual(self.commits(), 1)

    def test_sem_root_nao_publica(self):
        rc, _ = self.pub(euid=1000)
        self.assertEqual(rc, 2)
        self.assertEqual(self.commits(), 1)

    def test_origem_telegram_nao_publica_pela_cli(self):
        t = self.quadro.criar("tg", "", "web-app", "codex", "dono")
        self.quadro.atualizar(t["id"], estado="em_revisao", branch="agent/9-x", workspace="/x")
        rc, _ = self.pub(t["id"])
        self.assertEqual(rc, 2)
        self.assertEqual(self.root.chamadas and [c for c in self.root.chamadas if c[0] == "push_base"], [])

    def test_publica_main_no_ff_pelo_daemon_e_marca_concluida(self):
        rc, _ = self.pub()
        self.assertEqual(rc, 0)
        self.assertEqual(self.quadro.ver(self.tid)["estado"], "concluida")
        self.assertIn("Merge agent/", sh("git", "--git-dir", str(self.gh), "log", "--oneline", "main"))
        self.assertEqual(sh("git", "--git-dir", str(self.gh), "tag"), "")
        self.assertEqual(self.tr.enviados(), [])
        rc, _ = self.pub()  # de novo: ja concluida, recusa
        self.assertEqual(rc, 2)

    def test_tarefa_inexistente(self):
        rc, _ = self.pub(999)
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
