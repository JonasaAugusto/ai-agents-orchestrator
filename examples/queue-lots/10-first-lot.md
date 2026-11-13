---
id: L1
repo: web-app                     # name known to `aao`
repo_path: /path/to/web-app       # optional: enables branch, commit and diff checks
title: Validate signup e-mails
files: app/signup.py, tests/test_signup.py
effort: medium
---
Objective: reject malformed e-mail addresses at signup with a clear message.
Files: app/signup.py, tests/test_signup.py
Tests: `python -m pytest tests/test_signup.py` (disposable data only).
Done: malformed addresses return 422 with a message; valid ones still work; CHANGELOG updated.
Constraints: no new dependencies; do not merge or publish.
