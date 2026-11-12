# Runnable examples

Install the package in a venv, then run from the checkout:

```bash
python examples/offline_relay.py
```

This uses the installed service, board, worker and delivery code with `CodexFalso` and `RootFalso`. It imports one
synthetic Git commit into a temporary local source, delegates a change, executes one worker cycle, checks the result
is in review, verifies the patch and asserts no push was requested. All Git/SQLite/report files are deleted on exit.
No account, API key, chat channel, system service or network access is needed. No timing/polling is involved.

For a fresh-venv installation check, run `sh scripts/clean-install-test.sh --empty`; see
[First 10 minutes](../docs/first-10-minutes.md). Build-tool downloads need network.

Other files here are **operator templates, not ready-to-run production configuration**:

- `config.example.toml` and `repos.example.json`: replace repository/path placeholders and keep restrictive defaults.
- `claude-hooks.json`: merge into Claude Code settings only if you are not installing the plugin; avoid duplicate hooks.
- `publish.example.json` and `systemd/`: review with the operator before installation; they do not grant publication
  approval and are not exercised against a real daemon by the offline demo.
