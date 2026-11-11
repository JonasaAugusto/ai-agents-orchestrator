# aao-codex-pane (experimental)

Optional companion plugin for ai-agents-orchestrator. `/codex-pane [task id]` opens a side pane inside Claude Code that
refreshes every 2 seconds with what Codex is doing on the task (commands, file edits, messages, diff so far), by running
the read-only `aao watch <id> --no-follow`. It is built on Claude Code's early-access function-hooks API (panes), which
may change between Claude Code releases; if it stops working, use `/codex` from the main plugin, which opens the same
view in a tmux split or a terminal window. Install:

```
/plugin install aao-codex-pane --marketplace JonasaAugusto/ai-agents-orchestrator
```

MIT licensed.
