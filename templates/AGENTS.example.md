# Instructions for the agent in this repository (example)

Copy this file to `<templates_dir>/AGENTS.<repo-name>.md`. The service copies it to `AGENTS.md` at the root of
each workspace of that repo (kept out of commits) unless the repo already versions its own AGENTS.md.
Keep stable repository context here (max 40 lines): stack, setup, tests, conventions and boundaries, read on every run.
Task briefings refer here instead of repeating context. Use the fixed <=15-line template in the orchestrator skill:
Objective (one sentence), Files, Tests (exact command), Done, Constraints, Report. Cite paths/symbols, never paste code/diffs.

## Stack
- Language/framework: e.g. Python 3.12 + FastAPI.
- Package manager: e.g. `python -m venv .venv && .venv/bin/pip install -r requirements.txt`.

## Tests
- Full suite: e.g. `.venv/bin/python -m pytest -q`.
- Lint/type check: e.g. `.venv/bin/ruff check .`.
- Tests needing a database must use a disposable one (never a shared or production database).

## Conventions
- Code style, naming, folder layout and anything a reviewer would ask for.
- Files or directories the agent must never touch (generated code, vendored deps, migrations...).

## Done means
- Suite green, new tests proven to fail without the change, docs updated when behavior changes.
- Codex runs the full suite on disposable data and records revert/run/restore evidence for every new test.
- Break the key line, confirm a test failure, restore and rerun; concurrency changes must pass 20x without sleeps.
- Report <=20 lines: status, changes, test commands/counts, red/green, restored mutation, gaps, For Claude and risks.
- Claude requests Codex cross-review first, then reads the short reports and opens only risk files before human approval.
