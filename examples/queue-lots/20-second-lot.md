---
id: L2
repo: web-app
repo_path: /path/to/web-app
title: Rate-limit the signup endpoint
depends: L1
files: app/signup.py, tests/test_rate_limit.py
---
Objective: at most 5 signup attempts per IP per minute.
Files: app/signup.py, tests/test_rate_limit.py
Tests: `python -m pytest tests/test_rate_limit.py`.
Done: the sixth attempt in a minute returns 429; CHANGELOG updated.
Constraints: in-memory counter is fine; document the single-process limitation.
