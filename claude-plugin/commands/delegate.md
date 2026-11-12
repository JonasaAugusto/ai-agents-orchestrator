---
description: Delegate a well-scoped task to Codex through the orchestrator board
argument-hint: <repo> <what to do>
allowed-tools: Bash(aao delegate:*), Bash(aao list:*), Bash(aao hook-prompt:*), Bash(aao wait:*), Write
---

Delegate this to Codex using the orchestrator-workflow skill: $ARGUMENTS

1. Delegate by default if the task fits in <=15 briefing lines. Decisions, conversation, UI/front-end and production stay with Claude.
2. Check `aao list --state running` and `aao list --state pending`: do not queue a task that touches the same files as a running or pending one from the same base; serialize it instead.
3. Write a temporary markdown file with the skill's fixed <=15-line template: Objective (one sentence), Files, Tests
   (exact command), Done (observable criteria), Constraints, Report (<=20 lines with test evidence and gaps).
   Never paste code or diffs, or include secrets. Cite paths/symbols; stable repo context belongs in `AGENTS.<repo>.md`
   using `templates/AGENTS.example.md`.
4. Run `aao delegate --repo <repo> --title "<short title>" --files <comma-separated paths> --briefing <that file>`.
5. Report the task id, then prefer the next hook note (`aao hook-prompt`) or optional wake-up. Do not poll in a loop;
   use `aao wait` only if blocking is necessary, as described in the skill.
