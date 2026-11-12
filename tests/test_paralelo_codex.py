"""service.max_concurrent_codex: parallel Codex tasks only when repositories/files do not touch."""
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_agents_orchestrator import config, servico, workspace
from ai_agents_orchestrator.engines import Resultado
from tests.helpers import Base, sh

OK = "Feito: novo.py. Testes: ok. Falta: nada. Para o Claude: nada."


class Sonda:
    """Fake Codex answer: counts how many runs are inside at once and waits at a barrier (deterministic overlap)."""

    def __init__(self, n_barreira=0, espera=None):
        self.lock = threading.Lock()
        self.dentro = self.maximo = 0
        self.barreira = threading.Barrier(n_barreira, timeout=10) if n_barreira else None
        self.espera = espera
        self.entrou = threading.Event()
        self.cwds = []

    def __call__(self, prompt, cwd):
        with self.lock:
            self.dentro += 1
            self.maximo = max(self.maximo, self.dentro)
            self.cwds.append(str(cwd))
        self.entrou.set()
        try:
            if self.barreira:
                self.barreira.wait()
            if self.espera:
                self.assertEsperou = self.espera.wait(10)
            (Path(cwd) / "novo.py").write_text("print('feito')\n")
        finally:
            with self.lock:
                self.dentro -= 1
        return Resultado(ok=True, texto=OK, thread_id="T", segundos=1)


class Paralelo(Base):
    def setUp(self):
        super().setUp()
        self.criar_repo()
        self.cfg.max_codex_simultaneos = 2

    def rodar_juntos(self, *fns):
        res, ths = [], [threading.Thread(target=lambda f=f: res.append(f())) for f in fns]
        for t in ths:
            t.start()
        for t in ths:
            t.join(30)
            self.assertFalse(t.is_alive())
        return res

    def criar(self, titulo, arquivos, repo="web-app"):
        return self.quadro.criar(titulo, "", repo, arquivos=arquivos)["id"]

    def test_default_is_one_and_extra_slot_is_off(self):
        self.assertEqual(config.Config().max_codex_simultaneos, 1)
        self.cfg.max_codex_simultaneos = 1
        self.criar("A", ["a/"])
        self.assertFalse(self.svc.ciclo_extra())
        self.assertEqual(self.quadro.listar(("pendente",), "codex")[0]["titulo"], "A")

    def test_disjoint_files_run_at_the_same_time_each_in_its_own_workspace(self):
        sonda = Sonda(n_barreira=2)
        self.codex.respostas = [sonda, sonda]
        a, b = self.criar("A", ["a/"]), self.criar("B", ["b/"])
        r = self.rodar_juntos(self.svc.ciclo_worker, self.svc.ciclo_extra)
        self.assertEqual(r, [True, True])
        self.assertEqual(sonda.maximo, 2)
        self.assertEqual(len(set(sonda.cwds)), 2)
        ta, tb = self.quadro.ver(a), self.quadro.ver(b)
        self.assertEqual((ta["estado"], tb["estado"]), ("em_revisao", "em_revisao"))
        self.assertNotEqual(ta["workspace"], tb["workspace"])
        self.assertEqual(self.svc._paralelas, {})
        self.assertIsNone(self.svc.tarefa_atual)

    def test_overlapping_files_wait_for_the_running_task(self):
        solta = threading.Event()
        sonda = Sonda(espera=solta)
        self.codex.respostas = [sonda, sonda]
        self.criar("A", ["src/"])
        b = self.criar("B", ["src/x.py"])
        t = threading.Thread(target=self.svc.ciclo_worker)
        t.start()
        self.assertTrue(sonda.entrou.wait(10))
        self.assertFalse(self.svc.ciclo_extra())  # overlaps with the running A
        self.assertEqual(self.quadro.ver(b)["estado"], "pendente")
        solta.set()
        t.join(30)
        self.assertTrue(self.svc.ciclo_extra())
        self.assertEqual(sonda.maximo, 1)
        self.assertEqual(self.quadro.ver(b)["estado"], "em_revisao")

    def test_task_without_declared_files_never_runs_beside_another_in_the_repo(self):
        solta = threading.Event()
        sonda = Sonda(espera=solta)
        self.codex.respostas = [sonda, sonda]
        self.criar("A", ["a/"])
        b = self.criar("B", [])
        t = threading.Thread(target=self.svc.ciclo_worker)
        t.start()
        self.assertTrue(sonda.entrou.wait(10))
        self.assertFalse(self.svc.ciclo_extra())
        self.assertEqual(self.quadro.ver(b)["estado"], "pendente")
        solta.set()
        t.join(30)

    def test_different_repositories_run_in_parallel_even_without_files(self):
        self.criar_repo("docs-site")
        sonda = Sonda(n_barreira=2)
        self.codex.respostas = [sonda, sonda]
        self.criar("A", [])
        self.criar("B", [], repo="docs-site")
        r = self.rodar_juntos(self.svc.ciclo_worker, self.svc.ciclo_extra)
        self.assertEqual(r, [True, True])
        self.assertEqual(sonda.maximo, 2)

    def test_repo_mentioned_in_the_text_counts_as_touched(self):
        self.criar_repo("docs-site")
        solta = threading.Event()
        sonda = Sonda(espera=solta)
        self.codex.respostas = [sonda, sonda]
        self.criar("A", ["a/"], repo="docs-site")
        b = self.quadro.criar("B update docs-site too", "", "web-app", arquivos=["b/"])["id"]
        t = threading.Thread(target=self.svc.ciclo_worker)
        t.start()
        self.assertTrue(sonda.entrou.wait(10))
        self.assertFalse(self.svc.ciclo_extra())
        self.assertEqual(self.quadro.ver(b)["estado"], "pendente")
        solta.set()
        t.join(30)

    def test_never_exceeds_the_limit(self):
        solta = threading.Event()
        sonda = Sonda(espera=solta)
        self.codex.respostas = [sonda, sonda, sonda]
        for i, d in enumerate("abc"):
            self.criar(f"T{i}", [f"{d}/"])
        ths = [threading.Thread(target=self.svc.ciclo_worker), threading.Thread(target=self.svc.ciclo_extra)]
        for t in ths:
            t.start()
        for _ in range(400):
            if sonda.dentro == 2:
                break
            time.sleep(0.025)
        self.assertEqual(sonda.dentro, 2)
        self.assertFalse(self.svc.ciclo_extra())  # third slot refused
        solta.set()
        for t in ths:
            t.join(30)
        self.assertEqual(sonda.maximo, 2)

    def test_cancel_reaches_the_runner_of_a_parallel_task(self):
        class Runner:
            cancelado = False

            def cancelar(self):
                self.cancelado = True
        runner = Runner()
        tid = self.criar("A", ["a/"])
        self.quadro.atualizar(tid, estado="em_andamento")
        self.svc._paralelas[tid] = runner
        self.assertTrue(self.svc.cancelar_tarefa(tid)["ok"])
        self.assertTrue(runner.cancelado)
        self.svc._paralelas.clear()

    def test_merges_into_the_base_are_serial(self):
        ids = []
        for n in ("a", "b"):
            tid = self.criar(n, [f"{n}/"])
            ws, branch, _ = workspace.preparar_workspace(self.cfg, "web-app", tid, n)
            (ws / f"{n}.py").write_text(n)
            sh("git", "add", "-A", cwd=ws)
            sh("git", "commit", "-qm", n, cwd=ws)
            self.quadro.atualizar(tid, estado="em_revisao", workspace=str(ws), branch=branch)
            ids.append(tid)
        estado = {"dentro": 0, "max": 0}
        lock = threading.Lock()
        real = workspace.merge_e_push

        def lento(*a, **k):
            with lock:
                estado["dentro"] += 1
                estado["max"] = max(estado["max"], estado["dentro"])
            threading.Event().wait(0.2)
            try:
                return real(*a, **k)
            finally:
                with lock:
                    estado["dentro"] -= 1
        with patch.object(workspace, "merge_e_push", lento):
            r = self.rodar_juntos(*[lambda i=i: servico.publicar_base(self.cfg, self.quadro, self.root, i, 1.0)
                                    for i in ids])
        self.assertEqual(estado["max"], 1)
        self.assertEqual(sum(1 for x in r if x["ok"]), 2, r)


if __name__ == "__main__":
    unittest.main()
