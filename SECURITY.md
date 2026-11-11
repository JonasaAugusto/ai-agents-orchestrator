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
- Masking of secrets and personal data is pattern based: a secret in a novel format can slip through.
- The WhatsApp channel uses an unofficial client; see the warning in the README.

## Hardening notes (1.0.1)

- **Root never honours repository config it does not own.** Git config and hooks can execute commands
  (`core.fsmonitor`, `core.hooksPath`, `credential.helper=!cmd`, `core.gitProxy`, `url.*.insteadOf`, filter drivers).
  Every git call made as root goes through `gitseguro`: in repositories that someone else may write it runs with
  `GIT_CONFIG_NOSYSTEM=1`, `GIT_CONFIG_GLOBAL=/dev/null`, `-c core.fsmonitor=false -c core.hooksPath=/dev/null
  -c credential.helper= -c core.gitProxy= -c protocol.ext.allow=never` and local transport only. No
  `safe.directory=*` is used anywhere.
- **Mirrors:** the root daemon fetches into its own bare clones under `paths.root_state_dir/mirror` (0700) and then
  publishes a fresh copy to `<state_dir>/repos/<name>.git` for the service (built in a root-only staging directory,
  swapped in by rename; `<state_dir>/repos` itself is root-owned 0750). Root never runs git inside the service's copy,
  so a rewritten `config` or planted hook there is simply discarded at the next refresh. This doubles the disk used
  by mirrors.
- **`aao watch` and `aao install`** read a directory owned by another user as that user (root drops to the owner's
  uid), hardened, so nothing in it runs with root's privileges.
- **Workspace git** (commits, patches, pushes made by the service outside Codex's sandbox) always overrides hooks,
  fsmonitor, credential helpers and proxies, and refuses to deliver when the workspace `.git/config` contains keys
  outside a small allowlist (e.g. filter/diff/merge drivers, `include.path`).
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
- Known residual: the root daemon still writes the mirror files (`<state_dir>/mirror`) under the service-owned
  state directory; a separate root-owned parent for them would remove that last shared path.
