# Version and support policy

- Releases follow **SemVer**. Within 1.x, documented CLI commands/configuration and the task/report/approval contract
  are the compatibility surface. Python internals, SQLite tables and local socket payloads are not a public API.
  Security fixes can reject previously accepted unsafe operations; read the changelog before upgrading.
- Only the **latest release** receives fixes, including security fixes; there is no LTS branch or response-time SLA.
  A badge links to published releases; the checkout can include unreleased changes. This policy does not certify an
  independent audit or production suitability for every environment.
- Python **3.11–3.14** is the CI target: the full suite on **Linux**, and the lite mode (no root, no systemd) on
  **Linux, macOS and native Windows**. The operational service/root daemon and example units target **Linux with
  systemd**; a macOS or Windows test pass does not certify the privileged deployment there. WSL2 runs the Linux lite
  instructions and is not separately certified. External CLI compatibility also
  depends on their installed versions; report those versions with bugs.
- Delegation, review results, restricted defaults and owner-approved publishing are the supported core workflows.
  `/codex-pane` and interactive Claude wake-up remain **experimental**, off by default, and can change without
  compatibility guarantees. WhatsApp remains optional/unofficial with its documented risks. Usage balancing requires
  calibration and treats unknown quotas as unknown.

## Reproduce the installation check

```bash
sh scripts/clean-install-test.sh --empty
```

This creates a temporary venv, runs `pip install .` from a source copy, checks the installed import and `pip check`,
runs `aao setup --non-interactive --output-dir ... --repo-source ...`, runs `aao doctor`, executes a disposable local
task with fake Codex, and validates plugin manifests, skill frontmatter and hook CLI arguments. Network is needed
only to obtain Python build tools. It never starts a real service or logs into an agent.
Doctor **must fail** when required agents/daemons are absent: the test checks the exact expected failures and exit
status, and rejects additional failures. Passing this check does not mean a live installation is ready.
Unattended setup refuses directories owned by another user or writable by group/others and creates configuration
exclusively, without replacing existing files.
Plugin validation here is static; actually loading the plugin in an authenticated Claude Code session is a separate
manual integration check. The plugin is installed from GitHub/the checkout; the Python wheel does not install it.
CI also builds a wheel/sdist and exercises both with `--distribution`, and runs the source check in empty Python
slim containers. See [CONTRIBUTING](../CONTRIBUTING.md) for the scope of lint/type checks.


Back to the [README](../README.md#quickstart-lite-mode-121) · [honest comparison](comparison.md).
