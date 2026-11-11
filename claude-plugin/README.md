# ai-agents-orchestrator - Claude Code plugin

Teaches Claude Code how to work with the [ai-agents-orchestrator](../README.md) service and wires it in:

- **Skill `orchestrator-workflow`**: when to delegate to Codex and when not to, how to write tasks (to a file, never
  inline with backticks), waiting for the wake-up/note instead of polling, the review protocol (read the diff, prove new
  tests fail without the patch, run the full suite yourself on disposable data), honest states (partial/blocked are not
  done), publishing only after review and human approval, and keeping the living briefing short.
- **Hooks**: `StopFailure` (rate_limit) runs `aao claude-limit` (handoff to Codex); `UserPromptSubmit` and
  `SessionStart` run `aao hook-prompt` (delivers pending notes from Codex). Both always exit 0.
- **Slash commands**: `/delegate <repo> <task>`, `/orchestrator-status`, `/review-task <id>`, `/codex [id]` (live view).

Requires the `aao` CLI on PATH (see the Quickstart in the main README) and a running service. Install:

```
/plugin install ai-agents-orchestrator --marketplace JonasaAugusto/ai-agents-orchestrator
```

MIT licensed.
