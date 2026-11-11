# Division of work between Claude and Codex (example - edit freely)

Codex (backup agent / second worker) is good and cheap at:
- isolated implementation with a clear scope and file list;
- writing and fixing tests; mechanical refactors; small, well-specified fixes;
- scripts and utilities;
- cross-review of diffs (bugs, regressions, missing tests) of Claude's branches.

Claude (orchestrator) keeps:
- orchestration, planning and the conversation with the owner;
- UI/design work and anything that needs Claude Code skills;
- production: publishing, migrations, restarts, backups, servers;
- decisions that depend on the owner and ambiguous tasks;
- integration: reviewing and merging Codex's branches.

House rules:
- each worker uses its own branch (Codex: `agent/<id>-...`);
- declare the files a task will touch (`--files`) so the board can warn about overlaps;
- do not queue two tasks that touch the same files from the same base: serialize them;
- Codex never merges into main nor pushes it without the owner's approval.
