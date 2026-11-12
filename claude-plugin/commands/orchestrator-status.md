---
description: Show the orchestrator status, the board and any pending note from Codex
allowed-tools: Bash(aao status:*), Bash(aao list:*), Bash(aao note:*), Bash(aao briefing-status:*)
---

Run `aao status --brief`; summarize the queue, pause state and scheduled resets. Use `aao list --state review`
only when review details are needed; its states are already translated. Open full `aao status`, `aao note` or
`aao briefing-status` only to answer a specific question about activity, limits or briefing age.
Prefer the next `aao hook-prompt` note over `aao wait` or repeated status checks. Suggest the next action in a few lines.
