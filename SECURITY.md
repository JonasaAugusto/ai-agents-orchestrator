# Security policy

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub's
[security advisories](https://github.com/JonasaAugusto/ai-agents-orchestrator/security/advisories/new). Do not open a
public issue. Include the version (`aao --version`), what an attacker needs (e.g. control of the chat account, a local
user, a malicious repository) and what they gain. You will get an answer as soon as possible; there is no bug bounty.

## Supported versions

Only the latest release receives security fixes.

## Threat model in short

- The chat channel account (Telegram or WhatsApp) is a control plane: whoever controls it can queue tasks and press
  publish buttons. Enable two-step verification on it. Only one owner id is accepted; everything else is ignored.
- Codex is untrusted code execution. By default it runs as an unprivileged user, inside its `workspace-write`
  sandbox, without network, in throwaway clones. `codex.full_access = true` removes that boundary.
- The service user cannot read the bot token or the publish HMAC key from disk (root-only `EnvironmentFile`); Codex
  shares the service uid, so it can read what that user can read, including its own Codex login.
- The root daemon accepts a fixed set of operations from the service uid only, pushes only `agent/*` branches
  to allowlisted GitHub repositories, never force-pushes, tags or deletes, and runs deploy commands only from an exact
  allowlist with an HMAC-signed, expiring, single-use request.
- Masking uses content patterns and sensitive field names in structured events: an unlabelled secret in a novel
  format can still slip through.
- The WhatsApp channel uses an unofficial client; see the warning in the README.

## Hardening notes (1.0.2)

- **Privileged filesystem state:** masked mirrors and exported repositories live under
  `<paths.state_dir>-root-shared/{mirror,repos}`, a root-owned 0750 sibling of the service-writable state directory.
  Its `private` directory (0700) holds the root audit logs (0600), including workspace audit when invoked by the
  root CLI, and mirror lock. Root validates parents using
  directory descriptors and `O_NOFOLLOW`; it refuses service-owned/writable parents and symlink directories.
  Atomic writes use unpredictable exclusive temporary files and descriptor-relative rename. Append opens refuse
  symlinks, non-regular files, hard links and files with an unexpected owner or permissions. Old mirror and audit
  paths under `state_dir` are not reused. A root-owned sticky temporary parent is supported for disposable tests.
- **Mirrors:** the root daemon fetches into its own bare clones under `paths.root_state_dir/mirror` (0700) and then
  exports a fresh copy for the service through a protected parent. Root never runs Git inside the service's copy;
  config/hooks planted there are discarded at the next refresh. This doubles the disk used by mirrors.
- **Workspace Git:** delivery runs against a fresh private gitdir with a configuration authored by the service,
  controlled global/system config, disabled hooks/fsmonitor/helpers and fixed remote URLs from operator config.
  Temporary configuration lives outside the engine workspace and system temporary directory (under service spool,
  or the protected root-private sibling when the caller is root). Only object/ref storage and explicit metadata are shared; the mutable `.git/config` is never loaded. Local clone
  sources and the daemon's workspace fetch use the same private view. A config allowlist remains a diagnostic,
  with no safety dependency on the time between checking it and running Git. Delivery supports standalone clones
  with a `.git` directory; linked-worktree/gitdir-file layouts are refused. Separate engine/service uids remain
  recommended: a private directory is not a boundary against unrestricted code running as its owner.
- **`aao watch` and `aao install`** read another user's directory as that user (root drops to the owner's uid), with
  exec-capable config overrides. **`aao doctor`** probes Git from `/`, with controlled system/global config; it does
  not inherit the invoking checkout's local configuration. Root's own credential helper remains available there.
- **Publish approval:** callbacks authenticate the payload including reviewed SHA, repository, branch, task and
  destination. The daemon's expiring, single-use HMAC covers those fields; the fetched commit must match before
  integration. The daemon merges the approved object id, even if the agent changes its branch during the request.
  A changed destination also requires fresh approval. Root CLI captures and displays these fields before confirmation.
- **WhatsApp:** approvals use a random per-message action code, consumed once; bare numbers never select actions.
  Every new outgoing message invalidates earlier options before transport starts; options activate only after a
  complete successful send. Sends are serialized, while reply resolution uses a separate lock.
- **Claude hook notifications:** only integer task ids and states from a fixed vocabulary enter the prompt.
  Titles, descriptions, branches and reports remain untrusted data to consult explicitly, not hook instructions.
- **Structured masking:** sensitive key names (including nested API keys, tokens, passwords, credentials and
  authorization values) redact the entire value, including objects and numbers, in addition to content patterns.
- **Secrets in the service process:** at start the service calls `prctl(PR_SET_DUMPABLE, 0)` (Linux), so a process of
  the same uid (Codex) can no longer read `/proc/<pid>/environ` or `/proc/<pid>/mem` nor ptrace it, and removes
  `AAO_TELEGRAM_TOKEN` / `AAO_PUBLISH_KEY` from its environment after reading them. There is a brief window between
  exec and that call; and the same uid can still read every *file* the service user can.
- **Recommended: run the engines under a separate uid.** The strongest boundary is a second unprivileged user for
  Codex/Claude runs (no access to the service's state, database or sockets), with the service handing it only the
  workspace. This is not automated yet; the example units already add `NoNewPrivileges`, `ProtectSystem=strict`,
  `ProtectHome`, an empty capability set and more for the service, and the compatible subset for the root daemon
  (`ProtectSystem=full` is offered commented out because deploy commands run under that unit).
- **CLI publish needs the owner at a terminal.** `aao publish <id>` (root) asks the owner to type the task id in an
  interactive terminal; `--approved-by-owner` alone is self-attested and no longer publishes. This stops an agent
  from approving its own work through the CLI, but it is not a boundary against a process that already has root
  (such a process can read the publish key directly). Keep agents that run as root on a short leash.
- **Wake-up carries no agent text.** The optional wake-up sends only "Task #N finished with status X. Run
  `aao result N`." (or the fixed "limit renewed" notice); the text is built by the root daemon from the task id and
  a state from a fixed list, so a report written by Codex can never be injected into the Claude session.
- **`config.toml`** is written 0640 root:<service group> (0600 if the group does not exist yet), since it holds the
  owner's chat id or phone number.
- **Upgrade to 1.0.2:** the operator must restart both components with matching versions, refresh the mirrors and
  regenerate approvals. Existing workspace trees stay usable; old buttons/HMAC requests are rejected. The parent
  of `state_dir` must be root-controlled; update custom paths and filesystem write allowlists for the new sibling.
  This release does not change the separate-uid recommendation, the brief startup secret window, or the fact that
  an agent already running as root can bypass application-level approval.
