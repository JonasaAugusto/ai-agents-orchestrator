---
name: Bug report
about: Something does not work as documented
labels: bug
---

**What happened**

**What you expected**

**How to reproduce**
1.
2.

**Environment**
- ai-agents-orchestrator version (`aao --version`):
- Python version:
- OS / init system:
- Install method: source / wheel / sdist / plugin marketplace; release or checkout SHA:
- Codex CLI version (`codex --version`) and Claude Code version (`claude --version`):
- Chat channel: telegram / whatsapp / none

**Output of `aao doctor`** (it never prints secrets; still, double-check before pasting)

**Installation problem?** Run `sh scripts/clean-install-test.sh --empty` from a checkout and include its exit status
and sanitized final error. An absent Codex/service is expected in that test; an additional failure is not.

Please do not paste tokens, chat ids, phone numbers, transcripts or anything from your repositories you would not
publish. Security problems: see SECURITY.md instead of opening an issue.
