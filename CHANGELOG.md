# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- `aao mcp`: minimal MCP server on stdio (standard library only) with `delegate` (including `model`/`effort`), `status`, `result` and `review`. It never publishes; publishing stays a human approval in the CLI. Registration: `claude mcp add aao -- aao mcp`.
- `lite.transcripts_project` (or `AAO_LITE_TRANSCRIPTS_PROJECT`): names the project whose Claude Code transcripts the lite mirror reads, instead of relying on the directory where `aao serve` runs. Accepts a project path or the encoded folder name. `aao setup` asks for it and `aao doctor` reports a `transcripts project` check with the exact fix.

### Fixed
- `examples/offline_relay.py` builds its disposable source repository with `git add`/`git commit` instead of `git fast-import`, inherits `PATH` (the old minimal `PATH` hid git on Windows) and writes bytes as-is; the test now runs on Windows too.
- Typing: `file_lock`, `ProcessJob`, `which`, the IPC client, the usage window and the mirror export now type-check without `# type: ignore`.

### Changed
- WhatsApp is now clearly an optional, unofficial extra with ban risk: out of the first screen of both READMEs, `pip install '...[whatsapp]'`, the library is imported lazily, the wizard says it is an unofficial extra and checks the library, and `aao doctor` warns when the library is installed but unused. Existing WhatsApp setups keep working.
- mypy now runs with `check_untyped_defs` over the lite runtime, IPC, compat, usage measurement, config, mirror and doctor, plus a `--platform win32` pass over the portable modules in CI.

## [1.3.0] - 2026-11-12

### Added
- `release.yml`: tag, build, GitHub Release and PyPI Trusted Publishing (off until `PUBLISH_TO_PYPI=true` and the PyPI project exists).
- `publish.push_url_scheme = "ssh"` uses `git@github.com:owner/repo.git` in lite; credential failures say what to configure.
- `aao delegate --model`, `--effort` (this task only) and `--wait` (waits and prints the short result; exit 0 on review/completed).

### Changed
- New `codex.default_effort` (default `medium`, validated against the model's levels). Precedence: task `--effort` > runtime state > `codex.default_effort` > Codex default.
- Tests: the zone-dependent tests (`Cartao`, snapshot day rollover) pin their time zone and `test_retomada` restores it, removing order-dependent failures in the full suite.
- Spend policy (skill `spend-policy.md`, SKILL.md and both READMEs): Codex `medium` by default, one Codex task at a time, new short-briefing subagents instead of forking, at most 2 parallel on non-overlapping files; `balance.alert_high_pct` now defaults to 70 (was 85).
- README: the lite Quickstart and the one-command install (`pipx install git+https://github.com/JonasaAugusto/ai-agents-orchestrator`) are on the first screen; the comparison and the support policy moved to `docs/comparison.md` and `docs/support-policy.md` (with pt-BR copies).
- The "Windows is not tested" text now matches the OS support table, the CI and 1.2.1; Python classifier is Beta.
- `docs/first-10-minutes.md` recaptured for 1.2.1 with the lite path and a PowerShell note; broken `#quickstart` anchors fixed; `aao delegate` example now creates a real task; config example mode comment aligned with `aao setup`.
- WhatsApp is flagged as optional and unofficial on the first screen; Portuguese CLI names documented as compatibility aliases; pt-BR README lists the English-only sections.
- The wizard asks before installing the Claude Code plugin and reinstalls only when the package version changes.
- Lite probes Claude every 60 minutes by default (`claude.probe_minutes` still overrides it).
- CI actions in the lite job are pinned by SHA; Dependabot keeps GitHub Actions current.

### Fixed
- `aao setup --non-interactive` works on native Windows (no `pwd`, `geteuid`, `O_DIRECTORY` or `O_NOFOLLOW`); CI now runs it on Windows.
- Lite transcript mirror without a hook session reads only the current project's folder, never the whole `~/.claude/projects` tree.
- Local TCP transport checks the token in the first bytes (the CLI sends it first in the JSON) before reading the body, so an unauthenticated local process cannot hold the connection slots.
- Lite mirror fetches only the 20 most recent task workspaces instead of every workspace ever created.
- `aao doctor` in lite reports a stopped service as a warning ("run aao serve") instead of a failure, and its repository fix no longer mentions the root daemon.

## [1.2.1] - 2026-11-12

### Native Windows lite mode

- User runtime on Windows with Python 3.11–3.14, LocalAppData paths, UTF-8 and
  authenticated IPv4 loopback IPC. The per-process random token is stored with a
  protected current-user-only DACL; no root, systemd, Unix socket or symlink privilege.
- Portable file locks, CLI/npm/PowerShell shim detection, Windows process groups and
  process-tree cancellation. Account usage reads stdio through a bounded reader on
  Windows, where selectors cannot monitor pipes.
- Isolated Git storage on Windows avoids symlinks and ignores repository executable
  configuration; controlled Git operations use longpaths and deterministic LF checkout.
- `aao service install --windows` registers a least-privilege current-user logon task;
  `uninstall` removes it and `aao stop` gracefully stops the lite process. No password, admin elevation or system service.
- Shell-free Claude hooks and PowerShell command permissions; bilingual OS support
  table and native/WSL2 instructions. Windows CI covers 3.11–3.14, clean installation
  and native API regressions. Platform CI must pass before release.

## [1.2.0] - 2026-11-12

### Lite installation and runtime

- Add top-level `mode = "lite"`, selected by setup, with a single foreground user
  service, local backend and user/XDG paths. No root daemon, systemd, `/proc` or inotify
  dependency in lite; Unix peer authentication supports Linux and macOS.
- Preserve delegation, results, read-only cross-review, briefing, hooks and persistent
  usage-limit scheduling. Local task branches are imported into mirrors for cross-review.
- Interactive lite `aao publish <id>` binds confirmation to the full SHA and destination,
  merges against the current remote base and pushes without force. The service backend
  refuses base publication. No lite remote channel or deployment runner.
- Idempotent guided setup detects both official CLIs, checks login without reading
  credential files, installs the Claude plugin and preserves existing config/rules.
  Document `pipx install . && aao setup` and `uv tool install . && aao setup` in EN/PT-BR.
- Add Linux/macOS lite CI with Python 3.11/3.12; macOS CI is a release gate. Full-mode
  privileged behavior remains available, with its separate root service and channel.

### Added
- Linux/macOS × Python 3.11–3.14 CI, scoped Ruff/mypy checks, secret scanning, package builds and wheel/sdist install checks.
- Fresh-venv installation check (`scripts/clean-install-test.sh`) and a runnable fake-engine relay example, using only
  disposable state. Static plugin validation covers manifests, marketplace sources, skill metadata and hook arguments.
- `aao setup --non-interactive --output-dir <new-or-empty-dir> [--repo-source <checkout>]`: isolated local evaluation
  configuration, no prompts or credentials, no channel or publishing, restricted permissions; existing contents are refused.
- Source-linked honest comparisons, release/support policy, captured first-ten-minutes walkthrough and conduct policy.
- Codex permission modes documented and configurable: `restricted` (default) or full access, the same permissions as
  Claude. New key `codex.full_access_read_only` extends `codex.full_access` to read-only phases (reviews, research),
  and `aao setup` asks which mode to use and who orchestrates (`claude` or `codex`). `aao doctor` shows both.
- README (English and Portuguese): permission modes, who orchestrates, a first-task tutorial and the design decisions
  with their reasons. The "Codex has no network" premise is replaced by the two modes and their risks.

### Reliability and context economy

- Wait for task completion on a single socket request; notify waiters on state changes and service shutdown.
- Deliver bounded unread-note deltas, acknowledging only complete entries on first delivery; retain overflow and
  new results for later hooks. Keep generated notes bounded and remove repeated command boilerplate.
- Resume saved engine sessions with delta prompts and live workspace base rules. Preserve cross-review threads
  across the existing single automatic timeout/restart retry budget.
- Check service/root-daemon sockets, the live briefing and installed current plugin hooks in doctor. Add
  `doctor --fix` for missing directories and bundled example rules, preserving existing files and service state.
- Follow file metadata every 2.5 seconds in watch, rereading SQLite only on event/checkpoint changes. Persist task
  state events so early failures and cancellations also finish followers.

## [1.1.0] - 2026-11-12

### Usage measurement and balance

- Persist input/output tokens for Codex completed turns and Claude headless runs through an additive, idempotent
  SQLite migration. Status exposes recent execution counters and rolling 5-hour/7-day totals.
- Add lightweight `aao claude-usage` and plugin Stop hook: hash API-message ids and import only counters from the
  mirrored session tail, deduplicating streamed blocks and repeated hooks; accounting stores no conversation text.
- Default `aao result` to `--short` (state, branch, stored diffstat, warnings and ten report lines); `--full` preserves
  the complete record. `aao status --brief` prints five formatted lines including both usage windows and ratios.
- Configurable `[balance]` quotas, target ratio, task-cap reduction, cached Codex account limits and asymmetric
  channel alerts. Economic prompt guidance delegates review/test verification and routes auto development/review
  to Codex. Low ratios allow direct Claude review with a reduced task cap; unknown quotas never invent percentages.
- Doctor checks measurement configuration, quotas, schema, mirror and Stop hook. Document the token estimate's
  coverage limitations and `tasks_per_hour`/`handoff_checks_per_day` as independent cost controls.
- Bound the Codex account reader when a subprocess sends an incomplete line; no provider calls in hooks/status.

### Added

- Persistent automatic usage-limit resumption, first-reset scheduling when both engines are limited, runtime
  checkpoints with separate engine sessions, priority queueing and headless Claude development fallback (opt-in,
  capped at half of persisted task attempts). Codex can keep coordination without an interactive Claude session.
- `aao status --brief` displays reset times; `aao doctor` checks the persisted schedule. The independent timer and
  periodic Claude availability probes recover after service/machine restarts, including interrupted context checks.

### Changed

- Skill v1.1 part B: Codex owns suite execution and red/green/mutation evidence, reports in at most 20 lines;
  Claude requests Codex cross-review before reading reports/source and opens only risk files. Fixed delegation
  briefings are at most 15 lines, with stable repository context in `AGENTS.<repo>.md` and no pasted code/diffs.
  Routing defaults to Codex for tasks that fit; status uses `--brief` and hook notes take precedence over waiting.
- CLI `list --state review` (also `running` and English labels) maps to internal states and returns English `estado`
  labels. Portuguese filters remain accepted; database/socket states are unchanged. CLI list consumers must adapt.
- Queued task timeouts retry once with the saved session and “finish and report”, sharing the existing restart retry
  budget. Default timeout is 3600 seconds; `FALLBACK_TASK_TIMEOUT` is accepted as a legacy environment alias.

### Documentation

- English/pt-BR cost and balancing guides and delegation-to-publication examples; five skill scenarios for partial
  results, publication conflicts, For Claude requests and each engine's limit.
- Quickstart now clones the repository first, so `rules/`, `examples/` and `templates/` exist for the copy steps;
  installs from the clone (`pip install .`, `pip install '.[whatsapp]'`).
- The root daemon is documented as required to run tasks (the service refreshes repository mirrors through it and
  blocks a task when it is unreachable); the FAQ claimed it was optional.

## [1.0.2] - 2026-11-12

### Security
- Privileged mirror files and exported repositories now live in a root-controlled sibling of the service state
  directory (`<state_dir>-root-shared`). Audit and lock files use its private subdirectory. Directory descriptors,
  no-follow opens and exclusive temporary files protect writes; untrusted parents are refused.
- Workspace Git uses a private configuration authored by the service, including for local clone sources; mutable
  repository config is never loaded for delivery operations. The old config check remains a tampering diagnostic.
- Publish approvals bind the reviewed commit SHA, repository, branch, task and destination. Changed branches or
  destinations require a new approval; legacy buttons and signatures must be recreated after upgrading.
- Git probes in `aao doctor` run in a neutral directory with controlled configuration.
- WhatsApp approvals require the message's exact action code (for example `a1b2c3d4:1`). Bare numbers do not approve
  actions. A new send invalidates older options; concurrent sends and answer resolution are synchronized.
- Claude hooks carry only task identifiers and fixed states. Agent-authored content is consulted explicitly as
  untrusted data. Structured event masking also redacts values by sensitive field name, regardless of value format.

### Upgrade
- Refresh mirrors with the upgraded root daemon before resuming tasks. It creates the protected sibling directories;
  existing shared mirrors are not reused. The parent of `state_dir` must be controlled by root. Review custom path
  references, filesystem permissions and systemd write restrictions before restarting (operator action).
## [1.0.1] - 2026-11-11

### Security
- Root no longer runs git with `safe.directory=*` in service-owned repositories, where repo config/hooks
  (`core.fsmonitor`, `core.hooksPath`, `credential.helper=!cmd`, `core.gitProxy`, `url.*.insteadOf`) could execute
  code as root. New hardened git helper (no system/global config, exec-capable keys overridden, local transport
  only); mirrors are fetched into root-only bare clones (`<root_state_dir>/mirror`) and copied out to the service;
  `aao watch` and `aao install` read other users' checkouts as their owner.
- The service marks itself non-dumpable (`PR_SET_DUMPABLE=0`) and drops `AAO_TELEGRAM_TOKEN` / `AAO_PUBLISH_KEY`
  from its environment at start, so same-uid engines cannot read them from `/proc/<pid>/environ`. Example systemd
  units gain extra compatible hardening; running engines under a separate uid is documented as recommended.
- Workspace git outside the sandbox always disables hooks, fsmonitor, credential helpers and proxies, and refuses to
  deliver when the workspace `.git/config` holds keys outside an allowlist (e.g. a planted filter driver).
- `aao publish <id>` requires the owner to type the task id in an interactive terminal; the self-attested
  `--approved-by-owner` flag alone no longer publishes (kept only as a deprecated, ignored option).
- The experimental wake-up sends a fixed notice built by the root daemon ("Task #N finished with status X. Run
  `aao result N`."); agent-authored text is never posted into the Claude session, and free text sent to the
  daemon's socket is rejected.
- `aao setup` writes `config.toml` as 0640 root:<service group> (0600 when the group does not exist) instead of 0644.

## [1.0.0] - 2026-11-11

First public release.

### Added
- Service (`aao serve`) that runs OpenAI Codex CLI as a second worker for Claude Code, using your own subscriptions
  (no API keys): shared task board (SQLite), serial Codex queue with per-hour limits and file-overlap locks.
- Honest task states: the final report's first line (`Status: completed | partial | blocked`) decides the state; a
  process that exits 0 with a blocked report is blocked, partial work never gets a publish button.
- Cross-review of branches by Codex (`aao review`), delegation from Claude Code (`aao delegate`, `aao wait`).
- Automatic handoff when Claude Code hits its usage limit (StopFailure hook), with Codex checking a living
  briefing against a masked tail of the transcript; notes back to Claude through UserPromptSubmit/SessionStart hooks.
- Execution snapshot injected into every prompt (time, base SHA per repo, real sandbox/network/model/effort,
  briefing age) and mandatory mirror sync before work starts.
- Delivery gate: stray note files kept out of commits, large document deletions flagged, time-dependent tests flagged,
  secret scan before chat commits.
- Optional root daemon with fixed operations: mirror refresh, guarded pushes of `agent/*` branches to an explicit
  GitHub allowlist, HMAC-signed single-use merges into the base branch, exact-allowlist deploy commands.
- Chat channel interface with Telegram (buttons, HMAC-signed single-use callbacks, single allowed user) and an
  UNOFFICIAL, opt-in WhatsApp channel (optional `neonize` extra, numbered replies, rate limit, backoff).
  Running without any chat channel is supported (CLI + Claude Code only).
- `aao setup` (interactive wizard writing the config and a 0600 root-only secrets file) and `aao doctor`
  (checks with fix instructions; Codex login is checked only with the official `codex login status`).
- Live view of Codex: masked per-task event log, `aao watch [id]`, `aao watch-open` (tmux split / terminal window).
- Claude Code plugin (`claude-plugin/`): orchestrator-workflow skill, handoff hooks, `/delegate`,
  `/orchestrator-status`, `/review-task`, `/codex`; experimental `/codex-pane` live side pane (`claude-plugin-pane/`).
- Secret and personal-data masking for everything the model reads from Claude's transcript and memory.
- Single TOML configuration with documented example; every key overridable by `AAO_*` environment variables.
- Rules linter (`aao check-rules`), example base rules, division of work and per-repo AGENTS template.

### Security defaults
- Codex runs as an unprivileged user with its `workspace-write` sandbox and no network; `danger-full-access` only via
  the explicit `codex.full_access = true` opt-in.
- Publishing always needs a human approval; nothing is pushed unless the repository is in `publish.push_allowlist`.
- The Claude Code session wake-up (undocumented local socket format) is experimental and off by default.
