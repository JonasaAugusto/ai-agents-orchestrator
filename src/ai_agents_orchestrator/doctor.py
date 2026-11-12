"""`aao doctor`: checks the installation and prints clear fix instructions.

Every external probe goes through `Ambiente` (injectable), so tests use fakes. Credentials are NEVER read, copied or
printed: Codex login is checked only with the official `codex login status`, run as the service user.
"""
from __future__ import annotations

import json
import math
import os
import shutil
import socket
import sqlite3
import stat
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import compat, config, engines, gitseguro
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
        return compat.which(prog)

    def rodar(self, cmd: list[str], env: dict | None = None, timeout: int = 30) -> tuple[int, str]:
        try:
            r = subprocess.run(compat.command(cmd, env), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, env=env)
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

    def euid(self) -> int | None:
        return compat.euid()

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

    def socket_ativo(self, caminho: Path) -> bool:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as cliente:
                cliente.settimeout(2)
                cliente.connect(str(caminho))
            return True
        except OSError:
            return False


HOOKS_ATUAIS = {"StopFailure": "aao claude-limit", "UserPromptSubmit": "aao hook-prompt",
                "SessionStart": "aao hook-prompt", "Stop": "aao claude-usage"}


def _json(amb: Ambiente, path: Path) -> dict:
    try:
        d = json.loads(amb.ler(path) or "{}")
        return d if isinstance(d, dict) else {}
    except ValueError:
        return {}


def _hooks_presentes(d: dict) -> set[str]:
    hooks = d.get("hooks")
    if not isinstance(hooks, dict):
        return set()
    achados = set()
    for evento, comando in HOOKS_ATUAIS.items():
        grupos = hooks.get(evento, [])
        if not isinstance(grupos, list):
            continue
        for grupo in grupos:
            if not isinstance(grupo, dict) or not isinstance(grupo.get("hooks"), list):
                continue
            if evento == "StopFailure" and grupo.get("matcher") != "rate_limit":
                continue
            for h in grupo["hooks"]:
                if not isinstance(h, dict) or h.get("type") != "command":
                    continue
                cmd = h.get("command", "")
                if cmd == "aao" and isinstance(h.get("args"), list) and all(isinstance(x, str) for x in h["args"]):
                    cmd = " ".join([cmd, *h["args"]])
                if isinstance(cmd, str) and (cmd == comando or cmd.startswith(comando + " ")):
                    achados.add(evento)
    return achados


def hooks_instalados(amb: Ambiente, settings: list[Path] | None = None) -> set[str]:
    """Check effective settings or the enabled plugin's installed manifest and hook file."""
    achados = set()
    for path in settings or [Path.home() / ".claude/settings.json"]:
        d = _json(amb, path)
        achados |= _hooks_presentes(d)
        enabled = d.get("enabledPlugins")
        if not isinstance(enabled, dict):
            continue
        registry = _json(amb, path.parent / "plugins/installed_plugins.json").get("plugins", {})
        if not isinstance(registry, dict):
            continue
        for nome, ativo in enabled.items():
            if ativo is not True or nome.split("@")[0] != "ai-agents-orchestrator":
                continue
            installs = registry.get(nome, [])
            if not isinstance(installs, list):
                continue
            for install in installs:
                if not isinstance(install, dict) or not isinstance(install.get("installPath"), str):
                    continue
                raiz = Path(install["installPath"])
                manifest = _json(amb, raiz / ".claude-plugin/plugin.json")
                try:
                    versao = tuple(int(x) for x in str(manifest.get("version", "")).split("."))
                except ValueError:
                    continue
                if manifest.get("name") == "ai-agents-orchestrator" and versao >= (1, 1, 0):
                    achados |= _hooks_presentes(_json(amb, raiz / "hooks/hooks.json"))
    return achados


def comando_login(cfg: Config, device: bool = True) -> str:
    if cfg.mode == "lite":
        return f"{cfg.codex_bin} login{' --device-auth' if device else ''}"
    return (f"sudo -u {cfg.service_user} env HOME={cfg.state_dir} CODEX_HOME={cfg.codex_home} "
            f"codex login{' --device-auth' if device else ''}")


def checar_codex(cfg: Config, amb: Ambiente) -> list[Item]:
    if not amb.which(cfg.codex_bin):
        return [Item("codex CLI", FALHA, "not installed",
                     "Install the official Codex CLI (e.g. `npm install -g @openai/codex`), then run `aao doctor` again.")]
    env = engines.env_usuario() if cfg.mode == "lite" else engines.env_filhos(cfg.state_dir)
    cmd = [cfg.codex_bin, "login", "status"]
    if cfg.mode == "full" and amb.euid() == 0 and amb.usuario_existe(cfg.service_user):
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
    if set(HOOKS_ATUAIS) <= hooks_instalados(amb, settings):
        itens.append(Item("claude hooks", OK, "all 1.1.0 handoff, prompt and usage hooks confirmed"))
    else:
        itens.append(Item("claude hooks", AVISO, "current handoff, prompt and usage hooks not confirmed",
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
            code, out = amb.rodar(["git", "-C", Path.cwd().anchor, *gitseguro.CONFIAVEL, "ls-remote", "--exit-code", f"https://github.com/{gh}.git", "HEAD"],
                                  env={**compat.windows_env(), "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", "/root"),
                                       "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_SYSTEM": os.devnull,
                                       "GIT_CONFIG_GLOBAL": str(Path.home() / ".gitconfig") if cfg.mode == "lite" else
                                       ("/root/.gitconfig" if amb.euid() == 0 else os.devnull)}, timeout=60)
            if code == 0:
                extra = "" if gh in cfg.push_allowlist else " (not in publish.push_allowlist: the agent cannot push it)"
                itens.append(Item(f"repo {nome}", OK if not extra else AVISO, f"github {gh} reachable{extra}"))
            else:
                itens.append(Item(f"repo {nome}", FALHA, f"cannot read github {gh}",
                                  ("Give your user read/write access (git credential helper with a fine-grained token "
                                   "limited to these repos, or publish.push_url_scheme = \"ssh\"), then retry."
                                   if cfg.mode == "lite" else
                                   "Give the account that runs the root daemon read/write access (e.g. a git credential "
                                   "helper with a fine-grained token limited to these repos), then retry.")))
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
    config_file = config.default_config_file()
    if amb.stat(config_file) is None:
        itens.append(Item("config file", AVISO, f"{config_file} not found (defaults in use)", f"Run `{CLI} setup`."))
    else:
        itens.append(Item("config file", OK, str(config_file)))
    if compat.WINDOWS:
        itens.append(Item("Windows runtime", OK if cfg.mode == "lite" else FALHA,
                          "native lite, UTF-8, loopback TCP with user-only token ACL; no root daemon/systemd"))
        itens.append(Item("Windows Codex sandbox", AVISO,
                          "Sandbox support depends on the installed Codex CLI; AAO does not validate its isolation",
                          "Use WSL2 for a Linux sandbox; install aao, git, codex and claude together inside WSL2. Never disable the sandbox to fix compatibility."))
        itens.append(Item("Windows paths", INFO, "Git workspaces use core.longpaths=true and autocrlf=false; prefer a short state directory for third-party tools."))
    if cfg.mode == "lite":
        itens.append(Item("runtime mode", AVISO, "lite: current user, no systemd; no isolation from engines and no Publish button",
                          "Review results, then use aao publish <id> at a terminal and confirm the full SHA."))
    elif amb.usuario_existe(cfg.service_user):
        itens.append(Item("runtime mode", INFO, "full: privileged root daemon and isolated service state; remote approval channel optional"))
        itens.append(Item("service user", OK, cfg.service_user))
    else:
        itens.append(Item("service user", FALHA, f"user '{cfg.service_user}' does not exist",
                          f"useradd --system --home-dir {cfg.state_dir} --shell /usr/sbin/nologin {cfg.service_user}"))
    if cfg.codex_full_access:
        alcance = "every phase, including read-only reviews" if cfg.codex_full_access_read_only else "every phase except read-only reviews"
        itens.append(Item("codex sandbox", AVISO, f"codex.full_access = true: Codex runs with NO sandbox, network and the service uid in {alcance}",
                          "Fine on your own machine with trusted repos (it is what gives Codex Claude's permissions). "
                          "Set codex.full_access = false for the restricted mode."))
    else:
        itens.append(Item("codex sandbox", OK, "workspace-write sandbox" + (" with network" if cfg.codex_network else ", no network")))
    itens.append(Item("orchestrator", OK, f"{cfg.orchestrator} coordinates"
                      + ("; Claude executes and reviews risk" if cfg.orchestrator == "codex" else "; Codex implements and cross-reviews")))
    if cfg.claude_wakeup:
        itens.append(Item("claude wake-up", AVISO, "EXPERIMENTAL: uses an undocumented local socket format"))
    if amb.stat(cfg.rules_file) is None:
        itens.append(Item("rules file", AVISO, f"{cfg.rules_file} not found",
                          "Copy rules/BASE_RULES.example.md there and adapt it."))
    return itens


def checar_agendamento(cfg: Config) -> list[Item]:
    """Read-only: the limit records themselves are the durable timer, no system cron required."""
    if not cfg.db_estado.exists():
        return [Item("resume schedule", INFO, "no state database yet")]
    try:
        with sqlite3.connect(cfg.db_estado.as_uri() + "?mode=ro", uri=True) as c:
            rows = dict(c.execute("SELECT k,v FROM kv WHERE k IN ('claude_limite','codex_limite')"))
        for key, raw in rows.items():
            d = json.loads(raw)
            if not isinstance(d, dict):
                raise ValueError("invalid limit record")
            campos = ("reset_epoch", "proxima_sonda") if key == "claude_limite" else ("ate",)
            for campo in campos:
                if campo in d and (isinstance(d[campo], bool) or not isinstance(d[campo], (int, float))
                                   or not math.isfinite(d[campo]) or d[campo] <= 0):
                    raise ValueError(f"invalid {key}.{campo}")
            if (key == "codex_limite" and d and "ate" not in d) or (
                    key == "claude_limite" and d.get("ativo") and "reset_epoch" not in d):
                raise ValueError(f"missing reset in {key}")
        return [Item("resume schedule", OK, "persisted SQLite resets; internal timer resumes after restart")]
    except (sqlite3.Error, ValueError, TypeError) as e:
        return [Item("resume schedule", FALHA, f"invalid persisted schedule ({type(e).__name__})",
                     "Have the service operator inspect the limit records before resuming.")]


def diagnosticar(cfg: Config, amb: Ambiente | None = None, settings: list[Path] | None = None) -> list[Item]:
    amb = amb or Ambiente()
    return (checar_base(cfg, amb) + checar_codex(cfg, amb) + checar_claude(cfg, amb, settings)
            + checar_canal(cfg, amb) + checar_repos(cfg, amb) + checar_agendamento(cfg)
            + checar_medicao(cfg, amb, settings) + checar_runtime(cfg, amb))


def checar_runtime(cfg: Config, amb: Ambiente) -> list[Item]:
    itens = []
    sockets = [("service socket", cfg.socket_path)]
    if cfg.mode == "full":
        sockets.append(("root daemon socket", cfg.root_socket))
    for nome, caminho in sockets:
        if cfg.use_tcp and nome == "service socket":
            from .cli import chamar
            ativo = bool(chamar(cfg, {"cmd": "status"}, timeout=2).get("ok"))
        else:
            ativo = amb.socket_ativo(caminho)
        fix = ("Run aao serve and check paths.socket." if cfg.mode == "lite" else
               "Ask the operator to check aao serve / aao root-daemon and their configured sockets.")
        # Em lite o serviço ainda não está de pé logo após o setup: é a próxima etapa, não uma falha.
        parado = AVISO if cfg.mode == "lite" else FALHA
        itens.append(Item(nome, OK if ativo else parado,
                          "accepting connections" if ativo else
                          ("not running yet: run aao serve" if cfg.mode == "lite" else "not accepting connections"),
                          fix if not ativo else ""))
    existe = amb.stat(cfg.para_codex / "BRIEFING.md") is not None
    itens.append(Item("live briefing", OK if existe else AVISO, "found" if existe else "BRIEFING.md missing",
                      "Ask Claude to write the live briefing; doctor --fix never invents context." if not existe else ""))
    return itens


def checar_medicao(cfg: Config, amb: Ambiente | None = None, settings: list[Path] | None = None) -> list[Item]:
    """Read-only: schema readiness, configurable quotas and Stop-hook installation."""
    from . import medicao
    amb = amb or Ambiente()
    erros = medicao.problemas_config(cfg)
    itens = [Item("usage balance config", FALHA if erros else OK,
                  "; ".join(erros) if erros else f"target ratio {cfg.balance_ratio_low}–{cfg.balance_ratio_high}; enabled={cfg.balance_enabled}")]
    faltam = [f"{m}_tokens_{j}" for m in ("claude", "codex") for j in medicao.JANELAS
              if getattr(cfg, f"{m}_tokens_{j}") == 0]
    itens.append(Item("usage quotas", AVISO if faltam else OK,
                      "unknown: " + ", ".join(faltam) if faltam else "token quotas configured",
                      "Calibrate [balance] token quotas; unknown usage never triggers routing. Codex account limits take precedence." if faltam else ""))
    achou = "Stop" in hooks_instalados(amb, settings)
    itens.append(Item("claude usage hook", OK if achou else AVISO,
                      "Stop counter hook found" if achou else "Stop counter hook not confirmed",
                      "Update the plugin to 1.1.0 or merge examples/claude-hooks.json; Stop must run aao claude-usage." if not achou else ""))
    if cfg.db_estado.exists():
        try:
            with sqlite3.connect(cfg.db_estado.as_uri() + "?mode=ro", uri=True) as c:
                cols = {r[1] for r in c.execute("PRAGMA table_info(execucoes)")}
                ok = {"tokens_in", "tokens_out", "usage_key"} <= cols
            itens.append(Item("usage schema", OK if ok else AVISO, "ready" if ok else "additive migration pending",
                              "Restart the service after operator approval to add the counter columns." if not ok else ""))
        except sqlite3.Error:
            itens.append(Item("usage schema", FALHA, "cannot read state database"))
    else:
        itens.append(Item("usage schema", INFO, "no state database yet; service creates counter columns"))
    itens.append(Item("usage mirror", OK if amb.stat(cfg.mirror / "transcript-cauda.jsonl") else AVISO,
                      "counter source: mirrored transcript tail (no conversation stored in accounting)",
                      "Run aao claude-usage after a Claude turn; the root daemon refreshes only the transcript."))
    return itens


def corrigir_triviais(cfg: Config) -> list[Item]:
    """Only missing directories and example rules; never credentials, services or existing files."""
    itens = []
    uid = None
    if cfg.mode == "full" and not compat.WINDOWS:
        import pwd
        try:
            uid = pwd.getpwnam(cfg.service_user).pw_uid if compat.euid() == 0 else None
        except KeyError:
            pass

    def criar_diretorios(path: Path, modo: int, dono: int | None = None) -> None:
        faltam = []
        while not path.exists():
            faltam.append(path)
            path = path.parent
        for path in reversed(faltam):
            try:
                path.mkdir(mode=modo)
            except FileExistsError:
                continue
            path.chmod(modo)  # examples/state must remain usable even with umask 077
            if dono is not None:
                os.chown(path, dono, -1)

    dirs = (cfg.state_dir, cfg.spool, cfg.workspaces, cfg.resultados, cfg.handoffs, cfg.para_claude,
            cfg.para_codex, cfg.para_codex / "tarefas", cfg.logs,
            *((cfg.codex_home,) if cfg.mode == "full" else ()))
    for path in dirs:
        try:
            criar_diretorios(path, 0o700, uid)
        except OSError as e:
            itens.append(Item("fix directories", FALHA, f"{path}: {type(e).__name__}"))
    roots = (Path(__file__).resolve().parents[2] / "rules", Path(sys.prefix) / "share/ai-agents-orchestrator/rules")
    for nome, destino in (("BASE_RULES", cfg.rules_file), ("DIVISION", cfg.division_file)):
        if destino.exists():
            continue
        origem = next((r / f"{nome}.example.md" for r in roots if (r / f"{nome}.example.md").is_file()), None)
        try:
            if origem is None:
                raise FileNotFoundError("example rules not installed")
            criar_diretorios(destino.parent, 0o755)
            with destino.open("x", encoding="utf-8") as f:
                if hasattr(os, "fchmod"):
                    os.fchmod(f.fileno(), 0o644)
                else:
                    os.chmod(destino, 0o644)
                f.write(origem.read_text(encoding="utf-8"))
            itens.append(Item("fix rules", INFO, f"created example {destino}; adapt it to your team"))
        except FileExistsError:
            pass
        except OSError as e:
            itens.append(Item("fix rules", FALHA, f"{destino}: {type(e).__name__}"))
    return itens


def main(cfg: Config, como_json: bool = False, amb: Ambiente | None = None, saida=print, settings=None, fix=False) -> int:
    itens = (corrigir_triviais(cfg) if fix else []) + diagnosticar(cfg, amb, settings)
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
