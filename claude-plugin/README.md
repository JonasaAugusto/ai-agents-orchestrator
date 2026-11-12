# ai-agents-orchestrator - Claude Code plugin

Teaches Claude Code how to work with the [ai-agents-orchestrator](../README.md) service and wires it in:

- **Skill `orchestrator-workflow`**: delegate by default with a fixed <=15-line file briefing (`--briefing`), no pasted
  code/diff, stable repo context in `AGENTS.<repo>.md`. Prefer hook notes over polling/waiting. Codex runs the suite,
  proves red/green and restored mutation, and reports in <=20 lines. Claude requests Codex cross-review before
  reading reports/source, reads `aao result <id> --short` and opens only risk files (deletions, auth, migrations,
  money, security).
  Partial/blocked remain unfinished; publishing requires review and human approval. Keep the living briefing short.
- **Hooks**: `StopFailure` (rate_limit) runs `aao claude-limit` (handoff to Codex); `Stop` runs `aao claude-usage`
  (counter-only accounting); `UserPromptSubmit` and `SessionStart` run `aao hook-prompt` (notes and cached balance
  guidance). Accounting always exits 0, even when the service is unavailable. Configure token quotas in `[balance]`.
- **Usage balance**: start with `aao status --brief` (five lines) and `aao result <id> --short` (default).
  Above the target ratio, Codex handles review and test verification; Claude reads the short findings. Below it,
  Claude may review directly. `--full` returns the original complete task record. Human publication approval stays.
- **Slash commands**: `/delegate <repo> <task>`, `/orchestrator-status`, `/review-task <id>`, `/codex [id]` (live view).

Requires the `aao` CLI on PATH (see the Quickstart in the main README) and a running service. Install:

```
/plugin install ai-agents-orchestrator --marketplace JonasaAugusto/ai-agents-orchestrator
```

MIT licensed.
