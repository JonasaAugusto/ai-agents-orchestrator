# Primeiros 10 minutos / First 10 minutes

Try the task lifecycle before connecting accounts or installing a service. This walkthrough uses synthetic code
and fake agents. It cannot prove that your Claude Code login, plugin loader, live hooks or production daemon work.
Commands below are for a Linux/macOS shell with Git and Python 3.11–3.14, including `venv`/pip support.
Build-tool downloads require network; the fake task does not. Times are a guide, not a benchmark.

## Minutes 0–2: install locally

```bash
git clone https://github.com/JonasaAugusto/ai-agents-orchestrator
cd ai-agents-orchestrator
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
aao --version
```

Captured from the development checkout on Linux/Python 3.14, 2026-11-12:

```text
1.1.0
```

The checkout may contain unreleased changes. For a published release use the release selector on GitHub; compare
`aao --version` with its changelog. These docs do not assert that this branch has been published.

## Minutes 2–5: run the offline relay

```bash
python examples/offline_relay.py
```

Captured final three lines (earlier progress includes temporary paths and an expected missing-briefing warning):

```text
delegate: task #1
result: in review; demo.py changed from value = 1 to value = 2
Fake Codex calls: 1; remote pushes: 0; all state is disposable.
```

The example creates a local Git source, delegates through the service command handler, executes one synchronous
worker cycle with `CodexFalso`, checks the delivered patch and reads the result. Review is the endpoint: it never
approves publishing. No root daemon, socket server, Codex/Claude process or chat client is started. Temporary
SQLite files, branches, patches and reports are removed when it exits. Running as root here does not imply that
running a real agent as root is safe; see [SECURITY](../SECURITY.md).

## Minutes 5–8: verify a genuinely new venv

```bash
sh scripts/clean-install-test.sh --empty
```

This copies the source, creates another venv and an empty HOME, discards inherited credentials/configuration
overrides, installs via `pip install .`, and verifies imports come from that venv. It also checks packaged rules
and `pip check`. The script calls:

```text
aao setup --non-interactive --output-dir <temporary-directory>/setup --repo-source <temporary-directory>/source
aao doctor --json
```

Those are descriptive paths from the script, not literal shell commands to paste. Non-interactive setup requires
an explicit new/empty directory owned by the current user and not writable by group/others. It refuses an existing
symlink or contents, and exclusively creates a 0600 `config.toml`, preserving files inserted after the initial check.
It uses channel `none`, restricted Codex permissions and an empty publish allowlist. It creates no secrets, user,
login, service or plugin installation. All configured paths stay below the chosen directory. To use that config
in another terminal, explicitly set `AAO_CONFIG` to its `config.toml`; the test removes its directory on exit.

Captured selected output, in command order (task progress omitted):

```text
doctor: expected missing prerequisites confirmed (no agents or daemons installed).
Created isolated config.toml (0600); channel=none, sandbox=workspace-write, publishing disabled.
No login, user, secret, service or plugin installation was performed. Run aao doctor with AAO_CONFIG.
doctor: expected missing prerequisites confirmed (no agents or daemons installed).
delegate: task #1
result: in review; demo.py changed from value = 1 to value = 2
Fake Codex calls: 1; remote pushes: 0; all state is disposable.
plugin: manifest, marketplace, skill frontmatter, commands and hooks valid.
Clean installation passed (fresh venv; installed package; no real credentials).
```

**Doctor's exit 1 is expected here.** Before setup it reports missing Codex, service user, repositories and daemon
sockets; after setup the synthetic repo/user exist, but Codex and both sockets remain absent. The script checks
the exact failure set and exit status. Any extra failure or unexpected success fails the test. A clean install
pass therefore means the package and offline workflow work; it does not mean a live installation is ready.

The plugin check validates local files and hook CLI arguments, not Claude Code's loader. The wheel installs Python
and bundled rules; install the plugin separately from the marketplace/checkout when you move to a real session.

## Minutes 8–10: choose the next step

- For a small review-only integration, read the [honest comparison](../README.md#honest-comparison--comparação-honesta)
  before choosing a persistent service.
- For real use, follow [Quickstart](../README.md#quickstart) on Linux/systemd, choose an unprivileged service user,
  and run interactive `aao setup`. Configure only disposable repos first. Run `aao doctor` and address its failures
  before enabling the service. Account authentication and actual plugin loading require your own session.
- For contributing, follow [CONTRIBUTING](../CONTRIBUTING.md). CI covers Linux/macOS × Python 3.11–3.14, empty
  Linux containers and built wheel/sdist installs; macOS tests do not certify a macOS service deployment.
