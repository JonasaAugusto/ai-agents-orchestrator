"""Configuration: paths, limits and safety switches.

Everything is read from ONE TOML file (default ``/etc/ai-agents-orchestrator/config.toml``, override with the
``AAO_CONFIG`` environment variable). Secrets (Telegram token, publish HMAC key) should come from the environment
(a root-only systemd ``EnvironmentFile``) so that the unprivileged service user - and therefore Codex, which runs
with the same uid - cannot read them from disk. Every TOML key can also be overridden by an ``AAO_*`` variable
(see ``ENV_MAP``). Nothing here ever prints a secret.

Identifiers are kept in Portuguese for historical reasons (see the glossary in the README).
"""
from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from . import compat

PACKAGE_DIR = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_DIR.parent.parent  # source checkout root (src layout); examples live here
NOME = "ai-agents-orchestrator"
CLI = "aao"
def user_config_dir() -> Path:
    if compat.WINDOWS:
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / NOME
    return Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / NOME


def default_config_file() -> Path:
    if os.environ.get("AAO_CONFIG"):
        return Path(os.environ["AAO_CONFIG"])
    local = user_config_dir() / "config.toml"
    if compat.WINDOWS:
        return local
    system = Path(f"/etc/{NOME}/config.toml")
    return local if local.exists() or not system.exists() else system


CONFIG_FILE = default_config_file()
ENV_FILE = Path(os.environ.get("AAO_ENV_FILE", f"/etc/{NOME}/env"))
DEV_TREE = Path(os.environ.get("AAO_DEV_TREE", f"/srv/{NOME}"))  # git checkout the owner reviews and merges into
INSTALADO = Path(os.environ.get("AAO_INSTALLED", f"/opt/{NOME}"))  # root-owned copy that actually runs

# ------------------------------------------------------------------ timezone (display, briefing timestamps, day counters)
_TZ_NOME = "UTC"


def definir_fuso(nome: str) -> None:
    global _TZ_NOME
    try:
        ZoneInfo(nome)
    except (ZoneInfoNotFoundError, ValueError):
        return
    _TZ_NOME = nome


def fuso() -> ZoneInfo:
    return ZoneInfo(_TZ_NOME)


def nome_fuso() -> str:
    return _TZ_NOME


def codigo_inseguro(raiz: Path = PACKAGE_DIR) -> list[str]:
    """Problems that would let a non-root user change code that runs as root: file/dir owned by someone else or
    writable by group/others. Empty list = ok. The root daemon refuses to start if there is any."""
    problemas: list[str] = []
    for base, dirs, arqs in os.walk(raiz):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for nome in [base] + [os.path.join(base, n) for n in dirs + arqs]:
            try:
                st = os.lstat(nome)
            except OSError:
                continue
            if st.st_uid != 0 or (st.st_mode & 0o022 and not os.path.islink(nome)):
                problemas.append(nome)
                if len(problemas) >= 5:
                    return problemas
    return problemas


def ler_env_file(caminho: Path = ENV_FILE) -> dict[str, str]:
    """Reads KEY=VALUE lines (when readable). Used only by the manual CLI; the service gets them from systemd."""
    saida: dict[str, str] = {}
    try:
        for linha in caminho.read_text(encoding="utf-8").splitlines():
            linha = linha.strip()
            if not linha or linha.startswith("#") or "=" not in linha:
                continue
            k, v = linha.split("=", 1)
            saida[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return saida


@dataclass
class Config:
    mode: str = "full"  # full: privileged daemon; lite: current user, one service process
    local_transport: str = "auto"  # auto: Unix on POSIX, authenticated loopback TCP on Windows
    state_dir: Path = Path(f"/var/lib/{NOME}")
    root_state: Path = Path(f"/var/lib/{NOME}-root")  # bare clones used by the root daemon for pushes (root only)
    fonte_transcripts: Path = Path("/root/.claude/projects/-root")  # Claude Code transcripts of the watched project
    fonte_memoria: Path = Path("/root/.claude/projects/-root/memory")  # Claude Code memory files (optional)
    codex_bin: str = "codex"
    claude_bin: str = "claude"
    service_user: str = "aao"  # unprivileged system user that runs the service (and Codex)
    channel: str = "none"  # chat channel: "telegram" | "whatsapp" | "none" (CLI + Claude Code only)
    token: str = ""  # Telegram bot token (prefer the AAO_TELEGRAM_TOKEN env var)
    chat_id: int = 0  # the ONLY owner id accepted (Telegram user id, or WhatsApp number digits); 0 = nobody
    whatsapp_accept_risk: bool = False  # UNOFFICIAL WhatsApp Web client: explicit opt-in, at your own risk
    whatsapp_owner: str = ""  # owner's WhatsApp number, international format digits only (e.g. 15551234567)
    whatsapp_max_per_minute: int = 12  # outgoing rate limit
    timezone: str = "UTC"
    task_timeout: int = 3600
    veredito_timeout: int = 600
    tarefas_por_hora: int = 6
    max_msg_in: int = 3000
    max_msg_out: int = 3500
    acordadas_por_dia: int = 6
    inativ_minutos: int = 20
    max_codex_simultaneos: int = 1
    claude_sonda_minutos: int = 15
    claude_limite_padrao_min: int = 30
    claude_dev_fallback: bool = False  # opt-in: headless dev while Codex is limited
    balance_enabled: bool = True
    claude_tokens_5h: int = 0  # operator-calibrated quota; 0 means unknown, never unlimited
    claude_tokens_7d: int = 0
    codex_tokens_5h: int = 0  # fallback when account/rateLimits/read is unavailable
    codex_tokens_7d: int = 0
    balance_ratio_low: float = 0.7
    balance_ratio_high: float = 1.4
    balance_alert_high: float = 85.0
    balance_alert_low: float = 50.0
    balance_tasks_factor: float = 0.5
    balance_refresh_seconds: int = 300
    balance_max_age_seconds: int = 900
    orchestrator: str = "claude"  # coordinator; independent of the task engine
    botao_expira_seg: int = 600
    lembrete_min: int = 45
    autor_nome: str = "Orchestrator Agent"
    autor_email: str = "agent@localhost"
    repos: dict = field(default_factory=dict)
    # --- safety switches (safe defaults)
    codex_network: bool = False  # outbound network inside Codex's workspace-write sandbox
    codex_full_access: bool = False  # DANGER: danger-full-access (no sandbox). Explicit opt-in only.
    codex_full_access_read_only: bool = False  # with full_access: read-only phases (reviews) get full access too
    claude_wakeup: bool = False  # EXPERIMENTAL: post into the interactive Claude Code session socket
    claude_sessions_dir: Path = Path("/root/.claude/sessions")
    claude_session_name: str = ""
    mask_dev_secrets: bool = True  # also mask API keys/tokens/JWTs in the mirror sent to the model
    allowed_email_domains: list = field(default_factory=lambda: ["example.com", "localhost", "noreply.github.com"])
    allowed_emails: list = field(default_factory=list)
    push_allowlist: list = field(default_factory=list)  # "owner/repo" GitHub slugs the root daemon may push to
    self_repo: str = ""  # name (in `repos`) of this orchestrator's own repo, if the agent may edit it
    rules_file: Path = Path(f"/etc/{NOME}/BASE_RULES.md")
    division_file: Path = Path(f"/etc/{NOME}/DIVISION.md")
    templates_dir: Path = Path(f"/etc/{NOME}/templates")
    socket: Path = Path(f"/run/{NOME}/cli.sock")
    root_socket: Path = Path(f"/run/{NOME}-root/root.sock")
    publish_manifest: Path = Path(f"/etc/{NOME}/publish.json")
    publish_key_file: Path = Path(f"/etc/{NOME}/publish.key")
    push_modo: str = "root"  # "root" (root daemon pushes with root's git credential) or "deploy-key" (legacy)
    push_url_scheme: str = "https"  # lite: "https" or "ssh" (git@github.com:owner/repo.git) for publishing and fetching
    permitir_url_local: bool = False  # tests only: accept github_url (file://, local path) from the repo spec

    # --- derived paths
    @property
    def shared_state(self) -> Path:
        if self.mode == "lite":
            return self.state_dir / "shared"
        # A sibling: the service may write state_dir but cannot rename these root-owned children.
        return self.state_dir.with_name(self.state_dir.name + "-root-shared")
    @property
    def mirror(self) -> Path: return self.shared_state / "mirror"
    @property
    def spool(self) -> Path: return self.state_dir / "spool"
    @property
    def workspaces(self) -> Path: return self.state_dir / "workspaces"
    @property
    def repos_dir(self) -> Path: return self.shared_state / "repos"
    @property
    def resultados(self) -> Path: return self.state_dir / "resultados"
    @property
    def handoffs(self) -> Path: return self.state_dir / "handoffs"
    @property
    def para_claude(self) -> Path: return self.state_dir / "para-claude"
    @property
    def para_codex(self) -> Path: return self.state_dir / "para-codex"
    @property
    def logs(self) -> Path: return self.state_dir / "logs"
    @property
    def codex_home(self) -> Path:
        return Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) if self.mode == "lite" else self.state_dir / ".codex"
    @property
    def db_estado(self) -> Path: return self.state_dir / "estado.db"
    @property
    def db_quadro(self) -> Path: return self.state_dir / "quadro.db"
    @property
    def armado_flag(self) -> Path: return self.state_dir / "armado"
    @property
    def whatsapp_dir(self) -> Path: return self.state_dir / "whatsapp"  # session files (0700/0600), never in the repo
    @property
    def socket_path(self) -> Path:
        return Path(os.environ.get("AAO_SOCKET", str(self.socket)))
    @property
    def use_tcp(self) -> bool:
        return self.mode == "lite" and (compat.WINDOWS or self.local_transport == "tcp")
    @property
    def espelho_estado(self) -> Path: return self.state_dir / "espelho-estado.json"

    def preparar_dirs(self) -> None:
        for d in (self.spool, self.workspaces, self.resultados, self.handoffs, self.para_claude,
                  self.logs, *((self.codex_home,) if self.mode == "full" else ())):
            d.mkdir(parents=True, exist_ok=True)
            os.chmod(d, 0o700)


# TOML key (section.key) -> (Config attribute, type). The same keys can be set with AAO_<SECTION>_<KEY>.
ENV_MAP: dict[str, tuple[str, type]] = {
    "service.local_transport": ("local_transport", str),
    "balance.enabled": ("balance_enabled", bool),
    "balance.claude_tokens_5h": ("claude_tokens_5h", int), "balance.claude_tokens_7d": ("claude_tokens_7d", int),
    "balance.codex_tokens_5h": ("codex_tokens_5h", int), "balance.codex_tokens_7d": ("codex_tokens_7d", int),
    "balance.ratio_low": ("balance_ratio_low", float), "balance.ratio_high": ("balance_ratio_high", float),
    "balance.alert_high_pct": ("balance_alert_high", float), "balance.alert_other_below_pct": ("balance_alert_low", float),
    "balance.low_ratio_tasks_factor": ("balance_tasks_factor", float),
    "balance.refresh_seconds": ("balance_refresh_seconds", int),
    "balance.max_age_seconds": ("balance_max_age_seconds", int),
    "paths.state_dir": ("state_dir", Path), "paths.root_state_dir": ("root_state", Path),
    "paths.claude_transcripts": ("fonte_transcripts", Path), "paths.claude_memory": ("fonte_memoria", Path),
    "paths.claude_sessions": ("claude_sessions_dir", Path), "paths.rules_file": ("rules_file", Path),
    "paths.division_file": ("division_file", Path), "paths.templates_dir": ("templates_dir", Path),
    "paths.socket": ("socket", Path), "paths.root_socket": ("root_socket", Path),
    "service.user": ("service_user", str), "service.timezone": ("timezone", str),
    "service.task_timeout": ("task_timeout", int), "service.verdict_timeout": ("veredito_timeout", int),
    "service.tasks_per_hour": ("tarefas_por_hora", int), "service.handoff_checks_per_day": ("acordadas_por_dia", int),
    "service.max_concurrent_codex": ("max_codex_simultaneos", int), "service.briefing_reminder_min": ("lembrete_min", int),
    "service.commit_author_name": ("autor_nome", str), "service.commit_author_email": ("autor_email", str),
    "service.self_repo": ("self_repo", str),
    "service.orchestrator": ("orchestrator", str),
    "engines.codex_bin": ("codex_bin", str), "engines.claude_bin": ("claude_bin", str),
    "codex.network_access": ("codex_network", bool), "codex.full_access": ("codex_full_access", bool),
    "codex.full_access_read_only": ("codex_full_access_read_only", bool),
    "service.channel": ("channel", str), "whatsapp.accept_unofficial_risk": ("whatsapp_accept_risk", bool),
    "whatsapp.owner_number": ("whatsapp_owner", str), "whatsapp.max_messages_per_minute": ("whatsapp_max_per_minute", int),
    "telegram.token": ("token", str), "telegram.chat_id": ("chat_id", int),
    "telegram.max_message_in": ("max_msg_in", int), "telegram.button_ttl": ("botao_expira_seg", int),
    "claude.wakeup_enabled": ("claude_wakeup", bool), "claude.session_name": ("claude_session_name", str),
    "claude.dev_fallback": ("claude_dev_fallback", bool),
    "claude.probe_minutes": ("claude_sonda_minutos", int),
    "privacy.mask_dev_secrets": ("mask_dev_secrets", bool), "privacy.allowed_email_domains": ("allowed_email_domains", list),
    "privacy.allowed_emails": ("allowed_emails", list),
    "publish.push_allowlist": ("push_allowlist", list), "publish.manifest": ("publish_manifest", Path),
    "publish.key_file": ("publish_key_file", Path), "publish.push_mode": ("push_modo", str),
    "publish.push_url_scheme": ("push_url_scheme", str),
}
# Short env aliases for the two secrets (what the systemd EnvironmentFile usually holds).
ALIASES = {"AAO_TELEGRAM_TOKEN": "telegram.token", "AAO_TELEGRAM_CHAT_ID": "telegram.chat_id",
           "FALLBACK_TASK_TIMEOUT": "service.task_timeout"}


def _conv(valor, tipo):
    if tipo is bool:
        if isinstance(valor, bool):
            return valor
        return str(valor).strip().lower() in ("1", "true", "yes", "on")
    if tipo is list:
        if isinstance(valor, list):
            return [str(x) for x in valor]
        return [x.strip() for x in str(valor).split(",") if x.strip()]
    if tipo is Path:
        return Path(str(valor))
    return tipo(valor)


def ler_toml(caminho: Path) -> dict:
    try:
        with open(caminho, "rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return {}


# English keys accepted in repo specs -> internal keys
CHAVES_REPO = {"source": "fonte", "source_glob": "fonte_glob", "source_url": "fonte_url", "base_branch": "branch_base",
               "description": "descricao"}


def normalizar_repo(spec: dict) -> dict:
    return {CHAVES_REPO.get(k, k): v for k, v in spec.items()}


def carregar_repos(dados: dict | None = None) -> dict:
    """Repos come from `[repos.<name>]` tables in the TOML file and/or a JSON file named by `repos_file`."""
    dados = dados or {}
    repos: dict = {}
    arq = dados.get("repos_file")
    if arq:
        try:
            repos.update({k: normalizar_repo(v) for k, v in json.loads(Path(arq).read_text(encoding="utf-8")).items()
                          if isinstance(v, dict) and not k.startswith("_")})
        except (OSError, ValueError):
            pass
    if isinstance(dados.get("repos"), dict):
        repos.update({k: normalizar_repo(dict(v)) for k, v in dados["repos"].items() if isinstance(v, dict)})
    return repos


def carregar(env: dict[str, str] | None = None, arquivo: Path | None = None) -> Config:
    e = dict(os.environ) if env is None else env
    dados = ler_toml(Path(e.get("AAO_CONFIG") or str(arquivo or CONFIG_FILE)))
    mode = e.get("AAO_MODE", dados.get("mode", "lite" if compat.WINDOWS else "full"))
    if mode not in ("full", "lite"):
        raise ValueError("mode must be 'full' or 'lite'")
    if compat.WINDOWS and mode == "full":
        raise ValueError("full mode requires Linux; use mode = 'lite' on Windows")
    c = Config(mode=mode)
    if mode == "lite":
        import getpass
        base = compat.user_state_dir(e)
        conf = Path(e.get("AAO_CONFIG") or str(arquivo or CONFIG_FILE)).parent
        c.state_dir, c.root_state = base, base / "publisher"
        c.service_user = getpass.getuser()
        c.socket = base / "cli.sock"
        c.root_socket = base / "unused-root.sock"
        c.publish_manifest, c.publish_key_file = conf / "publish.json", conf / "publish.key"
        c.rules_file, c.division_file = conf / "BASE_RULES.md", conf / "DIVISION.md"
        c.templates_dir = conf / "templates"
        c.fonte_transcripts = Path.home() / ".claude/projects"
        c.fonte_memoria = c.fonte_transcripts / "memory"
        c.claude_sessions_dir = Path.home() / ".claude/sessions"
    for chave, (attr, tipo) in ENV_MAP.items():
        secao, nome = chave.split(".")
        v = (dados.get(secao) or {}).get(nome) if isinstance(dados.get(secao), dict) else None
        env_nome = "AAO_" + chave.replace(".", "_").upper()
        for alias, alvo in ALIASES.items():
            if alvo == chave and e.get(alias):
                v = e[alias]
        if e.get(env_nome) not in (None, ""):
            v = e[env_nome]
        if v is None:
            continue
        try:
            setattr(c, attr, _conv(v, tipo))
        except (TypeError, ValueError):
            continue
    if c.mode == "lite":
        if "AAO_PATHS_SOCKET" not in e and not (dados.get("paths") or {}).get("socket"):
            c.socket = c.state_dir / "cli.sock"
        # Remote channels and the legacy deploy-key path belong to full mode.
        c.channel, c.push_modo, c.claude_wakeup = "none", "root", False
        # A sonda `claude -p` usa a conta e a cota do próprio usuário: no modo leve, de hora em hora (salvo escolha).
        if "AAO_CLAUDE_PROBE_MINUTES" not in e and not (dados.get("claude") or {}).get("probe_minutes"):
            c.claude_sonda_minutos = 60
        if "AAO_PATHS_CLAUDE_MEMORY" not in e and not (dados.get("paths") or {}).get("claude_memory"):
            c.fonte_memoria = c.fonte_transcripts / "memory"
    c.max_codex_simultaneos = max(1, c.max_codex_simultaneos)
    c.claude_sonda_minutos = max(1, c.claude_sonda_minutos)
    c.task_timeout = max(1, c.task_timeout)
    if c.orchestrator not in ("claude", "codex"):
        c.orchestrator = "claude"
    if c.push_url_scheme not in ("https", "ssh"):
        c.push_url_scheme = "https"
    if c.push_modo not in ("root", "deploy-key"):
        c.push_modo = "root"
    c.channel = c.channel.strip().lower() if c.channel else "none"
    if c.channel not in ("telegram", "whatsapp", "none"):
        c.channel = "none"
    if c.channel == "whatsapp":
        digitos = "".join(ch for ch in c.whatsapp_owner if ch.isdigit())
        c.chat_id = int(digitos) if digitos else 0
    c.repos = carregar_repos(dados)
    aplicar(c)
    return c


def aplicar(c: Config) -> None:
    """Process-wide settings that modules without a Config at hand need (timezone, masking policy)."""
    from . import sanitize
    definir_fuso(c.timezone)
    sanitize.configurar(mascarar_dev=c.mask_dev_secrets, dominios=c.allowed_email_domains, emails=c.allowed_emails)
