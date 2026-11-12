## What and why

## How it was tested
- [ ] `python -W ignore -m unittest discover -s tests -t .` passes
- [ ] New tests fail without the change and pass with it
- [ ] Key-line mutation fails the regression test and has been restored (command/result below)
- [ ] Concurrency checks passed 20 times, if applicable
- [ ] No test depends on sleep/wall-clock time (inject a clock or use events)
- [ ] `ruff check .` and `mypy` pass
- [ ] Installation/packaging changes: clean-install script, build and wheel/sdist installation checks pass

Commands, counts, baseline failure and mutation evidence:

## Limits and follow-up

Unverified platforms/live integrations, migration notes, or maintainer actions:

## Security checklist
- [ ] Safe defaults unchanged (sandbox on, no network, human approval to publish, no full access) or the change is
      explicitly opt-in and documented
- [ ] No secret, token, chat id, phone number or personal data in code, tests, fixtures or docs
- [ ] Docs updated (README.md, README.pt-BR.md, CHANGELOG.md)
