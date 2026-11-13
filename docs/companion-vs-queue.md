# Companion agent vs. queue script: when to use each

Both run a stream of Codex lots one at a time. They differ in who waits and who judges.

| | Companion agent ([skill](../claude-plugin/skills/orchestrator-workflow/companion-agent.md)) | Queue script (`aao queue`) |
| --- | --- | --- |
| Who waits | an agent (spends tokens at each wake-up and each report) | a Python process (no tokens) |
| Who judges | the agent: reads reports and diffs, adapts the next briefing | fixed mechanical checks only |
| Deterministic | no | yes: same checks, same order, resumable state file |
| Best for | lots whose outcome changes the plan | predictable lots (migrations, mechanical refactors, test batches) |
| Weak at | long waits, many lots | anything needing judgment |
| Usage limit | the agent must notice and stop | detected from `aao status`/task errors; stops and resumes without repeating lots |

## Recommended: hybrid

The script drives; an agent reviews at the pause points.

1. `aao queue start lots/` runs lots until `--pause-every N` (default 3) are ready, a usage limit appears, or the queue ends.
2. It writes `NEEDS_REVIEW.md` with the branches and review commands. No tokens were spent while waiting.
3. An agent (or you) reviews those lots: reads `aao result <id> --short`, opens only risk files, accepts or writes a follow-up lot.
4. `aao queue reviewed L1 L2 L3`, then `aao queue resume`.

Agent tokens go to judgment, not to waiting.

## Step by step

```bash
mkdir lots && cp examples/queue-lots/*.md lots/        # edit repo, repo_path, files
aao status --brief                                      # service up, effort medium, nothing running
aao queue start lots/ --forbid-ai-mentions --pause-every 3
aao queue status            # table of lots
aao queue log -n 20         # follow what happened
aao queue stop              # ask it to stop at the next check (the running task is not cancelled)
# after a pause or a usage limit:
cat .aao-queue/NEEDS_REVIEW.md
aao queue reviewed L1 L2 L3
aao queue resume            # refuses while the limit is active unless --force; never repeats finished lots
```

Lot file: Markdown with a short header (`id`, `repo`, optional `repo_path`, `title`, `depends`, `files`, `effort`,
`model`) and the briefing as body. State lives in `.aao-queue/state.json` (`--workdir` to change it).

## What the script checks

After each task: it ended without error and is not reported as partial; the branch exists (needs `repo_path`) with new
commits over `--base`; commit messages carry no co-author or tool/AI mention (only with `--forbid-ai-mentions`, plus any
`--forbid-pattern`); `CHANGELOG.md` was touched (`--no-changelog-check` to skip); no `.env`, key or token file and no token,
private key or database URL with a real password in the added lines; the report shows green tests. A failure sends the lot
back once on the same branch; a second failure marks it `blocked`, lots that depend on it become `blocked_by_dependency`,
and independent lots go on. It never merges, publishes, pushes or deploys.

## Notes

- Why a subcommand and not an example script: it only shells out to `aao` and `git`, needs no service config, and is
  covered by the CLI tests; an example would drift from the CLI flags.
- `AAO_QUEUE_NOTIFY="cmd args"` runs a command with the message appended on pauses, limits and completion (default: log only).
- `AAO_QUEUE_POLL` / `--poll` sets the seconds between checks (default 60).
- Report text is parsed by regular expressions; unusual wording can fail a check, which then goes to the review file, not to production.
