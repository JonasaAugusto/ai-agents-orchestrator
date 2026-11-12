"""Run a real local task lifecycle with fake engines; no network, accounts or daemons.

Run with the installed package: python examples/offline_relay.py
All Git repositories, SQLite files and reports are disposable.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

from ai_agents_orchestrator import cli, config
from ai_agents_orchestrator.engines import Resultado


def git_environment() -> dict[str, str]:
    return {"PATH": os.defpath, "HOME": "/nonexistent", "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull, "GIT_TERMINAL_PROMPT": "0"}


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, env=git_environment(),
                          check=True, capture_output=True, text=True).stdout.strip()


def create_source(root: Path) -> Path:
    source = root / "source"
    source.mkdir()
    git("init", "-q", "-b", "main", cwd=source)
    # A fixed synthetic commit, imported only into this disposable repository.
    stream = ("blob\nmark :1\ndata 11\nvalue = 1\n\n"
              "commit refs/heads/main\ncommitter Example <demo@example.com> 1000000000 +0000\n"
              "data 7\nInitial\nM 100644 :1 demo.py\n\ndone\n")
    subprocess.run(["git", "fast-import", "--quiet"], cwd=source, input=stream,
                   env=git_environment(), check=True, capture_output=True, text=True)
    git("reset", "--hard", "HEAD", cwd=source)
    return source


def run_demo(root: Path) -> dict:
    source = create_source(root)
    cfg = config.Config(state_dir=root / "state", root_state=root / "root-state", channel="none",
                        service_user="offline-demo", templates_dir=root / "templates",
                        rules_file=root / "rules.md", division_file=root / "division.md")
    cfg.repos = {"demo": {"fonte": str(source), "branch_base": "main", "local": True}}
    svc = cli.montar_servico(cfg, dry_run=True, sem_telegram=True)
    try:
        cfg.repos_dir.mkdir(parents=True, exist_ok=True)
        git("clone", "--mirror", str(source), str(cfg.repos_dir / "demo.git"), cwd=root)
        sha = git("rev-parse", "HEAD", cwd=source)
        svc.root.espelho_status["demo"] = {"ok": True, "status": {"confirmado": True, "sha": sha, "sha_origem": sha}}

        def worker(prompt, cwd):
            (cwd / "demo.py").write_text("value = 2\n", encoding="utf-8")
            return Resultado(ok=True, texto="Status: completed\nChanged demo.py using a fake Codex.", thread_id="fake")

        svc.codex.respostas.append(worker)
        queued = svc.comando({"cmd": "delegar", "repo": "demo", "titulo": "Offline example", "arquivos": ["demo.py"]})
        if not queued.get("ok") or not svc.ciclo_worker():
            raise RuntimeError(f"delegation failed: {queued}")
        result = svc.comando({"cmd": "resultado", "id": queued["id"]})
        task = result.get("tarefa", {})
        if not result.get("ok") or task.get("estado") != "em_revisao":
            raise RuntimeError(f"task did not reach review: {result}")
        record = svc.quadro.ver(queued["id"])
        patches = list(Path(record["patch"]).glob("*.patch")) if record["patch"] else []
        if not patches or "value = 2" not in patches[0].read_text():
            raise RuntimeError("delivery did not contain the fake engine's change")
        if len(svc.codex.chamadas) != 1 or any(call[0].startswith("push_") for call in svc.root.chamadas):
            raise RuntimeError("unexpected engine or publishing call")
        print(f"delegate: task #{queued['id']}")
        print("result: in review; demo.py changed from value = 1 to value = 2")
        print("Fake Codex calls: 1; remote pushes: 0; all state is disposable.")
        return result
    finally:
        svc.estado.c.close()
        svc.quadro.c.close()


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="aao-example-") as temp:
        run_demo(Path(temp))
