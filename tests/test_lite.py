"""Disposable lite installs: no providers, root services or production resources."""
import io
import json
import os
import tempfile
import socket
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from unittest import mock

from ai_agents_orchestrator import assistente, cli, compat, config, doctor, engines, workspace
from tests.helpers import Base, sh
from tests.test_doctor_setup import AmbienteFalso


class LiteConfig(unittest.TestCase):
    def setUp(self):
        self.addCleanup(config.definir_fuso, config.nome_fuso())

    def test_user_paths_and_environment_overrides(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text('mode = "lite"\n[paths]\nstate_dir = ' + json.dumps(tmp + '/state') + '\n')
            cfg = config.carregar({"AAO_CONFIG": str(path), "XDG_STATE_HOME": tmp})
            self.assertEqual(cfg.mode, "lite")
            for p in (cfg.socket, cfg.root_socket, cfg.rules_file, cfg.templates_dir, cfg.root_state):
                self.assertFalse(str(p).startswith(("/etc/", "/run/", "/var/lib/")))
            self.assertEqual(cfg.codex_home, Path.home() / ".codex")
            self.assertEqual(cfg.fonte_memoria, cfg.fonte_transcripts / "memory")
            other = config.carregar({"AAO_CONFIG": str(path), "AAO_PATHS_SOCKET": str(Path(tmp) / "sock")})
            self.assertEqual(other.socket, Path(tmp) / "sock")

    def test_invalid_mode_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "c.toml"
            p.write_text('mode = "ltei"\n')
            with self.assertRaises(ValueError):
                config.carregar({}, p)

    def test_doctor_no_root_daemon_or_service_user(self):
        cfg = config.Config(mode="lite")
        amb = AmbienteFalso(usuario=False)
        items = doctor.checar_base(cfg, amb) + doctor.checar_runtime(cfg, amb)
        names = {i.nome for i in items}
        self.assertNotIn("root daemon socket", names)
        self.assertNotIn("service user", names)
        text = " ".join(i.detalhe for i in items)
        self.assertIn("lite", text)
        self.assertIn("isolation", text)
        self.assertIn("button", text)
        doctor.checar_codex(cfg, amb)
        self.assertEqual(amb.comandos[-1], ["codex", "login", "status"])


class LiteSetup(unittest.TestCase):
    def setUp(self):
        self.addCleanup(config.definir_fuso, config.nome_fuso())

    def test_setup_respects_config_selected_after_module_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            selected = root / "selected.toml"
            old = root / "unselected.toml"
            answers = iter(["lite", str(root / "state"), "UTC", "", "codex", "restricted", ""])
            with patch.dict(os.environ, {"AAO_CONFIG": str(selected)}), patch.object(config, "CONFIG_FILE", old):
                wizard = assistente.Assistente(entrada=lambda _: next(answers), saida=lambda _: None,
                                               amb=AmbienteFalso())
                self.assertEqual(wizard.executar(), 0)
            self.assertTrue(selected.is_file())
            self.assertFalse(old.exists())

    def test_setup_installs_plugin_checks_login_and_preserves_second_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "config.toml"
            answers = iter(["lite", str(Path(tmp) / "state"), "UTC", "", "codex", "restricted", ""])
            amb = AmbienteFalso()
            out = []
            wizard = assistente.Assistente(config_file=p, env_file=Path(tmp) / "env",
                entrada=lambda _: next(answers), saida=out.append, amb=amb)
            self.assertEqual(wizard.executar(), 0)
            cfg = config.carregar({}, p)
            self.assertEqual((cfg.mode, cfg.orchestrator, cfg.channel), ("lite", "codex", "none"))
            self.assertTrue(any("auth" in c and "status" in c for c in amb.comandos))
            self.assertTrue(any("plugin" in c and "install" in c for c in amb.comandos))
            before = p.read_bytes()
            wizard.entrada = lambda _: self.fail("idempotent setup must not ask or overwrite")
            self.assertEqual(wizard.executar(), 0)
            self.assertEqual(p.read_bytes(), before)
            self.assertFalse((Path(tmp) / "publish.key").exists())
            self.assertTrue(cfg.rules_file.exists())

    def test_claude_availability_uses_official_status_in_user_environment(self):
        env = {"HOME": "/tmp/fake-user", "PATH": "/tmp/bin:/usr/bin"}
        runner = engines.ClaudeRunner(Path("/tmp/fake-state"), child_env=env)
        with patch("subprocess.run", return_value=mock.Mock(returncode=0)) as run:
            self.assertTrue(runner.disponivel())
            self.assertEqual(run.call_args.args[0], ["claude", "auth", "status"])
            self.assertEqual(run.call_args.kwargs["env"], env)
        with patch("subprocess.run", side_effect=FileNotFoundError):
            self.assertFalse(runner.disponivel())


class LiteWorkflow(Base):
    native_lite = True
    def setUp(self):
        super().setUp()
        self.cfg.mode = "lite"
        self.cfg.fonte_transcripts = self.dir / "transcripts"
        self.cfg.fonte_memoria = self.dir / "memory"
        self.cfg.rules_file = self.dir / "BASE_RULES.md"
        self.cfg.division_file = self.dir / "DIVISION.md"
        self.cfg.templates_dir = self.dir / "templates"
        self.enterContext(patch.dict(os.environ, {"HOME": str(self.dir), "USERPROFILE": str(self.dir),
                                                "CODEX_HOME": str(self.dir / ".codex")}))
        from ai_agents_orchestrator.lite import LocalBackend
        self.backend = LocalBackend(self.cfg)

    def ready(self):
        self.criar_repo()
        tid = self.quadro.criar("lite", "", "web-app", origem="claude")["id"]
        ws, branch, _ = workspace.preparar_workspace(self.cfg, "web-app", tid, "lite")
        (ws / "app.py").write_text("print('lite')\n")
        sh("git", "add", "app.py", cwd=ws)
        sh("git", "commit", "-qm", "lite", cwd=ws)
        sha = sh("git", "rev-parse", "HEAD", cwd=ws).strip()
        self.quadro.atualizar(tid, estado="em_revisao", workspace=str(ws), branch=branch)
        return tid, ws, branch, sha

    def test_publish_interactive_bound_to_sha_as_normal_user(self):
        tid, ws, branch, sha = self.ready()
        with patch.object(compat, "euid", return_value=1000), patch("sys.stdout", new_callable=io.StringIO):
            rc = cli._publicar(self.cfg, tid, root=self.backend, interativo=True, entrada=lambda _: f"{tid} {sha}")
        self.assertEqual(rc, 0)
        self.assertEqual(self.quadro.ver(tid)["estado"], "concluida")
        remote = self.cfg.repos["web-app"]["github_url"]
        self.assertEqual(sh("git", "--git-dir", remote, "show", "main:app.py"), "print('lite')\n")

    def test_publish_rejects_changed_sha(self):
        tid, ws, branch, sha = self.ready()
        def confirm(_):
            (ws / "app.py").write_text("print('changed')\n")
            sh("git", "add", "app.py", cwd=ws)
            sh("git", "commit", "-qm", "changed", cwd=ws)
            return f"{tid} {sha}"
        with patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(cli._publicar(self.cfg, tid, root=self.backend, interativo=True, entrada=confirm), 1)
        self.assertEqual(self.quadro.ver(tid)["estado"], "em_revisao")

    def test_publish_merges_current_remote_base(self):
        tid, ws, branch, sha = self.ready()
        src = self.dir / "src"
        (src / "other.py").write_text("value = 1\n")
        sh("git", "add", "other.py", cwd=src)
        sh("git", "commit", "-qm", "remote update", cwd=src)
        remote = self.cfg.repos["web-app"]["github_url"]
        sh("git", "push", "-q", remote, "main", cwd=src)
        with patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(cli._publicar(self.cfg, tid, root=self.backend, interativo=True,
                                           entrada=lambda _: f"{tid} {sha}"), 0)
        self.assertEqual(sh("git", "--git-dir", remote, "show", "main:other.py"), "value = 1\n")
        self.assertEqual(sh("git", "--git-dir", remote, "show", "main:app.py"), "print('lite')\n")

    def test_publish_conflict_keeps_remote_unchanged(self):
        tid, ws, branch, sha = self.ready()
        src = self.dir / "src"
        (src / "app.py").write_text("print('remote edit')\n")
        sh("git", "add", "app.py", cwd=src)
        sh("git", "commit", "-qm", "remote conflict", cwd=src)
        remote = self.cfg.repos["web-app"]["github_url"]
        sh("git", "push", "-q", remote, "main", cwd=src)
        before = sh("git", "--git-dir", remote, "rev-parse", "main")
        with patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(cli._publicar(self.cfg, tid, root=self.backend, interativo=True,
                                           entrada=lambda _: f"{tid} {sha}"), 1)
        self.assertEqual(sh("git", "--git-dir", remote, "rev-parse", "main"), before)
        self.assertEqual(self.quadro.ver(tid)["estado"], "em_revisao")

    def test_publish_rejects_pipe_and_id_only(self):
        tid, ws, branch, sha = self.ready()
        with patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(cli._publicar(self.cfg, tid, root=self.backend, interativo=False), 2)
            self.assertEqual(cli._publicar(self.cfg, tid, root=self.backend, interativo=True, entrada=lambda _: str(tid)), 2)

    def test_same_process_backend_and_real_user_engine_environment(self):
        with patch("ai_agents_orchestrator.root_daemon.RootClient", side_effect=AssertionError("root")):
            svc = cli.montar_servico(self.cfg, False, True)
        self.addCleanup(svc.estado.c.close)
        self.addCleanup(svc.quadro.c.close)
        self.assertIsInstance(svc.root, type(self.backend))
        self.assertEqual(svc.codex.child_env["HOME"], str(Path.home()))
        self.assertEqual(svc.codex.child_env["CODEX_HOME"], str(Path.home() / ".codex"))
        self.assertEqual(svc.codex.child_env["PATH"], os.environ["PATH"])
        self.assertNotIn("AAO_PUBLISH_KEY", svc.codex.child_env)

    def test_local_mirror_and_masked_transcript_without_privileged_files(self):
        self.criar_repo()
        source = self.dir / "src"
        self.cfg.repos["local"] = {"fonte": str(source), "branch_base": "main"}
        self.cfg.fonte_transcripts = self.dir / "transcripts"
        self.cfg.fonte_transcripts.mkdir()
        (self.cfg.fonte_transcripts / "s.jsonl").write_text(json.dumps({
            "type": "user", "timestamp": "2026-11-12T12:00:00Z", "message": {"content": "hello"}}) + "\n")
        with patch("ai_agents_orchestrator.arquivos_root.abrir_dir", side_effect=AssertionError("privileged path")):
            r = self.backend.espelhar_repo("local")
            self.assertTrue(r["ok"], r)
            r = self.backend.espelhar_uso("s")
            self.assertTrue(r["ok"], r)
            other = self.cfg.fonte_transcripts / "other.jsonl"
            other.write_text(json.dumps({"type": "user", "message": {"content": "wrong session"}}) + "\n")
            os.utime(self.cfg.fonte_transcripts / "s.jsonl", ns=(1000, 1000))
            os.utime(other, ns=(2000, 2000))
            self.backend.espelhar({"session_id": "s"})
        self.assertIn("hello", (self.cfg.mirror / "transcript-cauda.jsonl").read_text())
        self.assertNotIn("wrong session", (self.cfg.mirror / "transcript-cauda.jsonl").read_text())
        self.assertEqual(sh("git", "--git-dir", str(self.cfg.repos_dir / "local.git"), "rev-parse", "main").strip(),
                         sh("git", "rev-parse", "HEAD", cwd=source).strip())

    def test_delegate_review_briefing_and_hook_with_local_backend(self):
        self.criar_repo()
        self.cfg.repos["web-app"]["fonte"] = str(self.dir / "src")
        self.svc.root, self.svc.sem_telegram = self.backend, True
        self.svc.tg = None
        from ai_agents_orchestrator.engines import Resultado
        def change(prompt, cwd):
            (Path(cwd) / "app.py").write_text("print('review me')\n")
            return Resultado(ok=True, texto="Status: completed\nDone: updated app.py", thread_id="lite")
        self.codex.respostas = [change]
        task = self.svc.delegar("web-app", "change", briefing="Task-specific context")
        self.assertTrue(self.svc.ciclo_worker())
        result = self.quadro.ver(task["id"])
        self.assertEqual(result["estado"], "em_revisao")
        briefing = self.cfg.para_codex / "tarefas" / f"{task['id']}.md"
        self.assertEqual(briefing.read_text(), "Task-specific context")
        self.assertIn(str(briefing), self.codex.chamadas[0]["prompt"])
        self.assertIn("updated app.py", result["resumo"])
        self.assertTrue(self.svc.comando({"cmd": "briefing_status"})["ok"])
        self.assertIn(f"#{task['id']}", self.svc.comando({"cmd": "recado"})["texto"])
        review = self.svc.revisar("web-app", result["branch"])
        self.assertTrue(review["espelho_ok"])
        self.assertTrue(self.svc.ciclo_worker())
        self.assertEqual(self.quadro.ver(review["id"])["estado"], "concluida")
        self.assertEqual(self.codex.chamadas[-1]["sandbox"], "read-only")

    def test_serve_backend_never_publishes_base(self):
        tid, ws, branch, sha = self.ready()
        r = self.backend.push_base("web-app", tid, branch, commit=sha, destino="main")
        self.assertFalse(r["ok"])
        remote = self.cfg.repos["web-app"]["github_url"]
        self.assertNotEqual(sh("git", "--git-dir", remote, "rev-parse", "main").strip(), sha)

    @unittest.skipIf(compat.WINDOWS, "Unix peer credentials; authenticated TCP is covered in test_windows")
    def test_local_socket_dispatch_authenticates_real_peer(self):
        self.svc.root = self.backend
        a, b = socket.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        b.settimeout(5)
        self.svc._socket_slots.acquire()
        thread = threading.Thread(target=self.svc._atender_socket, args=(a,))
        thread.start()
        try:
            b.sendall(b'{"cmd":"briefing_status"}\n')
            response = json.loads(b.recv(65536))
            self.assertTrue(response["ok"])
        finally:
            thread.join(5)
        self.assertFalse(thread.is_alive())

    def test_single_user_service_lock(self):
        from ai_agents_orchestrator import lite
        started, finish = threading.Event(), threading.Event()
        svc = mock.Mock()
        def run():
            started.set()
            self.assertTrue(finish.wait(5))
        svc.rodar.side_effect = run
        with patch.object(compat, "euid", return_value=1000), patch.object(cli, "montar_servico", return_value=svc), \
                patch("sys.stdout", new_callable=io.StringIO):
            results = []
            thread = threading.Thread(target=lambda: results.append(lite.serve(self.cfg)))
            thread.start()
            try:
                self.assertTrue(started.wait(5))
                self.assertEqual(lite.serve(self.cfg), 2)
            finally:
                finish.set()
                thread.join(5)
            self.assertEqual(results, [0])
            self.assertEqual(svc.rodar.call_count, 1)

    @unittest.skipIf(compat.WINDOWS, "AF_UNIX pathname limit; Windows uses TCP and a user endpoint file")
    def test_socket_startup_error_is_reported_without_hanging(self):
        self.cfg.socket = self.dir / ("x" * 120)
        thread = threading.Thread(target=self.svc.run_socket)
        thread.start()
        self.assertTrue(self.svc.socket_ready.wait(5))
        thread.join(5)
        self.assertIn("local socket", self.svc.socket_error)
        self.assertTrue(self.svc.parar_ev.is_set())
        self.assertFalse(thread.is_alive())


@unittest.skipIf(compat.WINDOWS, "BSD/macOS getpeereid compatibility; no Unix peer API on Windows")
class PortablePeer(unittest.TestCase):
    def test_bsd_getpeereid_without_linux_socket_option(self):
        from ai_agents_orchestrator import servico
        import ctypes
        real_hasattr = hasattr
        def getpeereid(fd, uid, gid):
            ctypes.cast(uid, ctypes.POINTER(ctypes.c_uint))[0] = 501
            return 0
        lib = mock.Mock(getpeereid=getpeereid)
        with patch("builtins.hasattr", side_effect=lambda obj, name:
                   False if obj is socket and name == "SO_PEERCRED" else real_hasattr(obj, name)), \
                patch("ctypes.CDLL", return_value=lib):
            self.assertEqual(servico.peer_uid(mock.Mock()), 501)


if __name__ == "__main__":
    unittest.main()
