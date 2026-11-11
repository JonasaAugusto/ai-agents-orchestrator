---
description: Show the orchestrator status, the board and any pending note from Codex
allowed-tools: Bash(aao status:*), Bash(aao list:*), Bash(aao note:*), Bash(aao briefing-status:*)
---

Run `aao status`, `aao list` and `aao note`, then summarize in a few lines: what Codex is doing now, tasks in review
waiting for you, blocked tasks (with the reason), whether the queue is paused, Codex login/limit state and the age of
the living briefing. Translate internal states for the user: pendente=pending, em_andamento=in progress,
em_revisao=in review, concluida=completed, bloqueada=blocked, cancelada=cancelled. Suggest the next action.
