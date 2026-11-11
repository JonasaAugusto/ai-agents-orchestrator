"""Per-task Codex event log (masked) and the read-only `aao watch` view."""
import json

from ai_agents_orchestrator import watch
from ai_agents_orchestrator.engines import Resultado
from tests.helpers import Base

FAKE_SK = "sk-" + "Zz9Yy8Xx7Ww6Vv5Uu4"
EVENTOS = [
    {"type": "thread.started", "thread_id": "T"},
    {"type": "item.started", "item": {"type": "command_execution", "command": "pytest -q"}},
    {"type": "item.completed", "item": {"type": "command_execution", "command": "pytest -q", "exit_code": 0,
                                        "aggregated_output": f"3 passed OPENAI_API_KEY={FAKE_SK}"}},
    {"type": "item.completed", "item": {"type": "file_change", "changes": [{"path": "app.py", "kind": "update"}]}},
    {"type": "item.completed", "item": {"type": "agent_message", "text": "Status: completed"}},
    {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 5}},
]


class Watch(Base):
    def rodar_tarefa(self):
        self.criar_repo()
        t = self.quadro.criar("Tarefa", "", "web-app", origem="dono")
        self.codex.eventos = EVENTOS
        self.codex.respostas = [Resultado(ok=True, texto="Status: completed\nDone.", segundos=1)]
        self.svc.ciclo_worker()
        return t["id"]

    def test_eventos_sao_gravados_mascarados_por_tarefa(self):
        tid = self.rodar_tarefa()
        log = self.cfg.resultados / str(tid) / "events.jsonl"
        linhas = log.read_text().splitlines()
        self.assertEqual(len(linhas), len(EVENTOS))
        self.assertNotIn(FAKE_SK, log.read_text())
        self.assertEqual(json.loads(linhas[0])["ev"]["type"], "thread.started")

    def test_watch_reproduz_e_termina_quando_a_tarefa_acaba(self):
        tid = self.rodar_tarefa()
        saida = []
        rc = watch.main(self.cfg, tid, seguir=True, saida=saida.append, dormir=lambda s: None, max_ciclos=5)
        self.assertEqual(rc, 0)
        texto = "\n".join(saida)
        for trecho in (f"task #{tid}", "branch: agent/", "$ pytest -q", "-> exit 0", "edit: update app.py",
                       "codex: Status: completed", "turn completed", "is in review", "diff so far"):
            self.assertIn(trecho, texto)
        self.assertNotIn(FAKE_SK, texto)

    def test_watch_segue_o_arquivo_ao_vivo_sem_interferir(self):
        t = self.quadro.criar("Ao vivo", "", "web-app", origem="dono")
        self.quadro.atualizar(t["id"], estado="em_andamento")
        gravar = self.svc.gravador_eventos(t["id"])
        saida = []
        passos = iter([lambda: gravar(EVENTOS[1]), lambda: gravar(EVENTOS[4]),
                       lambda: self.quadro.atualizar(t["id"], estado="em_revisao")])

        def dormir(_s):
            next(passos, lambda: None)()
        watch.main(self.cfg, t["id"], seguir=True, saida=saida.append, dormir=dormir, max_ciclos=10)
        texto = "\n".join(saida)
        self.assertIn("$ pytest -q", texto)
        self.assertIn("codex: Status: completed", texto)
        self.assertEqual(self.quadro.ver(t["id"])["estado"], "em_revisao")

    def test_sem_tarefa_padrao_usa_a_em_execucao_ou_a_ultima_com_log(self):
        self.assertIsNone(watch.tarefa_padrao(self.cfg))
        tid = self.rodar_tarefa()
        self.assertEqual(watch.tarefa_padrao(self.cfg), tid)
        self.assertEqual(watch.tarefa_padrao(self.cfg, chamar=lambda c, r, timeout=3: {"ok": True, "tarefa_atual": 42}), 42)
        self.assertEqual(watch.main(self.cfg.__class__(state_dir=self.dir / "vazio"), None, saida=lambda *_: None), 1)

    def test_formatacao_ignora_ruido(self):
        self.assertIsNone(watch.formatar_evento({"ev": {"type": "item.updated", "item": {"type": "reasoning"}}}))
        self.assertIn("ERROR", watch.formatar_evento({"ev": {"type": "error", "message": "boom"}}))


class AbrirAoLado(Base):
    def test_tmux_terminal_ou_manual(self):
        chamadas = []
        como, linha = watch.abrir(7, env={"TMUX": "x"}, which=lambda n: "/x" if n == "tmux" else None, rodar=chamadas.append)
        self.assertEqual((como, linha), ("tmux", "aao watch 7"))
        self.assertEqual(chamadas[-1][:2], ["tmux", "split-window"])
        como, _ = watch.abrir(None, env={"DISPLAY": ":0"}, which=lambda n: "/x" if n == "xterm" else None, rodar=chamadas.append)
        self.assertEqual(como, "xterm")
        self.assertEqual(chamadas[-1], ["xterm", "-e", "aao", "watch"])
        n = len(chamadas)
        self.assertEqual(watch.abrir(3, env={}, which=lambda n: "/x", rodar=chamadas.append), ("manual", "aao watch 3"))
        self.assertEqual(len(chamadas), n)  # nothing launched
