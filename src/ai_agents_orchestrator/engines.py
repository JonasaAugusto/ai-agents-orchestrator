"""Engines: Codex CLI (main worker) and Claude Code headless (optional), usage-limit detection and /usage."""
from __future__ import annotations

import json
import os
import re
import selectors
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from . import config
from .medicao import contadores

# ------------------------------------------------------------------ limit detection
# Claude Code messages observed in real transcripts (2.1.x):
#   "You've hit your session limit · resets 2:40pm (Europe/Lisbon)"
#   "You've hit your weekly limit · resets Nov 9, 2am (Europe/Lisbon)"
# Codex messages: ASSUMED from public reports ("You've hit your usage limit. ... try again at
# Jul 13th,2026 6:58 PM" / "Try again in 1 day 23 hours 37 minutes"); the code tolerates variations.
RX_LIMITE_CLAUDE = re.compile(r"(?i)you(?:'|’)ve hit your (session|weekly|usage|opus|\w+) limit|usage limit reached|limit reached.{0,20}resets")
RX_LIMITE_CODEX = re.compile(
    r"(?i)usage limit|hit your (?:usage )?limit|rate[ _-]?limit(?:ed| reached| exceeded)?|usage_limit_reached|"
    r"insufficient_quota|quota (?:exceeded|exhausted)|too many requests|try again (?:at|in)\b|\b429\b")
RX_AUTH = re.compile(r"(?i)not logged in|please (?:run )?codex login|authentication required|unauthorized|\b401\b|"
                     r"invalid[_ ]api[_ ]key|token (?:expired|invalid)|sign in")
MESES = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


@dataclass
class LimiteInfo:
    texto: str
    reset_epoch: int | None = None
    tipo: str | None = None  # sessao (session) | semanal (weekly) | outro (other)


def _hora(h: str, m: str | None, ap: str | None) -> tuple[int, int]:
    hh, mm = int(h), int(m or 0)
    if ap:
        ap = ap.lower()
        if ap == "pm" and hh < 12:
            hh += 12
        if ap == "am" and hh == 12:
            hh = 0
    return hh, mm


def parse_reset_claude(texto: str, agora: float | None = None) -> int | None:
    """Epoch from 'resets 2:40pm (Area/City)' or 'resets Nov 9, 2am (Area/City)'. Without a zone, the configured one."""
    m = re.search(r"resets?\s+(?:(?P<mes>[A-Za-z]{3})[a-z]*\s+(?P<dia>\d{1,2}),?\s+)?(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>[ap]m)?"
                  r"(?:\s*\((?P<tz>[A-Za-z_]+/[A-Za-z_]+)\))?", texto)
    if not m:
        return None
    try:
        tz = ZoneInfo(m.group("tz")) if m.group("tz") else config.fuso()
    except Exception:
        tz = config.fuso()
    agora_dt = datetime.fromtimestamp(agora if agora is not None else time.time(), tz)
    hh, mm = _hora(m.group("h"), m.group("m"), m.group("ap"))
    if m.group("mes"):
        mes = MESES.get(m.group("mes").lower()[:3])
        if not mes:
            return None
        alvo = agora_dt.replace(month=mes, day=int(m.group("dia")), hour=hh, minute=mm, second=0, microsecond=0)
        if alvo < agora_dt - timedelta(days=1):
            alvo = alvo.replace(year=alvo.year + 1)
    else:
        alvo = agora_dt.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if alvo <= agora_dt:
            alvo += timedelta(days=1)
    return int(alvo.timestamp())


def parse_reset_codex(texto: str, agora: float | None = None) -> int | None:
    """'try again at Jul 13th,2026 6:58 PM' or 'Try again in 1 day 23 hours 37 minutes' (assumed format)."""
    agora = agora if agora is not None else time.time()
    m = re.search(r"try again in\s+((?:\d+\s*(?:day|hour|hr|minute|min|second|sec)s?\s*(?:and\s*)?,?\s*)+)", texto, re.I)
    if m:
        seg = 0
        for n, u in re.findall(r"(\d+)\s*(day|hour|hr|minute|min|second|sec)", m.group(1), re.I):
            seg += int(n) * {"d": 86400, "h": 3600, "m": 60, "s": 1}[u[0].lower()]
        return int(agora + seg)
    m = re.search(r"try again at\s+([A-Za-z]{3})[a-z]*\s+(\d{1,2})(?:st|nd|rd|th)?,?\s*(\d{4})?\s*,?\s*(\d{1,2}):(\d{2})\s*([AaPp][Mm])?", texto)
    if m:
        mes = MESES.get(m.group(1).lower()[:3])
        if mes:
            hh, mm = _hora(m.group(4), m.group(5), m.group(6))
            ano = int(m.group(3)) if m.group(3) else datetime.fromtimestamp(agora).year
            try:
                return int(datetime(ano, mes, int(m.group(2)), hh, mm, tzinfo=config.fuso()).timestamp())
            except ValueError:
                return None
    return None


def detectar_limite_claude(texto: str, agora: float | None = None) -> LimiteInfo | None:
    m = RX_LIMITE_CLAUDE.search(texto or "")
    if not m:
        return None
    tipo = {"session": "sessao", "weekly": "semanal"}.get((m.group(1) or "").lower(), "outro") if m.lastindex else "outro"
    return LimiteInfo(texto=texto.strip()[:300], reset_epoch=parse_reset_claude(texto, agora), tipo=tipo)


def detectar_limite_codex(texto: str, agora: float | None = None) -> LimiteInfo | None:
    if not texto or not RX_LIMITE_CODEX.search(texto):
        return None
    return LimiteInfo(texto=texto.strip()[:300], reset_epoch=parse_reset_codex(texto, agora), tipo=None)


# ------------------------------------------------------------------ execution
@dataclass
class Resultado:
    ok: bool = False
    texto: str = ""
    thread_id: str | None = None
    limite: LimiteInfo | None = None
    auth_erro: bool = False
    cancelado: bool = False
    timeout: bool = False
    erro: str = ""
    exit_code: int | None = None
    segundos: float = 0.0
    eventos: list = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0


def motivo_sinal(code: int) -> str:
    """Negative exit code = killed by a signal; explain it instead of just showing the number."""
    try:
        nome = signal.Signals(-code).name
    except ValueError:
        nome = f"signal {-code}"
    return (f"Codex was interrupted by {nome} (exit code {code}), usually a service restart "
            "or out-of-memory; it is not an error in the request")


def env_filhos(state_dir: Path, extras: dict[str, str] | None = None) -> dict[str, str]:
    """MINIMAL environment for the engines: no Telegram token, no HMAC key."""
    e = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(state_dir), "CODEX_HOME": str(state_dir / ".codex"),
         "LANG": "C.UTF-8", "TERM": "dumb", "NO_COLOR": "1", "GIT_TERMINAL_PROMPT": "0"}
    if extras:
        e.update(extras)
    return e


class _Proc:
    """Controls a subprocess with timeout, cancellation and clean termination of its process group."""

    def __init__(self):
        self.p: subprocess.Popen | None = None
        self.cancel = threading.Event()

    def terminar(self, grace: float = 15.0) -> None:
        self.cancel.set()
        p = self.p
        if p and p.poll() is None:
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                return
            def _matar():
                time.sleep(grace)
                if p.poll() is None:
                    try:
                        os.killpg(p.pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass
            threading.Thread(target=_matar, daemon=True).start()


def _rodar(cmd: list[str], stdin: str, cwd: str | None, env: dict, timeout: float, ctl: _Proc,
           on_line=None, *, relogio=time.monotonic, esperar=time.sleep) -> tuple[int | None, list[str], str, bool]:
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd, env=env,
                         text=True, start_new_session=True, bufsize=1)
    ctl.p = p
    linhas: list[str] = []
    err: list[str] = []

    def le_out():
        for l in p.stdout:  # type: ignore[union-attr]
            linhas.append(l)
            if on_line:
                try:
                    on_line(l)
                except Exception:
                    pass

    def le_err():
        for l in p.stderr:  # type: ignore[union-attr]
            err.append(l)
            if len(err) > 400:
                del err[:100]

    t1, t2 = threading.Thread(target=le_out, daemon=True), threading.Thread(target=le_err, daemon=True)
    t1.start(); t2.start()
    try:
        p.stdin.write(stdin)  # type: ignore[union-attr]
        p.stdin.close()  # type: ignore[union-attr]
    except (BrokenPipeError, OSError):
        pass
    inicio = relogio()
    estourou = False
    while p.poll() is None:
        if ctl.cancel.is_set():
            ctl.terminar(grace=5)
        if relogio() - inicio > timeout and not estourou:
            estourou = True
            ctl.terminar(grace=10)
        esperar(0.2)
    t1.join(5); t2.join(5)
    return p.returncode, linhas, "".join(err), estourou


def sandbox_efetivo(sandbox: str, acesso_total: bool = False, incluir_somente_leitura: bool = False) -> str:
    """Sandbox Codex really gets. Only with the explicit opt-in `codex.full_access = true` does every mode other
    than read-only become danger-full-access (no sandbox, network, service uid). With the additional opt-in
    `codex.full_access_read_only = true`, read-only phases (reviews, research) get the same access, so Codex has
    exactly Claude's permissions in every phase. Default: unchanged."""
    if acesso_total and (sandbox != "read-only" or incluir_somente_leitura):
        return "danger-full-access"
    return sandbox


def status_login_codex(state_dir: Path, binario: str = "codex", runner=None) -> dict:
    """{"instalado": bool, "logado": bool, "detalhe": str}. Runs `codex login status` with the service HOME/CODEX_HOME.
    The output is masked and truncated; it is never expected to contain secrets."""
    from . import sanitize
    rodar = runner or (lambda cmd, env: subprocess.run(cmd, capture_output=True, text=True, timeout=30, env=env))
    try:
        r = rodar([binario, "login", "status"], env_filhos(Path(state_dir)))
    except FileNotFoundError:
        return {"instalado": False, "logado": False, "detalhe": "codex CLI not found in PATH"}
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"instalado": True, "logado": False, "detalhe": type(e).__name__}
    saida = sanitize.mascarar(((r.stdout or "") + (r.stderr or "")).strip(), dev=True)[:200]
    logado = r.returncode == 0 and not re.search(r"(?i)not logged in|logged out|expired", saida)
    return {"instalado": True, "logado": logado, "detalhe": saida}


class CodexRunner:
    def __init__(self, state_dir: Path, binario: str = "codex", runner=None, rede: bool = False,
                 acesso_total: bool = False, acesso_total_leitura: bool = False):
        self.state_dir = Path(state_dir)
        self.bin = binario
        self.atual: _Proc | None = None
        self._runner = runner or _rodar  # injectable in tests
        self.rede = rede  # codex.network_access
        self.acesso_total = acesso_total  # codex.full_access (DANGER, explicit opt-in)
        self.acesso_total_leitura = acesso_total_leitura  # codex.full_access_read_only (also read-only phases)

    def logado(self) -> bool:
        """Login status via the OFFICIAL command (`codex login status`), cached for 60 s. Credential files are never
        read, copied or printed by this project."""
        agora = time.time()
        cache = getattr(self, "_login_cache", None)
        if cache and agora - cache[0] < 60:
            return cache[1]
        ok = status_login_codex(self.state_dir, self.bin)["logado"]
        self._login_cache = (agora, ok)
        return ok

    def esquecer_login(self) -> None:
        self._login_cache = None

    def cancelar(self) -> None:
        if self.atual:
            self.atual.terminar()

    def comando(self, cwd: str, sandbox: str, saida: str, modelo: str | None = None, schema: str | None = None,
                resume: str | None = None, add_dirs: tuple = (), esforco: str | None = None) -> list[str]:
        sandbox = sandbox_efetivo(sandbox, self.acesso_total, self.acesso_total_leitura)
        base = [self.bin, "-a", "never", "exec"]
        if resume:
            base += ["resume", resume]
        cmd = base + ["--json", "--skip-git-repo-check", "-c", f'sandbox_mode="{sandbox}"',
                      "-c", 'approval_policy="never"', "-o", saida]
        if not resume:
            cmd += ["-s", sandbox, "-C", cwd]
            for d in add_dirs:
                cmd += ["--add-dir", str(d)]
        if sandbox == "workspace-write":
            # codex.network_access: outbound network inside the write sandbox (read-only never has network)
            cmd += ["-c", f"sandbox_workspace_write.network_access={'true' if self.rede else 'false'}"]
        if modelo:
            cmd += ["-m", modelo]
        if esforco:
            cmd += ["-c", f'model_reasoning_effort="{esforco}"']
        if schema:
            cmd += ["--output-schema", schema]
        return cmd + ["-"]

    def executar(self, prompt: str, cwd: Path, sandbox: str = "workspace-write", timeout: float = 3600,
                 modelo: str | None = None, schema: dict | None = None, resume: str | None = None,
                 add_dirs: tuple = (), on_event=None, esforco: str | None = None) -> Resultado:
        res = Resultado()
        t0 = time.time()
        with tempfile.TemporaryDirectory(prefix="cdx-", dir=str(self.state_dir / "spool")) as tmp:
            saida = str(Path(tmp) / "ultima.txt")
            schema_path = None
            if schema:
                schema_path = str(Path(tmp) / "schema.json")
                Path(schema_path).write_text(json.dumps(schema))
            cmd = self.comando(str(cwd), sandbox, saida, modelo, schema_path, resume, add_dirs, esforco)
            ctl = _Proc()
            self.atual = ctl

            def on_line(l: str):
                try:
                    ev = json.loads(l)
                except ValueError:
                    return
                self._evento(ev, res)
                if on_event:
                    on_event(ev)

            try:
                code, linhas, err, estourou = self._runner(cmd, prompt, str(cwd), env_filhos(self.state_dir), timeout, ctl, on_line)
            except FileNotFoundError:
                res.erro = "codex not found"
                return res
            finally:
                self.atual = None
            res.exit_code, res.timeout = code, estourou
            res.cancelado = ctl.cancel.is_set() and not estourou
            try:
                final = Path(saida).read_text(encoding="utf-8").strip()
            except OSError:
                final = ""
            if final:
                res.texto = final
            # events returned by an injected runner without callback (tests), or leftovers
            if not res.eventos:
                for l in linhas:
                    try:
                        self._evento(json.loads(l), res)
                    except ValueError:
                        pass
            bruto = (res.erro + "\n" + err[-2000:])
            falhou = code not in (0, None) or res.timeout
            res.limite = (detectar_limite_codex(res.erro) or detectar_limite_codex(err[-2000:])) if falhou else None
            res.auth_erro = bool(RX_AUTH.search(bruto)) and not res.limite and code not in (0, None)
            res.ok = code == 0 and not res.timeout and not res.cancelado and not res.limite and not res.auth_erro
            if not res.ok and not res.erro:
                if code is not None and code < 0 and not res.cancelado and not res.timeout:
                    res.erro = motivo_sinal(code)
                else:
                    res.erro = (err[-500:] or f"codex exited with code {code}").strip()
        res.segundos = time.time() - t0
        return res

    @staticmethod
    def _evento(ev: dict, res: Resultado) -> None:
        res.eventos.append(ev.get("type"))
        tipo = ev.get("type", "")
        if tipo == "thread.started":
            res.thread_id = ev.get("thread_id")
        elif tipo == "turn.completed":
            entrada, saida = contadores(ev.get("usage"))
            res.tokens_in += entrada
            res.tokens_out += saida
        elif tipo in ("error", "turn.failed"):
            msg = ev.get("message") or (ev.get("error") or {}).get("message") or json.dumps(ev)[:300]
            res.erro = (res.erro + "\n" + str(msg)).strip()
        elif tipo == "item.completed":
            it = ev.get("item") or {}
            if it.get("type") in ("agent_message", "assistant_message") and it.get("text") and not res.texto:
                res.texto = it["text"]


class ClaudeRunner:
    """Optional: headless `claude -p` as the service user, only if it has its own credentials."""

    def __init__(self, state_dir: Path, binario: str = "claude", runner=None):
        self.state_dir = Path(state_dir)
        self.bin = binario
        self._runner = runner or _rodar
        self.atual: _Proc | None = None

    def disponivel(self) -> bool:
        return (self.state_dir / ".claude" / ".credentials.json").exists()

    def cancelar(self) -> None:
        if self.atual:
            self.atual.terminar()

    def executar(self, prompt: str, cwd: Path, timeout: float = 3600, agora: float | None = None,
                 resume: str | None = None, on_event=None) -> Resultado:
        res = Resultado()
        t0 = time.time()
        cmd = [self.bin, "-p", "--output-format", "stream-json", "--verbose", "--permission-mode", "acceptEdits"]
        if resume:
            cmd += ["--resume", resume]
        ctl = _Proc()
        self.atual = ctl
        resultados = []

        def on_line(linha):
            try:
                ev = json.loads(linha)
            except ValueError:
                return
            if not isinstance(ev, dict):
                return
            if ev.get("session_id"):
                res.thread_id = ev["session_id"]
            if ev.get("type") == "result" or "result" in ev:
                resultados.append(ev)
            if on_event:
                on_event(ev)

        try:
            code, linhas, err, estourou = self._runner(cmd, prompt, str(cwd), env_filhos(self.state_dir), timeout, ctl, on_line)
        except FileNotFoundError:
            res.erro = "claude not found"
            return res
        finally:
            self.atual = None
        saida = "".join(linhas)
        texto = saida
        if not resultados:
            for linha in linhas:
                on_line(linha)
        try:
            d = resultados[-1] if resultados else json.loads(saida)
            texto = str(d.get("result", saida))
            res.erro = texto if d.get("is_error") else ""
            res.thread_id = d.get("session_id") or res.thread_id
            res.tokens_in, res.tokens_out = contadores(d.get("usage"), claude=True)
        except ValueError:
            res.erro = err[-500:] if code else ""
        res.exit_code, res.timeout, res.texto = code, estourou, texto
        res.cancelado = ctl.cancel.is_set() and not estourou
        res.limite = detectar_limite_claude(texto, agora) or detectar_limite_claude(err, agora)
        res.ok = code == 0 and not res.erro and not res.limite and not estourou and not res.cancelado
        res.segundos = time.time() - t0
        return res

    def sondar(self, agora: float | None = None) -> bool:
        """Light probe: does Claude answer? (1 short call). False on limit/error."""
        r = self.executar("Reply only: ok", self.state_dir, timeout=90, agora=agora)
        return r.ok


# ------------------------------------------------------------------ /usage and /model
def uso_codex(state_dir: Path, binario: str = "codex", timeout: float = 15.0) -> dict:
    """Reads the plan limits via `codex app-server` (JSON-RPC over stdio: account/rateLimits/read).
    Undocumented-ish protocol; may change between Codex CLI versions."""
    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"clientInfo": {"name": "ai-agents-orchestrator", "title": "ai-agents-orchestrator", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "account/rateLimits/read"}]
    try:
        p = subprocess.Popen([binario, "app-server", "--stdio"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, env=env_filhos(state_dir), start_new_session=True)
    except OSError as e:
        return {"ok": False, "erro": type(e).__name__}
    seletor = selectors.DefaultSelector()
    try:
        for m in msgs:
            p.stdin.write((json.dumps(m) + "\n").encode())  # type: ignore[union-attr]
        p.stdin.flush()  # type: ignore[union-attr]
        seletor.register(p.stdout, selectors.EVENT_READ)
        fim = time.monotonic() + timeout
        buffer = b""
        while True:
            restante = fim - time.monotonic()
            if restante <= 0 or not seletor.select(restante):
                break
            bloco = os.read(p.stdout.fileno(), 65536)
            if not bloco:
                break
            buffer += bloco
            if len(buffer) > 1024 * 1024:
                return {"ok": False, "erro": "oversized usage response"}
            while b"\n" in buffer:
                linha, buffer = buffer.split(b"\n", 1)
                try:
                    d = json.loads(linha)
                except ValueError:
                    continue
                if isinstance(d, dict) and d.get("id") == 2:
                    if "error" in d:
                        return {"ok": False, "erro": "usage request rejected"}
                    return {"ok": True, **resumir_limites(d.get("result") or {})}
        return {"ok": False, "erro": "no answer"}
    except OSError:
        return {"ok": False, "erro": "usage process unavailable"}
    finally:
        seletor.close()
        try:
            os.killpg(p.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            p.wait(timeout=1)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            p.wait(timeout=1)
        p.stdin.close()
        p.stdout.close()


def resumir_limites(r: dict) -> dict:
    snap = r.get("rateLimits") or {}
    def jan(w):
        if not w:
            return None
        return {"usado_pct": w.get("usedPercent"), "duracao_min": w.get("windowDurationMins"), "reseta_em": w.get("resetsAt")}
    return {"plano": snap.get("planType"), "primaria": jan(snap.get("primary")), "secundaria": jan(snap.get("secondary")),
            "limite_atingido": snap.get("rateLimitReachedType"), "uso_ordinario_permitido": r.get("ordinaryUsageAllowed")}


def modelos_codex(state_dir: Path, binario: str = "codex") -> list[dict]:
    """Local catalog (`codex debug models`): models with visibility=list."""
    try:
        r = subprocess.run([binario, "debug", "models"], capture_output=True, text=True, timeout=30,
                           env=env_filhos(state_dir))
        d = json.loads(r.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return []
    itens = d.get("models", d) if isinstance(d, dict) else d
    out = []
    for m in itens:
        if m.get("visibility") == "list":
            niveis = [x.get("effort") for x in (m.get("supported_reasoning_levels") or []) if x.get("effort")]
            out.append({"slug": m.get("slug") or m.get("id"), "nome": m.get("display_name"), "niveis": niveis,
                        "padrao": m.get("default_reasoning_level")})
    return out


# `codex debug models`: low, medium, high, xhigh (all models); max and ultra (some models only).
NIVEIS_BASE = ("low", "medium", "high", "xhigh")
NIVEIS_CONHECIDOS = NIVEIS_BASE + ("max", "ultra")


def niveis_effort(mods: list[dict], modelo: str | None) -> list[str]:
    """Valid model_reasoning_effort levels: the chosen model's; without a choice, those accepted by every listed
    model (Codex's default could be any of them); without a catalog, the 4 universal ones."""
    if modelo:
        for m in mods:
            if m["slug"] == modelo and m.get("niveis"):
                return [n for n in NIVEIS_CONHECIDOS if n in m["niveis"]]
    comuns = None
    for m in mods:
        if m.get("niveis"):
            comuns = set(m["niveis"]) if comuns is None else comuns & set(m["niveis"])
    return [n for n in NIVEIS_CONHECIDOS if n in comuns] if comuns else list(NIVEIS_BASE)
