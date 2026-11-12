# ai-agents-orchestrator

[![CI](https://github.com/JonasaAugusto/ai-agents-orchestrator/actions/workflows/ci.yml/badge.svg)](https://github.com/JonasaAugusto/ai-agents-orchestrator/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)

[Português (Brasil)](README.pt-BR.md)

**Run Claude Code and OpenAI Codex CLI as one relay team, on the subscriptions you already pay for.**

Claude Code leads: it plans, talks to you and reviews. Codex works as a second engineer on isolated branches:
it picks up delegated tasks, cross-reviews Claude's branches and, when Claude hits its usage limit, takes the baton
and keeps going until Claude is back. You stay in charge from the terminal or your phone: nothing is merged or
deployed without your explicit approval.

- **No API keys.** It drives the official `codex` and `claude` CLIs with your own logins.
- **Safe by default.** Codex runs unprivileged, in its sandbox, without network, in throwaway clones; publishing needs a human.
- **Honest results.** A task whose report says `partial` or `blocked` is never shown as done, whatever the exit code.
- **Python standard library only.** One optional extra (WhatsApp). Tested on Python 3.11 and 3.12.

## Contents

[How it works](#how-it-works) · [Features](#features) · [Security model](#security-model) · [Quickstart](#quickstart) ·
[Claude Code plugin](#claude-code-plugin) · [Watching Codex live](#watching-codex-live) · [Chat channels](#chat-channels) ·
[Telegram commands](#telegram-commands) · [CLI reference](#cli-reference) · [Configuration](#configuration) ·
[FAQ](#faq) · [Limitations](#limitations) · [Roadmap](#roadmap) · [Glossary](#glossary)

## How it works

```mermaid
flowchart LR
    U([You: terminal or phone]) -->|prompts, approvals| CH[Chat channel<br/>Telegram / WhatsApp / none]
    U -->|works with| CC[Claude Code<br/>lead]
    CC -->|aao delegate / review| SVC
    CC -.->|StopFailure rate_limit hook| SVC
    CH <--> SVC[aao service<br/>task board + Codex queue]
    SVC -->|prompt + execution snapshot| CX[Codex CLI<br/>sandboxed worker]
    CX -->|agent/ID branch, patch, report| SVC
    SVC -->|note via hook / optional wake-up| CC
    SVC -->|review, then Publish button<br/>or approved CLI publish| RD[root daemon<br/>allowlists + HMAC]
    RD -->|push agent/* or merge to main| GH[(GitHub)]
```

1. **Delegation.** Claude (or you, from the chat) queues a task with the files it touches. The service clones the repo
   from a freshly verified mirror into a throwaway workspace on a new `agent/<id>-<slug>` branch and runs Codex there.
2. **Execution snapshot.** Every prompt starts with facts generated in code: time, base SHA of each repo (checked
   against GitHub), the real sandbox, network, model and effort, and the age of the living briefing. The mirror must be
   provably current or the task is blocked before anything runs.
3. **Honest delivery.** When Codex ends, the service commits on the branch (never Codex), keeps stray notes out of the
   commit, flags large document deletions and time-dependent tests, writes `HANDOFF.md` and patches, and reads the
   first line of the report: `Status: completed | partial | blocked`.
4. **Review and publish.** Claude reviews the branch (see the [review protocol](#claude-code-plugin)). Publishing is a
   merge `--no-ff` into the base branch, only after a human approval: the owner's [Publish] button, or
   `aao publish <id>` for tasks Claude delegated, run by the owner as root in an interactive terminal (they type the
   task id to confirm; a flag alone never publishes). Production deploys are a second, separate button.
5. **Usage-limit handoff.** When Claude Code hits its limit, a hook calls `aao claude-limit`. Codex checks Claude's
   living briefing against a masked tail of the transcript, proposes tasks, and (with `/auto` on) continues on its own
   branches. When the limit resets, Claude gets a note (and, optionally, a wake-up message) and takes the baton back.

### Automatic resumption

Usage limits keep tasks pending in SQLite. Codex resumes at `codex_limite.ate` using its saved thread and workspace.
Claude's reset is read from the limit message; when no time is available, a headless probe runs every 15 minutes
(`claude.probe_minutes`), if the service has a Claude login. The reset and next probe survive service/machine restarts.
The optional interactive wake-up delivers a fixed notice; Claude reads the report in `HANDOFF-PARA-CLAUDE.md`.

If both engines are limited, the channel receives “Both at their limits; I resume at …”. An independent internal timer
checks the first reset, even while a worker is busy. Runnable tasks are selected by `--priority` (higher first), then
oldest id, respecting dependencies, file locks, pause and hourly caps. Resumption says “continue where you stopped and
report”. Thread/session ids are saved as soon as the engine announces them; runtime checkpoints and partial reports
stay in `resultados/<id>/HANDOFF.md`, outside the repository, beside the live briefing references.

Claude can run development tasks through `claude -p` while Codex is limited when **`claude.dev_fallback = true`**
and the service has its own Claude login. This is off by default. The fallback balance allows at most 50% Claude
attempts across persisted task runs (including limit failures); it never runs Codex cross-reviews or tasks assigned
to an interactive Claude. If credentials, balance or locks prevent a run, the queue waits for Codex's reset.
`service.orchestrator = "codex"` keeps pending handoff tasks with Codex when Claude returns, so coordination can
continue without an interactive Claude session. This setting is independent of the task engine.

A restart or timeout automatically retries a queued task **once**, sharing the retry budget, with the saved engine
session and workspace. Timeout resumption says “finish and report”; another interruption blocks the task for review.
The default task timeout is **3600 seconds**, configurable with `service.task_timeout`,
`AAO_SERVICE_TASK_TIMEOUT`, or the legacy `FALLBACK_TASK_TIMEOUT` (the AAO variable wins).
`aao status --brief` shows `retomada agendada: Claude HH:MM / Codex HH:MM` in the configured timezone (`-` means none);
`aao doctor` validates the durable reset records. The timer needs no cron job. Publishing still requires human approval.
Interrupted chat requests also wait for the reset and keep their session beyond the normal inactivity window;
cancelling a queued chat request prevents automatic resumption.

## Usage measurement and balance (1.1.0)

`aao status --brief` prints exactly five lines: 5-hour and 7-day usage/ratio, queue state, scheduled resets and the
effective hourly task cap. `aao status` also exposes token counters for the last ten executions and each rolling
window. `aao result <id>` now defaults to `--short`: state, branch, stored diffstat, warnings/errors and at most ten
report lines. `--full` retains the complete task record; `aao show <id>` remains full by default. Old results without
a stored diffstat display `-` and never run Git just to show a summary.

The plugin's lightweight `Stop` hook runs `aao claude-usage`. The root daemon refreshes only the selected session's
transcript tail, without repositories, memory or model calls. Accounting stores timestamps, hashed message ids and
input/output counters in the existing execution table; it does not store conversation content. Repeated Stop hooks,
overlapping tails and streamed blocks do not duplicate a message. Codex `turn.completed.usage`, Claude headless
results, chat turns, handoff checks and availability probes are counted as well. SQLite migration only adds columns
and indexes, preserving historical runs (whose unknown token counts start at zero).

Configure `[balance]` in [config.example.toml](examples/config.example.toml). Claude usage is total measured tokens
divided by an **operator-calibrated token quota** for each window, not an official Claude plan percentage. Claude
input includes uncached, cache-read and cache-creation tokens; Codex cached input is already part of input tokens.
These counters estimate quota consumption, not money or equal work across models. Zero quotas mean unknown (`?`).
The background timer reads Codex `account/rateLimits/read`, matching windows by duration (300/10080 minutes); fresh
account percentages take precedence, while local token totals remain visible. Stale samples, expired resets or an
unavailable account reader fall back to configured Codex token quotas. No provider request runs in the prompt hook
or status path. `aao doctor` checks quota settings, schema readiness, mirror availability and Stop-hook setup.

The target Claude/Codex usage ratio is **0.7–1.4**, configurable. Above 1.4 in either window, the prompt hook asks
Claude to delegate review/test verification to Codex, use `result --short` and avoid reading diffs; `auto` routes
development and cross-review to Codex. Below 0.7, Claude may review directly and `tasks_per_hour` is reduced by
`low_ratio_tasks_factor` (default 0.5, rounded down, minimum one when the original cap is positive). High Claude
usage in either window takes precedence over a low ratio in the other. Explicit engine selection, human publication
approval, file locks and hard usage limits remain in force; economic mode can make headless fallback wait for Codex.
Positive Claude usage with zero Codex usage is above target; both zero or missing data is unknown and leaves routing
and the hourly cap unchanged. `balance.enabled = false` disables guidance, automatic balancing and alerts, while
keeping accounting. A channel alert fires when one engine is above 85% and the other below 50%, once per imbalance
episode/window; thresholds are configurable. Without a channel there is no remote alert delivery.

`service.tasks_per_hour` and `service.handoff_checks_per_day` are independent cost levers: the first caps task
attempts, the second real Codex handoff checks. Lowering either saves calls but slows progress. Token coverage starts
with observed executions and mirrored sessions; only records present in the bounded recent transcript tail can be
imported. Separate subagent transcript files, missed sessions and old history are not reconstructed. Update the
plugin (or merge [claude-hooks.json](examples/claude-hooks.json)) and calibrate quotas before relying on the ratio.

## Features

| Area | What you get |
| --- | --- |
| Task board | SQLite board shared by Claude and Codex; owners, origins, dependencies, file-overlap locks, hourly limits |
| Delegation | `aao delegate`, `aao wait`, per-task briefings, `aao review` for cross-review by Codex |
| Honest states | `completed` / `partial` / `blocked` parsed from the report; blocked never becomes review, partial has no publish button |
| Handoff | StopFailure hook, Codex verdict as strict JSON, handoff notes for Claude, daily cap of checks |
| Living briefing | `BRIEFING.md` read first by Codex; stale/future/missing timestamps and diverging SHAs become warnings |
| Git hygiene | throwaway clones from a bare mirror, `agent/*` branches only, no force/tags/deletes, append-only audit log |
| Publishing | human approval always; root daemon with GitHub allowlist, HMAC-signed single-use merge requests, exact-allowlist deploys |
| Privacy | masking of secrets and personal data in everything the model reads from Claude's transcript and memory |
| Chat | Telegram (buttons, signed single-use callbacks), unofficial WhatsApp (opt-in), or none |
| Live view | masked per-task event log, `aao watch`, tmux/terminal opener, experimental Claude Code side pane |
| Operations | `aao setup`, `aao doctor`, graceful restarts that resume interrupted work once, chat history for Claude |

## Security model

| Default | Why |
| --- | --- |
| Codex runs as an unprivileged system user (`aao`) | it can only touch what that user can |
| `--sandbox workspace-write`, network **off** | writes only inside the throwaway clone; `codex.network_access = true` to allow outbound network |
| `danger-full-access` only with `codex.full_access = true` | explicit, warned opt-in; `aao doctor` flags it |
| Publishing requires a human | button with HMAC-signed, expiring, single-use callback, or root CLI where the owner types the task id at an interactive terminal |
| Nothing is pushed unless allowlisted | `publish.push_allowlist` is empty by default |
| Secrets stay out of the service user's reach | bot token and publish key come from a root-only 0600 `EnvironmentFile` |
| Engines get a minimal environment | no tokens or keys in Codex's environment; the service is non-dumpable and drops the secrets from its own environment at start |
| Root never runs repo config it does not own | hardened git (no hooks, fsmonitor, helpers, proxies); mirrors are fetched into root-only clones and copied out |
| One chat owner | every other chat, user or group is ignored without an answer |
| Root daemon is narrow | fixed operations, peer-uid check, refuses to start if its code is writable by non-root |
| Claude session wake-up is off | experimental, uses an undocumented local socket format; it only sends a fixed notice (task id + state), never agent text |
| Dev secrets are masked too | `privacy.mask_dev_secrets = true` by default |

Read [SECURITY.md](SECURITY.md) for the threat model and how to report vulnerabilities.

Version 1.0.2 stores root-generated mirrors in `<state_dir>-root-shared/mirror` and exported repositories in
`<state_dir>-root-shared/repos`. The sibling parent must be controlled by root. On upgrade, the operator restarts
both components, refreshes mirrors and recreates approval buttons; old approvals are invalid. Update custom path
references and filesystem write restrictions before restarting. See [SECURITY.md](SECURITY.md) for the boundaries.
Git delivery ignores mutable workspace config; publishing is pinned to the reviewed SHA, repository and destination.
Hook notices contain ids/states only; consulted reports are untrusted data.

## Quickstart

Linux with systemd, Python 3.11+, git, the [Codex CLI](https://github.com/openai/codex) and
[Claude Code](https://docs.anthropic.com/en/docs/claude-code) installed.

**1. Install**

```bash
git clone https://github.com/JonasaAugusto/ai-agents-orchestrator && cd ai-agents-orchestrator   # rules/, examples/ and templates/ used below
sudo useradd --system --home-dir /var/lib/ai-agents-orchestrator --create-home --shell /usr/sbin/nologin aao
sudo python3 -m venv /opt/ai-agents-orchestrator
sudo /opt/ai-agents-orchestrator/bin/pip install .   # from the clone; not on PyPI yet
sudo ln -s /opt/ai-agents-orchestrator/bin/aao /usr/local/bin/aao
# optional, unofficial WhatsApp channel:  sudo /opt/ai-agents-orchestrator/bin/pip install '.[whatsapp]'
```

**2. Configure with the wizard**

```bash
sudo aao setup
```

It asks for the service user, state directory, timezone, Claude Code transcripts folder, **the chat channel**
(Telegram / WhatsApp / none) and the repositories, then writes `/etc/ai-agents-orchestrator/config.toml` (0640 root:<service group>) and the
secrets file `/etc/ai-agents-orchestrator/env` plus `publish.key` (0600, root). For Telegram it tells you how to get
the token (talk to **@BotFather**, `/newbot`) and **your numeric user id** (send any message to **@userinfobot**).

> **Without a chat channel you cannot send prompts to Codex from your phone, receive its notifications, or approve
> publishing with a button. Telegram is the channel that enables this.** Everything else works through the CLI and
> Claude Code (delegate, status, review, publish with explicit CLI approval).

Copy and adapt the examples the config points to:

```bash
sudo cp rules/BASE_RULES.example.md /etc/ai-agents-orchestrator/BASE_RULES.md
sudo cp rules/DIVISION.example.md   /etc/ai-agents-orchestrator/DIVISION.md
sudo mkdir -p /etc/ai-agents-orchestrator/templates   # AGENTS.<repo>.md files, see templates/AGENTS.example.md
```

**3. Log Codex in, with the official command, as the service user**

```bash
sudo -u aao env HOME=/var/lib/ai-agents-orchestrator CODEX_HOME=/var/lib/ai-agents-orchestrator/.codex codex login --device-auth
```

`--device-auth` is for headless servers: open the URL it prints and type the code (it may need device-code login
enabled in your ChatGPT security settings); on a machine with a browser, plain `codex login` works. This project never
reads, copies or prints credential files; it checks the login only with `codex login status`. If the login expires, the
service pauses the queue and sends you this same command.

**4. Check everything**

```bash
sudo aao doctor
```

It checks the config, the service user, Codex installed and logged in, Claude Code and its hooks, the chat channel
(token, owner id, 0600 secrets file, WhatsApp opt-in), git access to every repository and the push allowlist, and
prints a fix for each problem. `aao doctor --json` for scripts.

**5. Start the services** (examples with placeholders in [`examples/systemd/`](examples/systemd))

```bash
sudo cp examples/systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ai-agents-orchestrator.service
sudo systemctl enable --now ai-agents-orchestrator-root.service   # optional: mirrors, pushes, deploys
```

**6. Connect Claude Code** with the [plugin](#claude-code-plugin) (or merge
[`examples/claude-hooks.json`](examples/claude-hooks.json) into `~/.claude/settings.json`), then opt in to the
automatic handoff, which sends a masked tail of your transcript to OpenAI:

```bash
aao arm        # aao disarm to turn it off
```

Try it without any account: `aao serve --dry-run` uses fake engines and no chat channel.

## Claude Code plugin

The repository is a Claude Code plugin marketplace. In a Claude Code terminal session:

```
/plugin install ai-agents-orchestrator --marketplace JonasaAugusto/ai-agents-orchestrator
```

From a shell: `claude plugin install ai-agents-orchestrator --marketplace JonasaAugusto/ai-agents-orchestrator`.
The `aao` CLI must be on PATH. It installs:

- **Skill `orchestrator-workflow`**: when to delegate and when to do it yourself; write task descriptions to a file and
  pass `--briefing <file>` or `"$(cat file)"` (inline text with backticks gets executed by the shell); wait for the
  note or wake-up instead of polling; the **review protocol** (read the diff, prove each new test fails without the
  patch, run the full suite yourself against disposable data, never trust the agent's own report alone); partial and
  blocked are not done; publish only after review and human approval (production needs its own approval); keep the
  living briefing at most 40 lines and update it at milestones; no secrets in task text; serialize tasks that touch the
  same files from the same base.
- **Hooks**: `StopFailure` (`rate_limit`) runs `aao claude-limit`; `UserPromptSubmit` and `SessionStart` run
  `aao hook-prompt`, which delivers pending notes from Codex and a reminder when the briefing is stale.
- **Slash commands**: `/delegate <repo> <task>`, `/orchestrator-status`, `/review-task <id>`, `/codex [id]`.

An **experimental** companion, `aao-codex-pane`, adds `/codex-pane [id]`: a live side pane inside Claude Code. It is
built on Claude Code's early-access function-hooks API, which may change between releases:

```
/plugin install aao-codex-pane --marketplace JonasaAugusto/ai-agents-orchestrator
```

## Watching Codex live

The service appends every Codex event of a task, masked, to `<state_dir>/resultados/<id>/events.jsonl`. Watching only
reads that file: it never interferes with the task, and you can replay a finished task later.

- `aao watch [id]`: header (task, repo, branch, workspace), the events so far, then follows until the task ends,
  finishing with the diff so far. Default id: the running task. `--no-follow` prints and exits.
- `aao watch-open [id]`: opens `aao watch` in a **tmux split** when you are inside tmux, else in a **new terminal
  window** (x-terminal-emulator, gnome-terminal, konsole, kitty, alacritty, wezterm, xterm...), else prints the command
  to run in another terminal.
- In Claude Code: `/codex [id]`, `/codex-pane [id]` (experimental pane), or just ask in plain language,
  *"open codex"*, *"show me what codex is doing"*, *"abre o codex"*: the skill tells Claude to open the view.

Example output (illustrative):

```
== Codex on task #12: Validate signup e-mails [in progress]
   repo: web-app  branch: agent/12-validate-signup-e-mails
   workspace: /var/lib/ai-agents-orchestrator/workspaces/web-app/12
[14:02:11] session started
[14:02:15] $ python -m pytest -q tests/test_signup.py
[14:02:19]   -> exit 1: 1 failed, 4 passed
[14:03:02] edit: update app/signup.py, add tests/test_signup_email.py
[14:03:40] $ python -m pytest -q
[14:03:47]   -> exit 0: 31 passed
[14:03:55] codex: Status: completed ...
== diff so far:
 app/signup.py                 | 14 ++++++++++++--
 tests/test_signup_email.py    | 38 ++++++++++++++++++++++++++++++++++++++
== task #12 is in review
```

## Chat channels

`service.channel = "telegram" | "whatsapp" | "none"`. The bot logic is channel-agnostic (`canal.py`: send text,
send with buttons, receive messages and button presses, allowed-user check), so other channels can be added.

**Telegram** (recommended): free text goes to Codex (read-only, or editing on an `agent/chat-<date>-<n>` branch when
a repository is known), every state change asks for a button, buttons are HMAC-signed, expire after 10 minutes and
work once per group. Only `telegram.chat_id`, in a private chat, is answered.

**WhatsApp (unofficial, at your own risk).** A full replacement for Telegram: free-text prompts to Codex, task
notifications, approvals and commands. Buttons show an exact action code such as `a1b2c3d4:1`; bare numbers
do not approve actions. Each new message invalidates older options, and a code is accepted only once.

> ⚠️ **WARNING:** this channel uses an **unofficial** WhatsApp Web client (the optional `neonize` package). It is **not
> affiliated with, endorsed by or supported by WhatsApp or Meta**. It **may violate WhatsApp's Terms of Service** and
> **the number can be restricted or banned**. Use a **secondary number**, **at your own risk**; this project gives
> **no guarantee**. It only runs with `whatsapp.accept_unofficial_risk = true`, and `aao setup` asks you to type
> `I ACCEPT THE RISK`.

Safety measures for the number: it talks only to `whatsapp.owner_number` and ignores everyone else and groups; the
destination is fixed, so it cannot message anyone else; no bulk sending (rate limit, default 12 messages per minute
plus a minimum gap); reconnects with exponential backoff (5 s up to 15 min) and stops for good on logout or a temporary
ban; session files live in `<state_dir>/whatsapp` (0700/0600), outside any repository and ignored by `.gitignore`.
Pairing happens in `aao setup`, with a pairing code or a QR code in the terminal.

**None**: CLI and Claude Code only (see the notice in the Quickstart).

## Telegram commands

Commands are registered in the bot menu. Portuguese aliases from the project's origins also work
(`/tarefa`, `/quadro`, `/pausar`...).

| Command | What it does |
| --- | --- |
| *free text* | conversation with Codex; edits code on an `agent/chat-*` branch when the repo is known |
| `/task [repo] <text>` | formal task for Codex (confirm / edit / cancel) |
| `/board` | open tasks with View / Assign to Codex / Assign to Claude / Cancel |
| `/status` | engine, model, effort, queue, Claude state, briefing age |
| `/usage` | plan usage windows reported by Codex, plus local counters |
| `/model`, `/effort [level]` | choose Codex model and reasoning effort |
| `/engine` | codex, claude (headless) or auto |
| `/repos`, `/repo <name>` | allowed repositories / default repository |
| `/context` | Codex checks the briefing and the transcript tail now |
| `/pause`, `/resume`, `/cancel` | control the queue |
| `/last`, `/diff`, `/branches` | latest result, diff stat, agent branches |
| `/new` | end the chat session; the next message starts a new branch |
| `/auto` | automatic continuation on Claude's limit on/off |
| `/cap [n]` | daily cap of Codex limit checks |
| `/limit` | latest evidence of a Claude usage limit |
| `/logs`, `/history [N]` | recent log lines (masked) and chat history |
| `/help` | this list |

## CLI reference

Every command is `aao <command>` (also installed as `ai-agents-orchestrator`). Portuguese aliases are accepted.

| Command | Purpose |
| --- | --- |
| `setup`, `doctor [--json]` | configure; check and get fix instructions |
| `serve [--dry-run] [--no-chat]` | run the service |
| `root-daemon` | run the optional privileged daemon |
| `delegate --repo R --title T [--files a,b] [--briefing FILE \| --briefing-text TXT] [--description D] [--priority N]` | queue a task for Codex |
| `list [--state S] [--owner claude\|codex]`, `show ID`, `result ID [--short\|--full]`, `cancel ID`, `claim ID [--owner] [--files]` | board; result defaults to short |
| `wait ID [--timeout S]` | block until a final state (exit 0 review/completed, 1 blocked/cancelled, 2 timeout) |
| `review --repo R --branch B` | cross-review by Codex (read-only) |
| `publish ID` | root only, interactive terminal: the owner types the task id to merge + push a reviewed task delegated by Claude |
| `watch [ID] [--no-follow]`, `watch-open [ID]` | live view |
| `status [--brief]`, `pause`, `resume` | service state, durable resume schedule and queue |
| `arm`, `disarm` | automatic handoff on Claude's usage limit |
| `claude-limit`, `claude-usage`, `hook-prompt [--event E]`, `claude-resumed`, `note` | Claude Code hook glue, token counters and notes |
| `briefing-status`, `briefing-check` | living briefing |
| `history [--last N] [--since YYYY-MM-DD] [--search TEXT]` | chat channel history |
| `mirror [--no-repos]` | refresh the mirror now |
| `check-rules [--file F]` | lint the base rules (expired review date only warns) |
| `install [--source DIR] [--dest DIR]` | root-owned install from a reviewed git checkout (self-editing setups) |

## Configuration

One TOML file, documented in [`examples/config.example.toml`](examples/config.example.toml). Every key can be
overridden by `AAO_<SECTION>_<KEY>` (e.g. `AAO_SERVICE_TASKS_PER_HOUR=3`); secrets come from the environment
(`AAO_TELEGRAM_TOKEN`, `AAO_PUBLISH_KEY`). Repositories are `[repos.<name>]` tables (or a JSON file, see
[`examples/repos.example.json`](examples/repos.example.json)) with `github`, `base_branch`, an optional local `source`
and `local = true` for repositories without a remote. Deploy commands live in a root-only allowlist,
[`examples/publish.example.json`](examples/publish.example.json).

## FAQ

**Does it need API keys?** No. It runs the `codex` and `claude` CLIs with the logins you already have; usage counts
against those plans.

**Is it allowed by the providers' terms?** It only automates the official CLIs on your own machine for your own work.
Check your plan's terms for automation and concurrency yourself; the WhatsApp channel is a separate, explicit risk.

**What does OpenAI receive?** The task prompts, the repositories Codex works on and, if you run `aao arm`, a masked tail
of your Claude Code transcript and memory files. Masking is pattern based.

**Can Codex push to main?** No. The service commits; only the root daemon pushes, only `agent/*` branches of
allowlisted repos, and merges into the base branch need your signed approval.

**Do I need the root daemon?** Yes, to run tasks. The service refreshes the repository mirrors through it before
each task and blocks the task if the daemon is unreachable. It is also the only component holding GitHub
credentials, so it is the one that pushes and publishes.

**Why are some identifiers in Portuguese?** The project started as an internal tool in Brazil. User-facing text is
English; see the [glossary](#glossary).

## Limitations

- Linux and systemd are assumed for the service, the sandbox and the examples; macOS is untested.
- Codex's usage-limit message format is not documented; detection is tolerant but may miss new wording.
- Initial interactive limit detection needs the StopFailure hook or `/context`; once recorded, reset scheduling and
  queued development continue without an open Claude session. Unknown reset times require a headless Claude login
  for probes; without it, Codex continues and Claude's availability awaits new evidence.
- Secret masking is regex based; secrets in unusual formats can slip through.
- The experimental Claude Code wake-up and the `/codex-pane` panel depend on undocumented or early-access interfaces.
- The WhatsApp channel was tested with fakes only, not against a live WhatsApp account; pairing and reconnection
  depend on the third-party library.
- One owner per installation; no multi-user roles.

## Roadmap

- Official **WhatsApp Business Cloud API** channel (Meta), in Python, behind the same channel interface. It needs a
  public HTTPS webhook and a Meta business app; limitations to document: per-conversation pricing and the 24-hour
  customer-service window for free-form replies.
- More channels (Slack, Matrix, e-mail) through the channel interface.
- English internal identifiers, with migration of existing databases.
- Container image and non-systemd service examples.
- Richer `aao watch` filters and a web dashboard.

## Glossary

Internal names you may see in JSON output and the database: `tarefa` task, `quadro` board, `dono` owner
(`claude`/`codex`), `origem` origin (`claude` delegated by Claude, `dono` from the owner's chat, `handoff` taken over on
Claude's limit), `estado` state: `pendente` pending, `em_andamento` in progress, `em_revisao` in review, `concluida`
completed, `bloqueada` blocked, `cancelada` cancelled. `espelho` mirror, `recado` note, `resumo` summary, `aviso` warning.

## License

[MIT](LICENSE) © 2026 Jonas Augusto. Not affiliated with Anthropic, OpenAI, Telegram, WhatsApp or Meta. Claude, Codex,
Telegram and WhatsApp are trademarks of their respective owners.
