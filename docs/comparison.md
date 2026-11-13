# Honest comparison

These are different workflows, not a ranking. Descriptions below were checked against the linked project READMEs
on **2026-11-12**; alternatives were not installed or benchmarked. "Prefer" is our interpretation of those documented
workflows. Names are ambiguous: here Hydra means `Geraldlol/hydra`, and codex-router means `gitguffaw/codex-router`.

| Alternative and source | What it does well, according to its docs | When to prefer it |
| --- | --- | --- |
| [openai/codex-plugin-cc](https://github.com/openai/codex-plugin-cc#readme) | Codex reviews, background delegation, rescue and session transfer inside Claude Code. | You want OpenAI's direct Claude Code integration without adopting this project's service/root-daemon model. It already supports handoff; that is not unique to us. |
| [kingbootoshi/codex-orchestrator](https://github.com/kingbootoshi/codex-orchestrator#readme) | Background Codex agents in tmux, parallel sessions, live attachment, steering and result capture. | Your priority is supervising several terminal workers; Bun and tmux fit your environment. |
| [jarrodwatts/claude-delegator](https://github.com/jarrodwatts/claude-delegator#readme) | Specialist expert roles and routing via MCP. Its current README says the project is archived and its Codex integration no longer works with current Codex. | Study its specialist prompts or maintain an existing pinned legacy setup. For a new current-Codex install, its author recommends the OpenAI plugin above. |
| [mustafaakben/agent-codex](https://github.com/mustafaakben/agent-codex#readme) | Delegation/review/research commands, agents, skills and opt-in project/MCP bootstrap templates. | You want a plugin-led workflow with configurable MCP integrations rather than a persistent relay service. |
| [madd86/codex-bridge](https://github.com/madd86/codex-bridge#readme) | Context-rich prompts, tmux delegation and a supervised review loop. Its documented launch uses `--yolo`. | A small tmux bridge fits a trusted local environment and you accept its documented permission mode. |
| [LegCLI](https://legcli.com/) | Monitors usage windows and switches models/agents through a configured ladder, carrying context in the same terminal. | Interactive continuity across several CLIs matters most; check its current commercial terms before adopting. |
| [Hydra](https://github.com/Geraldlol/hydra#readme) | A durable VS Code collaboration room with shared transcripts, planning, build/verification/review phases and multiple agent heads. | You want an editor-centered shared room and its recorded decisions instead of a systemd-backed board. |
| [codex-router](https://github.com/gitguffaw/codex-router#readme) | Policy-backed Codex delegation/reviews, model/effort controls and persistent jobs from Claude Code or Antigravity. | Routing and host integration are the main need; you do not need our remote control plane. |

This project's focus is the **combination** of reserve execution with return to the coordinator, isolated task branches,
publishing approval bound to the reviewed SHA and destination, a remote owner channel, and measured usage balancing.
See [how it works](../README.md#how-it-works), [security](../SECURITY.md) and [usage measurement](../README.md#usage-measurement-and-balance-110)
for the implementation and limits. Usage balancing uses counters and configured quotas; it is not a dollar-cost
optimizer and promises no savings. A root agent can bypass application approvals; same-UID isolation is limited.
The extra service, root daemon and configuration are real adoption costs. For a review inside one editor session,
start with a smaller integration if that meets your needs.


Back to the [README](../README.md#quickstart-lite-mode) · [support policy](support-policy.md).
