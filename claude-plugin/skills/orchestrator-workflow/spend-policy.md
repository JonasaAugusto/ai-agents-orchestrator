# Spend policy

Goal: the Claude and Codex limits should run out because work shipped, not because of waste. Running out fast is fine
when tasks get done; paying extra per task is not.

## Codex

- Default effort is `medium` (the "workhorse" tier, roughly Sonnet-medium). Do not set `high` or `xhigh` by default.
- Raise it only with an explicit reason (security, a hard bug) and only per task: `aao delegate --effort high ...`.
- One Codex task at a time (`service.max_concurrent_codex = 1`) unless the owner opts into parallel.
- Run focused tests while working; run the full suite once, at the end of the task.

## Claude

- Prefer a NEW subagent with a short, complete briefing over forking the conversation. Every fork rereads the whole
  inherited context, so each task costs more. Fork only when that context is indispensable.
- Briefing = objective, files, done criteria (and the test command). Never dump conversation history, code or diffs.

## Parallelism

- Allowed. At most 2 at once by default.
- Only on files/repos that do not overlap (check with `--files` and the board overlap warning).
- One worktree per agent; a single agent does the merge.

## Measure

- Use the existing measurement (`aao claude-usage`, `aao status --brief`, balance block) to compare parallel against
  isolated tasks for the same kind of work.
- At about 70% of one engine's weekly quota, with the other lower, hand work to the other engine before the first runs
  out (`balance.alert_high_pct = 70` raises the channel alert there).

## Long streams of lots

- Waiting is the expensive part. Do not keep an agent waiting on a long Codex task: use `aao queue` (a script, zero
  tokens while it waits) and spend agent tokens only on review at its pause points.
- Use a companion agent (`companion-agent.md`) only when each lot's result changes the next decision; keep its briefing
  short and let it stop at the first usage limit.
