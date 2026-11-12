---
description: Open a live view of what Codex is doing on a task (default the running one)
argument-hint: "[task id]"
allowed-tools: Bash(aao watch-open:*), Bash(aao watch:*), PowerShell(aao watch-open *), PowerShell(aao watch *)
---

On native Windows, run the commands through the PowerShell tool; no bash syntax is required.

Open the live Codex view for task $ARGUMENTS (empty = the task running now):

1. Run `aao watch-open $ARGUMENTS`. It opens `aao watch` in a tmux split when this session runs inside tmux, else in a
   new terminal window when one can be opened, else it prints the command to run in another terminal.
2. Tell the user where the view opened, or give them the exact command it printed.
3. If they want a snapshot here instead, run `aao watch $ARGUMENTS --no-follow` and summarize the latest events
   (commands run, files edited, Codex messages) and the diff so far.
The view is read-only: watching never changes the task.
