"""Portable lite regressions; Windows API integration runs on windows-latest."""
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from ai_agents_orchestrator import cli, config, engines


class WindowsPaths(unittest.TestCase):
    def test_windows_config_defaults_to_localappdata_and_lite(self):
        from ai_agents_orchestrator import compat
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(compat, "WINDOWS", True):
            env = {"LOCALAPPDATA": tmp, "AAO_CONFIG": str(Path(tmp) / "absent.toml")}
            cfg = config.carregar(env)
            self.assertEqual(cfg.mode, "lite")
            self.assertEqual(cfg.state_dir, Path(tmp) / config.NOME / "state")
            self.assertTrue(cfg.use_tcp)
            with mock.patch.dict(os.environ, env):
                self.assertEqual(config.user_config_dir(), Path(tmp) / config.NOME)

    def test_full_mode_rejected_on_windows(self):
        from ai_agents_orchestrator import compat
        with mock.patch.object(compat, "WINDOWS", True):
            with self.assertRaisesRegex(ValueError, "Linux"):
                config.carregar({"AAO_MODE": "full", "AAO_CONFIG": "absent.toml"})


class WindowsProcesses(unittest.TestCase):
    def test_windows_usage_reads_real_pipe_without_selectors(self):
        from ai_agents_orchestrator import compat
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "provider.py"
            script.write_text("import json,sys\nfor _ in range(3): sys.stdin.readline()\n"
                              "print(json.dumps({'id':2,'result':{'rateLimits':{'planType':'test'}}}),flush=True)\n"
                              "sys.stdin.read()\n", encoding="utf-8")
            jobs = []
            def attach(process):
                def close():
                    if process.poll() is None:
                        process.kill()
                obj = mock.Mock(close=close)
                jobs.append(obj)
                return obj
            with mock.patch.object(compat, "WINDOWS", True), \
                    mock.patch.object(compat, "command", return_value=[sys.executable, str(script)]), \
                    mock.patch.object(compat, "process_group", return_value={}), \
                    mock.patch.object(compat, "ProcessJob", side_effect=attach), \
                    mock.patch.object(engines.selectors, "DefaultSelector", side_effect=AssertionError("no pipe select on Windows")):
                result = engines.uso_codex(Path(tmp), child_env=engines.env_usuario(), timeout=5)
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["plano"], "test")
            self.assertEqual(len(jobs), 1)

    def test_ps1_detected_without_pathext_entry(self):
        from ai_agents_orchestrator import compat
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "claude.ps1"
            path.write_text("Write-Output ok", encoding="utf-8")
            with mock.patch.object(compat, "WINDOWS", True), mock.patch.object(compat.shutil, "which", return_value=None):
                self.assertEqual(compat.which("claude", {"PATH": tmp}), str(path.resolve()))

    def test_npm_shim_uses_node_with_uninterpreted_arguments(self):
        from ai_agents_orchestrator import compat
        with tempfile.TemporaryDirectory(prefix="aao shim space ") as tmp:
            shim = Path(tmp) / "codex.cmd"
            shim.write_text('@echo off\r\n"%_prog%" "%dp0%\\node_modules\\@openai\\codex\\bin\\codex.js" %*\r\n')
            with mock.patch.object(compat, "WINDOWS", True), mock.patch.object(
                    compat, "which", side_effect=lambda prog, env=None: str(shim) if prog == "codex" else "node.exe"):
                args = compat.command(["codex", "exec", "a&b", 'sandbox_mode="workspace-write"'])
            self.assertEqual(args, ["node.exe", str(Path(tmp) / "node_modules/@openai/codex/bin/codex.js"),
                                    "exec", "a&b", 'sandbox_mode="workspace-write"'])

    def test_windows_child_environment_preserved_without_service_secrets(self):
        from ai_agents_orchestrator import compat
        with mock.patch.object(compat, "WINDOWS", True), mock.patch.dict(os.environ, {
                "SystemRoot": "C:\\Windows", "LOCALAPPDATA": "C:\\Users\\Test\\Local",
                "AAO_PUBLISH_KEY": "fake-secret", "PYTHONUTF8": "0"}):
            env = engines.env_usuario()
            self.assertEqual(env["SystemRoot"], "C:\\Windows")
            self.assertEqual(env["PYTHONUTF8"], "1")
            self.assertNotIn("AAO_PUBLISH_KEY", env)

    def test_ps1_shim_uses_file_arguments_without_shell(self):
        from ai_agents_orchestrator import compat
        with mock.patch.object(compat, "WINDOWS", True), mock.patch.object(
                compat, "which", return_value=r"C:\Program Files\Tools\codex.ps1"):
            cmd = compat.command(["codex", "exec", "a path", "a&b"])
        self.assertEqual(cmd[-5:], ["-File", r"C:\Program Files\Tools\codex.ps1", "exec", "a path", "a&b"])
        self.assertNotIn("-Command", cmd)

    def test_windows_cancel_kills_tree_once(self):
        from ai_agents_orchestrator import compat
        ctl = engines._Proc()
        ctl.p = mock.Mock(pid=123, poll=lambda: None)
        with mock.patch.object(compat, "WINDOWS", True), mock.patch.object(compat.subprocess, "run", return_value=mock.Mock(returncode=0)) as run:
            ctl.terminar()
            ctl.terminar()
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0][-4:], ["/PID", "123", "/T", "/F"])

    def test_subprocess_utf8_and_crlf(self):
        from ai_agents_orchestrator import compat
        ctl = engines._Proc()
        code, lines, err, timeout = engines._rodar(
            [sys.executable, "-c", "import sys; sys.stdout.buffer.write('ação\\r\\n'.encode('utf-8'))"],
            "", None, engines.env_usuario(), 10, ctl)
        self.assertEqual((code, lines, err, timeout), (0, ["ação\n"], "", False))


class TcpRuntime(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="aao space ação ")
        self.addCleanup(self.tmp.cleanup)
        self.cfg = config.Config(mode="lite", state_dir=Path(self.tmp.name), local_transport="tcp")
        self.cfg.socket = self.cfg.state_dir / "endpoint.json"
        self.svc = cli.montar_servico(self.cfg, True, True)
        self.addCleanup(self.svc.estado.c.close)
        self.addCleanup(self.svc.quadro.c.close)

    def test_authenticated_cli_hooks_and_rejection(self):
        thread = threading.Thread(target=self.svc.run_socket)
        thread.start()
        try:
            self.assertTrue(self.svc.socket_ready.wait(5))
            self.assertIsNone(self.svc.socket_error)
            endpoint = json.loads(self.cfg.socket_path.read_text(encoding="utf-8"))
            self.assertEqual(endpoint["host"], "127.0.0.1")
            self.assertGreaterEqual(len(endpoint["token"]), 64)
            self.assertTrue(cli.chamar(self.cfg, {"cmd": "status"})["ok"])
            with mock.patch("sys.stdout", new_callable=io.StringIO):
                self.assertEqual(cli._hook_prompt(self.cfg, "SessionStart"), 0)
            for token in ("", "invalid"):
                with socket.create_connection((endpoint["host"], endpoint["port"]), timeout=5) as client:
                    client.sendall((json.dumps({"cmd": "delegar", "token": token}) + "\n").encode())
                    result = json.loads(client.recv(4096))
                    self.assertFalse(result["ok"])
                    self.assertIn("auth", result["erro"])
            self.assertEqual(self.svc.quadro.listar(), [])
            self.assertTrue(cli.chamar(self.cfg, {"cmd": "stop"})["stopping"])
            self.assertTrue(self.svc.parar_ev.is_set())
        finally:
            self.svc.parar_ev.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertFalse(self.cfg.socket_path.exists())

    def test_client_rejects_non_loopback_endpoint(self):
        self.cfg.socket_path.write_text(json.dumps({"host": "192.0.2.1", "port": 1234, "token": "a" * 64}))
        with mock.patch("socket.create_connection", side_effect=AssertionError("must never connect remotely")):
            result = cli.chamar(self.cfg, {"cmd": "status"})
        self.assertFalse(result["ok"])


class GitWithoutSymlinks(unittest.TestCase):
    def test_private_copy_updates_refs_objects_and_preserves_original_config(self):
        from ai_agents_orchestrator import compat, gitseguro
        with tempfile.TemporaryDirectory(prefix="aao git space ") as tmp:
            original = Path(tmp) / "repository.git"
            (original / "objects").mkdir(parents=True)
            (original / "refs/heads").mkdir(parents=True)
            (original / "refs/heads/deleted").write_text("old")
            (original / "config").write_text("[alias]\n malicious = !echo bad\n")
            with mock.patch.object(compat, "WINDOWS", True), mock.patch.object(
                    Path, "symlink_to", side_effect=AssertionError("must not require symlink privilege")):
                with gitseguro.repositorio_isolado(original, escrever=True) as private:
                    self.assertNotIn("malicious", (private / "config").read_text())
                    self.assertIn("autocrlf = false", (private / "config").read_text())
                    self.assertIn("longpaths = true", (private / "config").read_text())
                    (private / "refs/heads/deleted").unlink()
                    (private / "refs/heads/new").write_text("new SHA")
                    (private / "objects/object").write_bytes(b"object bytes")
            self.assertFalse((original / "refs/heads/deleted").exists())
            self.assertEqual((original / "refs/heads/new").read_text(), "new SHA")
            self.assertEqual((original / "objects/object").read_bytes(), b"object bytes")
            self.assertIn("malicious", (original / "config").read_text())


@unittest.skipUnless(sys.platform == "win32", "native Windows ACL, console and Job Object APIs; runs in Windows CI")
class NativeWindows(unittest.TestCase):
    def test_private_file_dacl_has_only_current_user(self):
        from ai_agents_orchestrator import compat, lite
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "secret.json"
            lite.write_private(path, "synthetic")
            # Inspect ACL metadata only; no file content or real credential is printed.
            result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                                     "(Get-Acl -LiteralPath $env:AAO_TEST_ACL).Sddl"],
                                    env={**os.environ, "AAO_TEST_ACL": str(path)}, capture_output=True,
                                    text=True, encoding="utf-8", timeout=15, check=True)
            dacl = result.stdout.strip().split("D:", 1)[1]
            self.assertTrue(dacl.startswith("P"), dacl)
            self.assertEqual(dacl.count("("), 1, dacl)
            self.assertIn(compat.windows_sid(), dacl)

    def test_job_close_terminates_real_child_tree(self):
        from ai_agents_orchestrator import compat
        import ctypes
        from ctypes import wintypes
        # Child announces readiness over a pipe; no sleeps or timing assertions.
        code = ("import subprocess,sys; child=subprocess.Popen([sys.executable,'-c',"
                "\"import sys; print('ready', flush=True); sys.stdin.read()\"],stdin=subprocess.PIPE); "
                "print(child.pid, flush=True); sys.stdin.read()")
        process = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   text=True, **compat.process_group())
        job = compat.ProcessJob(process)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = None
        try:
            lines = [process.stdout.readline().strip(), process.stdout.readline().strip()]
            self.assertIn("ready", lines)
            pid = int(next(line for line in lines if line != "ready"))
            handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
            self.assertTrue(handle)
            job.close()
            self.assertIsNotNone(process.wait(timeout=5))
            self.assertEqual(kernel.WaitForSingleObject(handle, 5000), 0)
        finally:
            job.close()
            if handle:
                kernel.CloseHandle(handle)
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            process.stdin.close()
            process.stdout.close()

    def test_ps1_shim_executes_utf8_without_bash(self):
        from ai_agents_orchestrator import compat
        with tempfile.TemporaryDirectory(prefix="aao ps space ") as tmp:
            shim = Path(tmp) / "codex.ps1"
            # ASCII script works in Windows PowerShell 5.1 without a BOM.
            shim.write_text('[Console]::OutputEncoding = [System.Text.Encoding]::UTF8\nWrite-Output $args[0]\n')
            cmd = compat.command([str(shim), "ação with spaces"])
            result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip().lstrip("\ufeff"), "ação with spaces")


class WindowsTask(unittest.TestCase):
    def test_task_xml_quotes_paths_runs_only_as_current_user(self):
        from ai_agents_orchestrator import windows_service
        from xml.etree import ElementTree as ET
        cfg = config.Config(mode="lite")
        xml = windows_service.task_xml(cfg, Path(r"C:\Users\Test Name\settings.toml"),
                                       python=r"C:\Tools & Stuff\python.exe", sid="S-1-5-21-123")
        root = ET.fromstring(xml)
        ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
        self.assertEqual(root.findtext("t:Actions/t:Exec/t:Command", namespaces=ns), r"C:\Tools & Stuff\python.exe")
        self.assertEqual(root.findtext("t:Principals/t:Principal/t:RunLevel", namespaces=ns), "LeastPrivilege")
        self.assertEqual(root.findtext("t:Principals/t:Principal/t:LogonType", namespaces=ns), "InteractiveToken")
        self.assertIn("--config", root.findtext("t:Actions/t:Exec/t:Arguments", namespaces=ns))
        self.assertEqual(root.findtext("t:Triggers/t:LogonTrigger/t:UserId", namespaces=ns), "S-1-5-21-123")


class ConsoleWatch(unittest.TestCase):
    def test_watch_reads_board_and_utf8_events_in_escaped_user_path(self):
        from ai_agents_orchestrator import watch
        from ai_agents_orchestrator.db import Quadro
        with tempfile.TemporaryDirectory(prefix="aao watch # ação ") as tmp:
            cfg = config.Config(mode="lite", state_dir=Path(tmp))
            board = Quadro(cfg.db_quadro)
            try:
                tid = board.criar("ação no console", "", "local")["id"]
                board.atualizar(tid, estado="concluida")
                output = cfg.resultados / str(tid)
                output.mkdir(parents=True)
                (output / "events.jsonl").write_text(json.dumps({
                    "type": "item.completed", "item": {"type": "agent_message", "text": "ação pronta"}},
                    ensure_ascii=False) + "\r\n", encoding="utf-8")
                found = watch.tarefa(cfg, tid)
                self.assertIsNotNone(found)
                self.assertEqual(found["titulo"], "ação no console")
                lines = []
                self.assertEqual(watch.main(cfg, tid, seguir=False, saida=lines.append), 0)
                self.assertIn("ação pronta", "\n".join(lines))
                self.assertIn("completed", lines[-1])
            finally:
                board.c.close()


if __name__ == "__main__":
    unittest.main()
