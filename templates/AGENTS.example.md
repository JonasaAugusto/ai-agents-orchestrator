# Instructions for the agent in this repository (example)

Copy this file to `<templates_dir>/AGENTS.<repo-name>.md`. The service copies it to `AGENTS.md` at the root of
each workspace of that repo (kept out of commits) unless the repo already versions its own AGENTS.md.
Keep it short (max 40 lines): it is read on every run.

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
