---
description: Review a Codex task with the full protocol before anything is published
argument-hint: <task id>
allowed-tools: Bash(aao result:*), Bash(aao show:*), Bash(git:*), Read, Grep
---

Review Codex task #$ARGUMENTS following the review protocol of the orchestrator-workflow skill:

1. `aao result $ARGUMENTS`: read the summary, the declared Status line, the branch, the patch path and any warning
   (PARTIAL, REVIEW DELETIONS, time-dependent tests, snapshot warnings).
2. Read the whole diff yourself (`git -C <workspace> diff <base>...<branch>`), as a reviewer, not just the summary.
3. For every new test, prove it fails without the change and passes with it.
4. Run the full test suite yourself, against a disposable database or fixtures only.
5. Give a verdict: approve, adjust (with a concrete list) or reject. A partial or blocked task is not done.
6. Never publish from here. If approved, tell the user how to publish: they run `aao publish <id>` as root in their own
   interactive terminal and type the task id to confirm. Production deploys need their separate approval.
