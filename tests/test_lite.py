"""Disposable lite installs: no providers, root services or production resources."""
import io
import json
import re
import os
import tempfile
import socket
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from unittest import mock

from ai_agents_orchestrator import assistente, cli, compat, config, doctor, engines, lite, workspace
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
            answers = iter(["lite", str(root / "state"), "UTC", "", "", "codex", "restricted", "", "y"])
            with patch.dict(os.environ, {"AAO_CONFIG": str(selected)}), patch.object(config, "CONFIG_FILE", old):
                wizard = assistente.Assistente(entrada=lambda _: next(answers), saida=lambda _: None,
                                               amb=AmbienteFalso())
                self.assertEqual(wizard.executar(), 0)
            self.assertTrue(selected.is_file())
            self.assertFalse(old.exists())

    def test_setup_installs_plugin_checks_login_and_preserves_second_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "config.toml"
            answers = iter(["lite", str(Path(tmp) / "state"), "UTC", "", "", "codex", "restricted", "", "y"])
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

    def test_setup_does_not_install_plugin_without_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "config.toml"
            answers = iter(["lite", str(Path(tmp) / "state"), "UTC", "", "", "codex", "restricted", "", "n"])
            amb = AmbienteFalso()
            wizard = assistente.Assistente(config_file=p, env_file=Path(tmp) / "env",
                entrada=lambda _: next(answers), saida=lambda _: None, amb=amb)
            self.assertEqual(wizard.executar(), 0)
            self.assertFalse(any("plugin" in c and "install" in c for c in amb.comandos))

    def test_claude_availability_uses_official_status_in_user_environment(self):
        env = {"HOME": "/tmp/fake-user", "PATH": "/tmp/bin:/usr/bin"}
        runner = engines.ClaudeRunner(Path("/tmp/fake-state"), child_env=env)
        with patch("subprocess.run", return_value=mock.Mock(returncode=0)) as run:
            self.assertTrue(runner.disponivel())
            self.assertEqual(run.call_args.args[0], ["claude", "auth", "status"])
            self.assertEqual(run.call_args.kwargs["env"], env)
        with patch("subprocess.run", side_effect=FileNotFoundError):
            self.assertFalse(runner.disponivel())


class LiteTranscriptsProject(unittest.TestCase):
    """The mirrored project must not depend only on the directory where `aao serve` happens to run."""

    def setUp(self):
        self.addCleanup(config.definir_fuso, config.nome_fuso())
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.transcripts = self.root / "projects"
        self.project = self.root / "my app"          # a path with a space, as on Windows profiles
        self.folder = self.transcripts / lite.nome_pasta_projeto(self.project)
        self.folder.mkdir(parents=True)
        (self.folder / "s.jsonl").write_text(json.dumps(
            {"type": "user", "timestamp": "2026-11-12T12:00:00Z", "message": {"content": "from the project"}}) + "\n")
        self.cfg = config.Config(mode="lite", state_dir=self.root / "state", fonte_transcripts=self.transcripts,
                                 fonte_memoria=self.transcripts / "memory")

    def test_config_key_comes_from_toml_and_environment(self):
        path = self.root / "config.toml"
        path.write_text('mode = "lite"\n[lite]\ntranscripts_project = "C:\\\\Users\\\\me\\\\app"\n', encoding="utf-8")
        self.assertEqual(config.carregar({"AAO_CONFIG": str(path)}).transcripts_project, "C:\\Users\\me\\app")
        env = {"AAO_CONFIG": str(path), "AAO_LITE_TRANSCRIPTS_PROJECT": "/work/other"}
        self.assertEqual(config.carregar(env).transcripts_project, "/work/other")

    def test_explicit_project_wins_over_cwd(self):
        self.cfg.transcripts_project = str(self.project)
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        old = os.getcwd()
        self.addCleanup(os.chdir, old)
        os.chdir(elsewhere)
        pasta, origem = lite.pasta_do_projeto(self.cfg)
        self.assertEqual((pasta, origem), (self.folder, "lite.transcripts_project"))

    def test_without_explicit_value_cwd_is_still_used(self):
        old = os.getcwd()
        self.addCleanup(os.chdir, old)
        self.project.mkdir()
        os.chdir(self.project)
        self.assertEqual(lite.pasta_do_projeto(self.cfg)[1], "cwd")
        self.assertEqual(lite.pasta_do_projeto(self.cfg)[0].name, lite.nome_pasta_projeto(os.getcwd()))

    def test_encoded_folder_name_is_accepted(self):
        self.cfg.transcripts_project = self.folder.name
        self.assertEqual(lite.pasta_do_projeto(self.cfg)[0], self.folder)

    def test_windows_style_path_is_encoded_like_claude_code(self):
        self.assertEqual(lite.nome_pasta_projeto("C:\\Users\\me\\my app"), "C--Users-me-my-app")

    def test_mirror_reads_explicit_project_when_cwd_differs(self):
        self.cfg.transcripts_project = str(self.project)
        self.cfg.mirror.mkdir(parents=True, exist_ok=True)
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        old = os.getcwd()
        self.addCleanup(os.chdir, old)
        os.chdir(elsewhere)
        backend = lite.LocalBackend(self.cfg)
        self.addCleanup(lambda: None)
        result = backend._mirror()
        self.assertTrue(result["ok"], result)
        self.assertIn("from the project", (self.cfg.mirror / "transcript-cauda.jsonl").read_text())

    def test_diagnostics_explain_each_case(self):
        self.cfg.transcripts_project = str(self.project)
        self.assertEqual(lite.diagnostico_transcripts(self.cfg)[0], "ok")
        self.cfg.transcripts_project = str(self.root / "typo")
        estado, msg, fix = lite.diagnostico_transcripts(self.cfg)
        self.assertEqual(estado, "FAIL")
        self.assertIn("lite.transcripts_project", msg)
        self.assertIn("typo", msg)
        self.cfg.transcripts_project = ""
        old = os.getcwd()
        self.addCleanup(os.chdir, old)
        os.chdir(self.root)
        estado, msg, fix = lite.diagnostico_transcripts(self.cfg)
        self.assertEqual(estado, "warn")
        self.assertIn("transcripts_project", fix)

    def test_unreadable_transcripts_directory_is_a_warning_not_a_crash(self):
        with patch.object(Path, "is_dir", side_effect=PermissionError(13, "Permission denied")):
            estado, msg, _fix = lite.diagnostico_transcripts(self.cfg)
        self.assertEqual(estado, "warn")
        self.assertIn("does not exist", msg)

    def test_doctor_reports_the_check_in_lite(self):
        self.cfg.transcripts_project = str(self.root / "typo")
        items = doctor.checar_base(self.cfg, AmbienteFalso())
        item = next(i for i in items if i.nome == "transcripts project")
        self.assertEqual(item.estado, doctor.FALHA)

    def test_setup_asks_and_writes_the_project(self):
        path = self.root / "setup" / "config.toml"
        answers = iter(["lite", str(self.root / "st"), "UTC", str(self.transcripts), str(self.project),
                        "codex", "restricted", "", "n"])
        out = []
        wizard = assistente.Assistente(config_file=path, env_file=self.root / "env", entrada=lambda _: next(answers),
                                       saida=out.append, amb=AmbienteFalso())
        self.assertEqual(wizard.executar(), 0)
        self.assertEqual(config.carregar({}, path).transcripts_project, str(self.project))
        self.assertTrue(any(line.startswith("Transcripts: ok") for line in out), out)


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

    def test_ssh_scheme_is_opt_in_for_github_remotes(self):
        self.cfg.repos["web-app"] = {"github": "dono/web-app"}
        self.cfg.push_allowlist = ["dono/web-app"]
        self.assertEqual(self.backend._url("web-app"), "https://github.com/dono/web-app.git")
        self.cfg.push_url_scheme = "ssh"
        self.assertEqual(self.backend._url("web-app"), "git@github.com:dono/web-app.git")

    def test_mirror_fetches_only_recent_workspaces(self):
        base = self.cfg.workspaces / "web-app"
        for n in range(1, 31):
            (base / str(n)).mkdir(parents=True)
            (base / str(n) / ".git").mkdir()
            os.utime(base / str(n) / ".git", ns=(n * 10**9, n * 10**9))
        escolhidos = {p.parent.name for p in self.backend._workspaces_recentes("web-app")}
        self.assertEqual(len(escolhidos), self.backend.MAX_WORKSPACES_ESPELHADOS)
        self.assertIn("30", escolhidos)
        self.assertNotIn("1", escolhidos)

    def test_lite_probe_defaults_to_hourly_unless_chosen(self):
        with tempfile.TemporaryDirectory() as tmp:
            arq = Path(tmp) / "c.toml"
            arq.write_text('mode = "lite"\n')
            self.assertEqual(config.carregar({}, arq).claude_sonda_minutos, 60)
            arq.write_text('mode = "lite"\n[claude]\nprobe_minutes = 20\n')
            self.assertEqual(config.carregar({}, arq).claude_sonda_minutos, 20)

    def test_doctor_lite_stopped_service_is_a_warning_not_a_failure(self):
        cfg = config.Config(mode="lite", state_dir=Path("/nonexistent/state"), local_transport="unix")
        amb = AmbienteFalso()
        amb.socket_ativo = lambda _p: False
        itens = {i.nome: i for i in doctor.checar_runtime(cfg, amb)}
        self.assertEqual(itens["service socket"].estado, doctor.AVISO)
        self.assertIn("aao serve", itens["service socket"].detalhe + itens["service socket"].correcao)

    def test_delegate_model_and_effort_apply_to_that_task_only(self):
        self.criar_repo()
        self.cfg.repos["web-app"]["fonte"] = str(self.dir / "src")
        self.assertFalse(self.svc.delegar("web-app", "x", modelo="bad model!")["ok"])
        self.assertFalse(self.svc.delegar("web-app", "x", esforco="turbo")["ok"])
        t = self.svc.delegar("web-app", "change", modelo="gpt-6.1-sol", esforco="medium")
        self.assertTrue(self.svc.ciclo_worker())
        chamada = self.codex.chamadas[0]
        self.assertEqual((chamada.get("modelo"), chamada.get("esforco")), ("gpt-6.1-sol", "medium"))
        self.assertEqual(self.quadro.ver(t["id"])["esforco"], "medium")

    def test_delegate_wait_prints_short_result_and_exit_code(self):
        respostas = [{"ok": True, "id": 7},
                     {"ok": True, "tarefa": {"id": 7, "estado": "em_revisao", "branch": "agent/7", "diffstat": "1 file",
                                             "aviso": None, "erro": None, "resumo": "feito"}}]
        pedidos = []
        def falso(_cfg, req, timeout=None):
            pedidos.append(req)
            return respostas.pop(0)
        saida = io.StringIO()
        with patch.object(cli, "chamar", falso), mock.patch("sys.stdout", saida), mock.patch("sys.stderr", io.StringIO()):
            code = cli.main(["delegate", "--repo", "web-app", "--title", "t", "--wait", "--effort", "medium"])
        self.assertEqual(code, 0)
        self.assertEqual(pedidos[0]["esforco"], "medium")
        self.assertEqual(pedidos[1]["cmd"], "esperar")
        self.assertIn("Status: em_revisao", saida.getvalue())
        self.assertIn("feito", saida.getvalue())

    def test_mirror_without_session_ignores_other_projects(self):
        root = self.dir / "projects"
        outro = root / "-outro-projeto"
        outro.mkdir(parents=True)
        (outro / "x.jsonl").write_text(json.dumps({"type": "user", "message": {"content": "segredo de outro projeto"}}) + "\n")
        self.cfg.fonte_transcripts = root
        r = self.backend._mirror()
        self.assertFalse(r["ok"] and "segredo" in (self.cfg.mirror / "transcript-cauda.jsonl").read_text())
        atual = root / re.sub(r"[^A-Za-z0-9]", "-", os.getcwd())
        atual.mkdir()
        (atual / "y.jsonl").write_text(json.dumps({"type": "user", "message": {"content": "deste projeto"}}) + "\n")
        self.backend._mirror()
        texto = (self.cfg.mirror / "transcript-cauda.jsonl").read_text()
        self.assertIn("deste projeto", texto)
        self.assertNotIn("segredo", texto)

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
        # No Windows o diretório temporário vem com nome curto (RUNNER~1); o serviço registra o caminho resolvido.
        prompt = self.codex.chamadas[0]["prompt"]
        # O prompt usa "/" como separador mesmo no Windows.
        prompt = prompt.replace("\\", "/")
        self.assertTrue(briefing.as_posix() in prompt or briefing.resolve().as_posix() in prompt, prompt[:300])
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

    @unittest.skipIf(compat.WINDOWS, "AF_UNIX pathname limit; Windows uses TCP and a user endpoint file")
    def test_serve_with_failing_socket_joins_threads_before_closing_the_database(self):
        import faulthandler
        faulthandler.enable()  # a crash inside sqlite would abort the whole run with a traceback instead of passing
        self.cfg.socket = self.dir / ("x" * 120)
        self.svc.sem_telegram, self.svc.tg = True, None
        baseline = set(threading.enumerate())
        alive_at_close = []

        class Watched:
            def __init__(self, real):
                self.real = real

            def close(self):
                alive_at_close.extend(t for t in threading.enumerate() if t not in baseline and t.is_alive())
                self.real.close()

            def __getattr__(self, name):
                return getattr(self.real, name)

        self.svc.estado.c = Watched(self.svc.estado.c)
        self.svc.quadro.c = Watched(self.svc.quadro.c)
        out = io.StringIO()
        with patch.object(compat, "euid", return_value=1000), patch.object(cli, "montar_servico", return_value=self.svc), \
                patch("sys.stdout", out):
            self.assertEqual(lite.serve(self.cfg), 1)
        self.assertEqual(alive_at_close, [])
        self.assertIn("path too long", out.getvalue().lower())  # the real OSError text, not a generic hint


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
