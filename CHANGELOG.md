# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

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
