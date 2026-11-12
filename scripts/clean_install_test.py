"""Install a source checkout or built distribution in a new, credential-free venv."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import runpy
import shlex
import shutil
import subprocess
import sys
import tempfile
import venv
from pathlib import Path


def isolated_environment(root: Path, bindir: Path) -> dict[str, str]:
    """Allowlist instead of inheriting account tokens, Python imports or AAO/Git overrides."""
    return {"PATH": str(bindir) + os.pathsep + os.defpath, "HOME": str(root / "home"),
            "LANG": "C.UTF-8", "PYTHONUTF8": "1", "PYTHONNOUSERSITE": "1",
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull, "GIT_TERMINAL_PROMPT": "0",
            "AAO_CONFIG": str(root / "config.toml"), "AAO_ENV_FILE": str(root / "absent-env"),
            "AAO_PATHS_STATE_DIR": str(root / "state"), "AAO_PATHS_ROOT_STATE_DIR": str(root / "root-state"),
            "AAO_PATHS_CLAUDE_TRANSCRIPTS": str(root / "transcripts"),
            "AAO_PATHS_CLAUDE_MEMORY": str(root / "memory"), "AAO_PATHS_CLAUDE_SESSIONS": str(root / "sessions"),
            "AAO_PATHS_RULES_FILE": str(root / "rules.md"), "AAO_PATHS_DIVISION_FILE": str(root / "division.md"),
            "AAO_PATHS_TEMPLATES_DIR": str(root / "templates"), "AAO_PATHS_SOCKET": str(root / "cli.sock"),
            "AAO_PATHS_ROOT_SOCKET": str(root / "root.sock"), "AAO_PUBLISH_MANIFEST": str(root / "publish.json"),
            "AAO_PUBLISH_KEY_FILE": str(root / "absent-key"), "PIP_CONFIG_FILE": os.devnull,
            "PIP_DISABLE_PIP_VERSION_CHECK": "1", "PIP_NO_CACHE_DIR": "1"}


def validate_plugin(root: Path) -> None:
    """Static bundled-plugin contract, not a substitute for loading it in Claude Code."""
    from ai_agents_orchestrator.cli import parser

    plugin = root / "claude-plugin"
    manifest = json.loads((plugin / ".claude-plugin/plugin.json").read_text())
    if manifest.get("name") != "ai-agents-orchestrator" or not manifest.get("version"):
        raise ValueError("invalid plugin manifest")
    marketplace = json.loads((root / ".claude-plugin/marketplace.json").read_text())
    for item in marketplace["plugins"]:
        source = (root / item["source"]).resolve()
        if not source.is_relative_to(root.resolve()) or not (source / ".claude-plugin/plugin.json").is_file():
            raise ValueError("invalid marketplace source")
    for path in [plugin / "skills/orchestrator-workflow/SKILL.md", *sorted((plugin / "commands").glob("*.md"))]:
        text = path.read_text()
        match = re.match(r"\A---\n(.*?)\n---\n", text, re.S)
        if not match:
            raise ValueError(f"missing frontmatter: {path.name}")
        fields = dict(line.split(":", 1) for line in match[1].splitlines() if ":" in line)
        # The bundled files use single-line scalar metadata, not arbitrary YAML.
        # Validate quoted scalars instead of accepting an unterminated YAML string.
        for key, raw in fields.items():
            value = raw.strip()
            if value.startswith('"'):
                try:
                    fields[key] = json.loads(value)
                except ValueError as error:
                    raise ValueError(f"invalid frontmatter: {path.name}") from error
            elif value.startswith("'"):
                if not re.fullmatch(r"'(?:[^']|'')*'", value):
                    raise ValueError(f"invalid frontmatter: {path.name}")
                fields[key] = value[1:-1].replace("''", "'")
        if not fields.get("description", "").strip() or (path.name == "SKILL.md" and fields.get("name", "").strip() != "orchestrator-workflow"):
            raise ValueError(f"invalid frontmatter: {path.name}")
    if len(list((plugin / "commands").glob("*.md"))) != 4:
        raise ValueError("missing plugin commands")
    hooks = json.loads((plugin / "hooks/hooks.json").read_text())["hooks"]
    if set(hooks) != {"StopFailure", "UserPromptSubmit", "SessionStart", "Stop"}:
        raise ValueError("invalid hook events")
    for groups in hooks.values():
        if not groups:
            raise ValueError("empty hook event")
        for group in groups:
            if not group.get("hooks"):
                raise ValueError("empty hook group")
            for hook in group["hooks"]:
                argv = shlex.split(hook.get("command", ""))
                if hook.get("type") != "command" or not argv or argv[0] != "aao" or not 0 < hook.get("timeout", 0) <= 30:
                    raise ValueError("invalid hook command/type/timeout")
                try:
                    with contextlib.redirect_stderr(io.StringIO()):
                        parser().parse_args(argv[1:])
                except SystemExit as error:
                    raise ValueError("invalid hook command") from error


def run(command: list[str], *, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True)
    if result.returncode:
        # All subprocesses run with synthetic state and no inherited secrets.
        raise RuntimeError(f"{command[0]} failed ({result.returncode})\n{result.stdout}\n{result.stderr}")
    return result


def check_doctor(aao: str, cwd: Path, env: dict[str, str], *, configured: bool) -> None:
    result = subprocess.run([aao, "doctor", "--json"], cwd=cwd, env=env, capture_output=True, text=True)
    items = json.loads(result.stdout)
    failures = {item["nome"] for item in items if item["estado"] == "FAIL"}
    expected = {"codex CLI", "service socket", "root daemon socket"}
    if not configured:
        expected |= {"service user", "repositories"}
    if result.returncode != 1 or failures != expected:
        raise RuntimeError(f"unexpected doctor result: exit={result.returncode}, failures={sorted(failures)}")
    print("doctor: expected missing prerequisites confirmed (no agents or daemons installed).")


def exercise(root: Path, source: Path, *, empty: bool) -> None:
    import ai_agents_orchestrator
    example = runpy.run_path(str(source / "examples/offline_relay.py"))
    create_source, run_demo = example["create_source"], example["run_demo"]

    if not Path(ai_agents_orchestrator.__file__).resolve().is_relative_to(Path(sys.prefix).resolve()):
        raise RuntimeError("package imported from checkout instead of clean venv")
    for name in ("BASE_RULES.example.md", "DIVISION.example.md"):
        if not (Path(sys.prefix) / "share/ai-agents-orchestrator/rules" / name).is_file():
            raise RuntimeError(f"installed package is missing bundled rules: {name}")
    env = isolated_environment(root, Path(sys.executable).parent)
    aao = str(Path(sys.executable).with_name("aao"))
    if empty:
        check_doctor(aao, root, env, configured=False)
    fixture = create_source(root)
    result = run([aao, "setup", "--non-interactive", "--output-dir", str(root / "setup"),
                  "--repo-source", str(fixture)], cwd=root, env=env)
    print(result.stdout, end="")
    env["AAO_CONFIG"] = str(root / "setup/config.toml")
    # Let the generated config supply its own isolated paths.
    env = {key: value for key, value in env.items() if not key.startswith(("AAO_PATHS_", "AAO_PUBLISH_"))}
    check_doctor(aao, root, env, configured=True)
    demo = root / "demo"
    demo.mkdir()
    run_demo(demo)
    validate_plugin(source)
    print("plugin: manifest, marketplace, skill frontmatter, commands and hooks valid.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--distribution", type=Path, help="wheel or sdist; default: pip install . from a clean source copy")
    parser.add_argument("--empty", action="store_true", help="also check doctor's expected failures before setup")
    parser.add_argument("--exercise", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[1]
    if args.exercise:
        exercise(args.exercise, source, empty=args.empty)
        return
    with tempfile.TemporaryDirectory(prefix="aao-clean-") as temp:
        root = Path(temp)
        copied = root / "source-tree"
        shutil.copytree(source, copied, ignore=shutil.ignore_patterns(".git", ".venv", "__pycache__", "*.egg-info", "build", "dist", ".mypy_cache", ".ruff_cache"))
        venv_dir = root / "venv"
        venv.EnvBuilder(with_pip=True).create(venv_dir)
        env = isolated_environment(root, venv_dir / "bin")
        (root / "home").mkdir()
        python = str(venv_dir / "bin/python")
        package = str(args.distribution.resolve()) if args.distribution else "."
        run([python, "-m", "pip", "install", "--no-deps", package], cwd=copied, env=env)
        run([python, "-m", "pip", "check"], cwd=root, env=env)
        command = [python, str(copied / "scripts/clean_install_test.py"), "--exercise", str(root)]
        if args.empty:
            command.append("--empty")
        result = run(command, cwd=copied, env=env)
        print(result.stdout, end="")
        print("Clean installation passed (fresh venv; installed package; no real credentials).")


if __name__ == "__main__":
    main()
