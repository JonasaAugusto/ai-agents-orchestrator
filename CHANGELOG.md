# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

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

- Queued task timeouts retry once with the saved session and “finish and report”, sharing the existing restart retry
  budget. Default timeout is 3600 seconds; `FALLBACK_TASK_TIMEOUT` is accepted as a legacy environment alias.

### Documentation

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
