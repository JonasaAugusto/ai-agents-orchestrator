# Contributing

Thanks for helping. This project runs AI agents next to real repositories, so changes are reviewed with security in
mind first.

## Development setup

```bash
git clone https://github.com/JonasaAugusto/ai-agents-orchestrator
cd ai-agents-orchestrator
python -m venv .venv && . .venv/bin/activate
pip install -e .
python -W ignore -m unittest discover -s tests -t .
```

The project is Python only and has no runtime dependencies (the WhatsApp extra is optional). The test suite needs
`git` on PATH and never calls OpenAI, Anthropic, Telegram or WhatsApp: engines, chat channels and the root daemon are
replaced by doubles in `ai_agents_orchestrator.testing` and in the tests.

## Rules for changes

- Every behavior change comes with tests; a new test must fail without the change and pass with it.
- No test may depend on sleeping or wall-clock time: inject a clock (`relogio=`), use events or fakes.
- Keep the safe defaults. Anything that widens what an agent can do (sandbox, network, pushes, deploys, channels)
  must be an explicit, documented opt-in and covered by `aao doctor`.
- Never commit secrets, tokens, chat ids, phone numbers or personal data, not even in fixtures. Use obviously fake values.
- Update `README.md`, `README.pt-BR.md` and `CHANGELOG.md` when user-facing behavior changes.
- Internal identifiers are in Portuguese for historical reasons; new user-facing text (CLI, chat messages, prompts,
  docs) is in English.

## Commit and PR

Small, focused pull requests with a clear description and the test command you ran. CI runs the suite on Python 3.11
and 3.12.
