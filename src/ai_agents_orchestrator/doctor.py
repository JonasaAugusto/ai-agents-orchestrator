"""`aao doctor`: checks the installation and prints clear fix instructions.

Every external probe goes through `Ambiente` (injectable), so tests use fakes. Credentials are NEVER read, copied or
printed: Codex login is checked only with the official `codex login status`, run as the service user.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import config, engines
from .config import CLI, Config

OK, AVISO, FALHA, INFO = "ok", "warn", "FAIL", "info"
AVISO_SEM_CANAL = ("Without a chat channel you cannot send prompts to Codex from your phone, receive its notifications, "
                   "or approve publishing with a button. Telegram is the channel that enables this.")


@dataclass
class Item:
    nome: str
    estado: str
    detalhe: str = ""
    correcao: str = ""


class Ambiente:
    """Real probes. Tests replace the methods."""

    def which(self, prog: str) -> str | None:
        return shutil.which(prog)

    def rodar(self, cmd: list[str], env: dict | None = None, timeout: int = 30) -> tuple[int, str]:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
            return r.returncode, (r.stdout + r.stderr)
        except FileNotFoundError:
            return 127, "not found"
        except (OSError, subprocess.TimeoutExpired) as e:
            return 1, type(e).__name__

    def usuario_existe(self, nome: str) -> bool:
        try:
            import pwd
            pwd.getpwnam(nome)
            return True
        except KeyError:
            return False

    def euid(self) -> int:
        return os.geteuid()

    def stat(self, p: Path):
        try:
            return os.stat(p)
        except OSError:
            return None

    def ler(self, p: Path) -> str | None:
        try:
            return Path(p).read_text(encoding="utf-8")
        except OSError:
            return None

    def modulo(self, nome: str) -> bool:
        import importlib.util
        return importlib.util.find_spec(nome) is not None


def comando_login(cfg: Config, device: bool = True) -> str:
    return (f"sudo -u {cfg.service_user} env HOME={cfg.state_dir} CODEX_HOME={cfg.codex_home} "
            f"codex login{' --device-auth' if device else ''}")


def checar_codex(cfg: Config, amb: Ambiente) -> list[Item]:
    if not amb.which(cfg.codex_bin):
        return [Item("codex CLI", FALHA, "not installed",
                     "Install the official Codex CLI (e.g. `npm install -g @openai/codex`), then run `aao doctor` again.")]
    env = engines.env_filhos(cfg.state_dir)
    cmd = [cfg.codex_bin, "login", "status"]
    if amb.euid() == 0 and amb.usuario_existe(cfg.service_user):
        cmd = ["runuser", "-u", cfg.service_user, "--", *cmd]
    code, out = amb.rodar(cmd, env=env)
    from . import sanitize
    out = sanitize.mascarar(out.strip(), dev=True)[:160]
    if code == 0 and "not logged in" not in out.lower():
        return [Item("codex login", OK, out or "logged in")]
    return [Item("codex login", FALHA, out or "not logged in",
                 f"Log in with the OFFICIAL command, as the service user: `{comando_login(cfg)}` (headless servers; "
                 f"open the URL it prints and type the code), or `{comando_login(cfg, False)}` on a machine with a "
                 "browser. If the login expires later, the service pauses the queue and tells you this same command.")]


def checar_claude(cfg: Config, amb: Ambiente, settings: list[Path] | None = None) -> list[Item]:
    itens = []
    if not amb.which(cfg.claude_bin):
        itens.append(Item("claude CLI", AVISO, "Claude Code not found in PATH",
                          "Install Claude Code for the user that runs your interactive sessions."))
    else:
        itens.append(Item("claude CLI", OK, "installed"))
    settings = settings or [Path.home() / ".claude" / "settings.json"]
    achou_hook = achou_plugin = False
    for s in settings:
        txt = amb.ler(s) or ""
        if "claude-limit" in txt or "claude-limite" in txt:
            achou_hook = True
        if "ai-agents-orchestrator" in txt:
            achou_plugin = True
    if achou_hook or achou_plugin:
        itens.append(Item("claude hooks", OK, "handoff hooks or plugin found in Claude Code settings"))
    else:
        itens.append(Item("claude hooks", AVISO, "no handoff hooks found",
                          "Install the Claude Code plugin from claude-plugin/ (see README) or merge "
                          "examples/claude-hooks.json into ~/.claude/settings.json; then run `aao arm`."))
    return itens


def _env_seguro(cfg: Config, amb: Ambiente) -> Item | None:
    st = amb.stat(config.ENV_FILE)
    if st is None:
        return Item("secrets env file", AVISO, f"{config.ENV_FILE} not found",
                    f"Run `{CLI} setup` (it writes the secrets there with mode 0600, owner root).")
    if stat.S_IMODE(st.st_mode) & 0o077 or st.st_uid != 0:
        return Item("secrets env file", FALHA, f"{config.ENV_FILE} is readable by others or not owned by root",
                    f"chown root:root {config.ENV_FILE} && chmod 600 {config.ENV_FILE}")
    return Item("secrets env file", OK, f"{config.ENV_FILE} is 0600 root")


def checar_canal(cfg: Config, amb: Ambiente) -> list[Item]:
    itens: list[Item] = []
    if cfg.channel == "telegram":
        if not cfg.token:
            itens.append(Item("telegram token", FALHA, "missing",
                              "Create a bot with @BotFather (/newbot), then put AAO_TELEGRAM_TOKEN=<token> in the "
                              f"root-only env file ({config.ENV_FILE}, mode 0600) or run `{CLI} setup`."))
        else:
            itens.append(Item("telegram token", OK, "present (not shown)"))
        if not cfg.chat_id:
            itens.append(Item("telegram user id", FALHA, "missing",
                              "Send any message to @userinfobot to get your numeric user id; set telegram.chat_id. "
                              "Only that user (in a private chat) is ever answered."))
        else:
            itens.append(Item("telegram user id", OK, "set"))
        e = _env_seguro(cfg, amb)
        if e:
            itens.append(e)
    elif cfg.channel == "whatsapp":
        from .whatsapp import AVISO_RISCO
        itens.append(Item("whatsapp risk", AVISO, AVISO_RISCO))
        if not cfg.whatsapp_accept_risk:
            itens.append(Item("whatsapp opt-in", FALHA, "whatsapp.accept_unofficial_risk is not true",
                              "Only if you accept the risk above: set whatsapp.accept_unofficial_risk = true."))
        if not cfg.chat_id:
            itens.append(Item("whatsapp owner number", FALHA, "missing",
                              "Set whatsapp.owner_number (international format, digits only)."))
        if not amb.modulo("neonize"):
            itens.append(Item("whatsapp library", FALHA, "neonize not installed",
                              "install the optional extra: pip install 'ai-agents-orchestrator[whatsapp] @ git+https://github.com/JonasaAugusto/ai-agents-orchestrator'"))
        sess = cfg.whatsapp_dir / "session.sqlite3"
        st = amb.stat(sess)
        if st is None:
            itens.append(Item("whatsapp pairing", AVISO, "no session yet", f"Run `{CLI} setup` to pair (QR code or pairing code)."))
        elif stat.S_IMODE(st.st_mode) & 0o077:
            itens.append(Item("whatsapp session", FALHA, "session file readable by others", f"chmod 600 {sess}"))
        else:
            itens.append(Item("whatsapp session", OK, "paired, 0600"))
    else:
        itens.append(Item("chat channel", INFO, "none (CLI + Claude Code only). " + AVISO_SEM_CANAL,
                          f"Optional: run `{CLI} setup` and choose Telegram."))
    return itens


def checar_repos(cfg: Config, amb: Ambiente) -> list[Item]:
    if not cfg.repos:
        return [Item("repositories", FALHA, "no repositories configured",
                     "Add [repos.<name>] tables to the config (see examples/config.example.toml).")]
    itens = []
    for nome, spec in cfg.repos.items():
        gh = spec.get("github")
        if gh:
            code, out = amb.rodar(["git", "ls-remote", "--exit-code", f"https://github.com/{gh}.git", "HEAD"],
                                  env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", "/root"),
                                       "GIT_TERMINAL_PROMPT": "0"}, timeout=60)
            if code == 0:
                extra = "" if gh in cfg.push_allowlist else " (not in publish.push_allowlist: the agent cannot push it)"
                itens.append(Item(f"repo {nome}", OK if not extra else AVISO, f"github {gh} reachable{extra}"))
            else:
                itens.append(Item(f"repo {nome}", FALHA, f"cannot read github {gh}",
                                  "Give the account that runs the root daemon read/write access (e.g. a git credential "
                                  "helper with a fine-grained token limited to these repos), then retry."))
        elif spec.get("fonte") or spec.get("fonte_url") or spec.get("source"):
            fonte = spec.get("fonte") or spec.get("source") or spec.get("fonte_url")
            ok = str(fonte).startswith(("http://", "https://")) or amb.stat(Path(str(fonte))) is not None
            itens.append(Item(f"repo {nome}", OK if ok else FALHA, f"local source {fonte}",
                              "" if ok else "Fix the `fonte` path of this repo."))
        else:
            itens.append(Item(f"repo {nome}", FALHA, "no `github` nor `fonte`", "Set one of them."))
    return itens


def checar_base(cfg: Config, amb: Ambiente) -> list[Item]:
    itens = []
    if amb.stat(config.CONFIG_FILE) is None:
        itens.append(Item("config file", AVISO, f"{config.CONFIG_FILE} not found (defaults in use)", f"Run `{CLI} setup`."))
    else:
        itens.append(Item("config file", OK, str(config.CONFIG_FILE)))
    if amb.usuario_existe(cfg.service_user):
        itens.append(Item("service user", OK, cfg.service_user))
    else:
        itens.append(Item("service user", FALHA, f"user '{cfg.service_user}' does not exist",
                          f"useradd --system --home-dir {cfg.state_dir} --shell /usr/sbin/nologin {cfg.service_user}"))
    if cfg.codex_full_access:
        itens.append(Item("codex sandbox", AVISO, "codex.full_access = true: Codex runs with NO sandbox",
                          "Set codex.full_access = false unless you really accept that risk."))
    else:
        itens.append(Item("codex sandbox", OK, "workspace-write sandbox" + (" with network" if cfg.codex_network else ", no network")))
    if cfg.claude_wakeup:
        itens.append(Item("claude wake-up", AVISO, "EXPERIMENTAL: uses an undocumented local socket format"))
    if amb.stat(cfg.rules_file) is None:
        itens.append(Item("rules file", AVISO, f"{cfg.rules_file} not found",
                          "Copy rules/BASE_RULES.example.md there and adapt it."))
    return itens


def diagnosticar(cfg: Config, amb: Ambiente | None = None, settings: list[Path] | None = None) -> list[Item]:
    amb = amb or Ambiente()
    return (checar_base(cfg, amb) + checar_codex(cfg, amb) + checar_claude(cfg, amb, settings)
            + checar_canal(cfg, amb) + checar_repos(cfg, amb))


def main(cfg: Config, como_json: bool = False, amb: Ambiente | None = None, saida=print, settings=None) -> int:
    itens = diagnosticar(cfg, amb, settings)
    if como_json:
        saida(json.dumps([i.__dict__ for i in itens], ensure_ascii=False, indent=1))
    else:
        for i in itens:
            saida(f"[{i.estado:>4}] {i.nome}: {i.detalhe}")
            if i.correcao and i.estado != OK:
                saida(f"       fix: {i.correcao}")
    falhas = [i for i in itens if i.estado == FALHA]
    if not como_json:
        saida(f"\n{len(falhas)} problem(s) found." if falhas else "\nAll required checks passed.")
    return 1 if falhas else 0
