---
description: Delegate a well-scoped task to Codex through the orchestrator board
argument-hint: <repo> <what to do>
allowed-tools: Bash(aao delegate:*), Bash(aao list:*), Bash(aao wait:*), Write
---

Delegate this to Codex using the orchestrator-workflow skill: $ARGUMENTS

1. Decide if it is a good fit for Codex (isolated scope, clear files, testable). If not, say why and offer to do it yourself.
2. Check `aao list --state em_andamento` and `aao list --state pendente`: do not queue a task that touches the same files as a running or pending one from the same base; serialize it instead.
3. Write the task description to a temporary markdown file (goal, files, acceptance criteria, test command). No secrets.
4. Run `aao delegate --repo <repo> --title "<short title>" --files <comma-separated paths> --briefing <that file>`.
5. Report the task id, then wait for the wake-up message or run `aao wait <id> --timeout 3600` in the background. Do not poll in a loop.
