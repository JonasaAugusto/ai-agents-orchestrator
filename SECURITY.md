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
- The optional root daemon accepts a fixed set of operations from the service uid only, pushes only `agent/*` branches
  to allowlisted GitHub repositories, never force-pushes, tags or deletes, and runs deploy commands only from an exact
  allowlist with an HMAC-signed, expiring, single-use request.
- Masking of secrets and personal data is pattern based: a secret in a novel format can slip through.
- The WhatsApp channel uses an unofficial client; see the warning in the README.
