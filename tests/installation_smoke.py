"""Run against the clean pipx installation; no credentials, providers or global state."""
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
from pathlib import Path


def main():
    executable = Path(os.environ["PIPX_BIN_DIR"]) / ("aao.exe" if os.name == "nt" else "aao")
    with tempfile.TemporaryDirectory(prefix="aao clean ação ") as directory:
        root = Path(directory)
        config = root / "config.toml"
        empty_bin = root / "empty-bin"
        empty_bin.mkdir()
        env = {**os.environ, "AAO_SERVICE_LOCAL_TRANSPORT": "tcp",
               "HOME": str(root), "USERPROFILE": str(root), "LOCALAPPDATA": str(root / "local"),
               "XDG_CONFIG_HOME": str(root / "config"), "XDG_STATE_HOME": str(root / "state"),
               "PATH": str(empty_bin), "PYTHONUTF8": "1"}
        for name in list(env):
            if name.startswith("AAO_") and name != "AAO_SERVICE_LOCAL_TRANSPORT":
                del env[name]

        def run(*args, stdin=None):
            return subprocess.run([str(executable), "--config", str(config), *args], input=stdin,
                                  capture_output=True, text=True, encoding="utf-8", timeout=30, env=env, cwd=root)

        # Real guided setup, with no engine installed in this disposable user's PATH.
        answers = "\n".join(["lite", str(root / "state"), "UTC", "", "codex", "restricted", ""]) + "\n"
        result = run("setup", stdin=answers)
        assert result.returncode == 0, result.stderr + result.stdout
        assert config.is_file()
        assert (root / "BASE_RULES.md").is_file(), "wheel must contain example rules"
        before = config.read_bytes()
        assert run("setup").returncode == 0
        assert config.read_bytes() == before, "second setup must preserve config"
        process = subprocess.Popen([str(executable), "--config", str(config), "serve", "--dry-run"],
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                   env=env, cwd=root)
        lines = queue.Queue()
        def read():
            for line in process.stdout:
                lines.put(line)
            lines.put(None)
        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        try:
            while True:
                line = lines.get(timeout=20)
                assert line is not None, "serve exited before readiness"
                if "Orchestrator started." in line:
                    break
            result = run("status")
            assert result.returncode == 0, result.stderr + result.stdout
            assert json.loads(result.stdout)["ok"]
            assert run("hook-prompt").returncode == 0
            assert run("claude-limit", stdin='{"error":"unrelated"}').returncode == 0
            assert run("claude-usage", stdin='{}').returncode == 0
            result = run("doctor", "--json")
            items = {item["nome"]: item for item in json.loads(result.stdout)}
            assert items["service socket"]["estado"] == "ok", result.stdout
            assert "root daemon socket" not in items
            assert run("arm").returncode == 0
            assert run("disarm").returncode == 0
            assert run("list").returncode == 0
            # Same API through the installed CLI requests a graceful worker shutdown.
            result = run("stop")
            assert result.returncode == 0, result.stdout
            assert process.wait(timeout=20) == 0
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            reader.join(5)
            process.stdout.close()
    print("Clean pipx install: guided/idempotent setup, user serve, TCP CLI, hooks, doctor and stop passed")


if __name__ == "__main__":
    main()
