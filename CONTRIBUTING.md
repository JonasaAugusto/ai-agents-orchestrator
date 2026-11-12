# Contributing

Thanks for helping. This project runs AI agents next to real repositories, so changes are reviewed with security in
mind first.

## Development setup

```bash
git clone https://github.com/JonasaAugusto/ai-agents-orchestrator
cd ai-agents-orchestrator
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
python -m unittest tests.test_clean_install tests.test_doctor_setup tests.test_plugin
ruff check .
mypy
mypy --platform win32 src/ai_agents_orchestrator/{lite,ipc,compat,medicao,config}.py
sh scripts/clean-install-test.sh --empty
python -m build
python -m twine check --strict dist/*
sh scripts/clean-install-test.sh --distribution dist/*.whl
sh scripts/clean-install-test.sh --distribution dist/*.tar.gz
```

On Windows PowerShell, create the venv with `python -m venv .venv`, then use
`.venv\Scripts\python -m pip install -e .` and
`.venv\Scripts\python -W ignore -m unittest discover -s tests -t .`.

The project is Python only; Windows installs `tzdata` for IANA timezones (the WhatsApp extra is optional). The test suite needs
`git` on PATH and never calls OpenAI, Anthropic, Telegram or WhatsApp: engines, chat channels and the root daemon are
replaced by doubles in `ai_agents_orchestrator.testing` and in the tests.

The clean-install check builds in a source copy and creates a fresh venv/HOME, then exercises the installed package
and static plugin contract. Only fetching build tools needs network. The fake task never pushes or deploys.
For a shorter runnable example use `python examples/offline_relay.py`; see [examples](examples/README.md).
`aao doctor` intentionally exits 1 without required agents and daemon sockets; the clean check asserts the exact
missing prerequisites. It does not claim a working live Claude Code/Codex installation.

CI runs Linux/macOS × Python 3.11–3.14, correctness lint, limited type checks, full-history gitleaks, empty Linux
container installs, and wheel/sdist builds and installs. Privileged tests use root **only on disposable CI runners**
with `TMPDIR=/tmp`. Never run the suite with production configuration, credentials or data. For local root runs,
put the test venv/package somewhere the unprivileged subprocesses can traverse; a private checkout can cause
permission failures. The existing Linux-only `/proc` test is not applicable on macOS; do not add skips to hide bugs.
The full-suite command is `python -W ignore -m unittest discover -s tests -t .`; run it as in CI on a disposable
runner. It includes real UID/ownership tests that cannot run as an ordinary local user. The setup commands above
run the focused adoption tests without elevated privileges; do not grant root on your working machine just to
make the full suite pass.

Ruff checks `E9`/`F`; existing unused-import exemptions are listed per legacy file in `pyproject.toml`, not applied
to new files. Mypy (with `check_untyped_defs`) checks setup, the lite runtime, IPC, the compatibility layer, usage measurement, config,
the mirror, doctor, the installer test and the offline example, following other modules silently; CI also checks the
portable modules with `--platform win32` so the Windows-only branches are typed. Fix types instead of adding
`# type: ignore` (unused ignores fail the build). It is not a claim that the entire application is typed. Extend the
scope as modules are annotated. Developer tool
versions are pinned in the `dev` extra. Runtime installation still has no mandatory dependencies.
`.gitleaks.toml` inherits the default secret rules and exempts only one exact synthetic masking-test value in its
specific fixture path (including history). It does not exclude test directories or commits from scanning.

## Rules for changes

- Every behavior change comes with tests; a new test must fail without the change and pass with it.
- Record the failing baseline and passing patch commands/results in the PR. Break the key line deliberately,
  confirm the regression test fails, and restore it. Repeat concurrency tests 20 times using events/barriers or
  injected clocks, not sleeps. Keep artifacts and scratch notes outside the repository.
- No test may depend on sleeping or wall-clock time: inject a clock (`relogio=`), use events or fakes.
- Keep the safe defaults. Anything that widens what an agent can do (sandbox, network, pushes, deploys, channels)
  must be an explicit, documented opt-in and covered by `aao doctor`.
- Never commit secrets, tokens, chat ids, phone numbers or personal data, not even in fixtures. Use obviously fake values.
- Update `README.md`, `README.pt-BR.md` and `CHANGELOG.md` when user-facing behavior changes.
- Internal identifiers are in Portuguese for historical reasons; new user-facing text (CLI, chat messages, prompts,
  docs) is in English.

## Commit and PR

Small, focused pull requests with a clear description and the test commands you ran. Use the PR template and read
[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md). Report vulnerabilities privately through [SECURITY.md](SECURITY.md).
Do not publish releases or tags from a contribution branch; maintainers review and the owner approves publication.
CI covers lite on Linux/macOS/Windows, Python 3.11–3.14. Windows runs suite discovery
with explicit skips for full Linux privilege fixtures, root ownership, /proc and Unix
peer sockets. Native ACL, process-tree and PowerShell tests run only on Windows;
portable TCP, queue, setup and Git workflows run on every lite platform.
