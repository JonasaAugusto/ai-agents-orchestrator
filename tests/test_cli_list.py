"""CLI state aliases and labels, exercised against a disposable task board."""
import io
import json
from contextlib import redirect_stdout
from unittest import mock

from ai_agents_orchestrator import cli
from tests.helpers import Base


class ListStates(Base):
    def setUp(self):
        super().setUp()
        self.tasks = {}
        for state in ("pendente", "em_andamento", "em_revisao", "concluida", "bloqueada", "cancelada"):
            task = self.quadro.criar(state, "", "web-app")
            self.quadro.atualizar(task["id"], estado=state)
            self.tasks[state] = task["id"]

    def run_cli(self, *args):
        output = io.StringIO()
        with mock.patch.object(cli, "_cfg", return_value=self.cfg), mock.patch.object(
                cli, "chamar", side_effect=lambda cfg, req: self.svc.comando(req)), redirect_stdout(output):
            rc = cli.main(list(args))
        self.assertEqual(rc, 0, output.getvalue())
        return json.loads(output.getvalue())["tarefas"]

    def test_english_filter_selects_task_and_portuguese_alias_still_works(self):
        for internal, argument, label in (
                ("pendente", "pending", "pending"),
                ("em_andamento", "running", "in progress"),
                ("em_revisao", "review", "in review"),
                ("concluida", "completed", "completed"),
                ("bloqueada", "blocked", "blocked"),
                ("cancelada", "cancelled", "cancelled")):
            with self.subTest(state=argument):
                tasks = self.run_cli("list", "--state", argument)
                self.assertEqual([t["id"] for t in tasks], [self.tasks[internal]])
                self.assertEqual(tasks[0]["estado"], label)
                self.assertEqual(tasks, self.run_cli("listar", "--estado", internal))
                self.assertEqual(self.quadro.ver(tasks[0]["id"])["estado"], internal)

    def test_unfiltered_list_translates_every_state_without_changing_board(self):
        tasks = self.run_cli("list")
        self.assertEqual({t["estado"] for t in tasks},
                         {"pending", "in progress", "in review", "completed", "blocked", "cancelled"})
        self.assertEqual({t["id"]: t["titulo"] for t in tasks},
                         {tid: state for state, tid in self.tasks.items()})
        self.assertEqual({t["id"]: t["estado"] for t in self.quadro.listar()},
                         {tid: state for state, tid in self.tasks.items()})
