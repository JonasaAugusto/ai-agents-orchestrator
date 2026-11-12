"""WhatsApp channel (UNOFFICIAL, EXPERIMENTAL, disabled by default).

WARNING: this adapter uses an UNOFFICIAL WhatsApp Web multi-device client (the optional `neonize` package, a Python
wrapper around whatsmeow). It is NOT affiliated with, endorsed by or supported by WhatsApp or Meta. Using it may
violate WhatsApp's Terms of Service and the number can be restricted or banned. Use a SECONDARY number, at your own
risk; this project gives no guarantee. It only runs when `whatsapp.accept_unofficial_risk = true`.

Safety measures for the number:
  - talks ONLY to the configured owner number: messages from anyone else are ignored, and sending to any other
    destination is impossible (the destination is fixed at construction);
  - no bulk sending: outgoing messages are rate limited (whatsapp.max_messages_per_minute, plus a minimum gap);
  - reconnects with exponential backoff (no restart loops); stops for good on logout or temporary ban;
  - session files live in <state_dir>/whatsapp (dir 0700, files 0600), outside any repository.

Buttons are unreliable on unofficial clients, so they are rendered as numbered options ("1 Publish / 2 Review first")
and the message-specific action code is turned into a button press (`callback_query`). Bare numbers never approve.
"""
from __future__ import annotations

import os
import queue
import re
import secrets
import threading
import time
from pathlib import Path

from .canal import Botoes, Canal, CanalErro

AVISO_RISCO = (
    "WARNING - UNOFFICIAL WhatsApp client: not affiliated with WhatsApp or Meta; it may violate WhatsApp's Terms of "
    "Service and the number can be restricted or banned. Use a secondary number, at your own risk. This project gives "
    "no guarantee. Enable it only with whatsapp.accept_unofficial_risk = true.")


class WhatsAppErro(CanalErro):
    pass


def so_digitos(numero: str | int) -> str:
    return "".join(c for c in str(numero) if c.isdigit())


class LimiteEnvio:
    """At most `por_minuto` messages in any 60 s window and at least `intervalo` s between two messages.
    `relogio`/`dormir` are injectable so tests never sleep."""

    def __init__(self, por_minuto: int = 12, intervalo: float = 1.5, relogio=time.monotonic, dormir=time.sleep):
        self.por_minuto, self.intervalo = max(1, por_minuto), intervalo
        self.relogio, self.dormir = relogio, dormir
        self._envios: list[float] = []
        self._lock = threading.Lock()

    def esperar(self) -> float:
        """Blocks until sending is allowed; returns how long it waited."""
        with self._lock:
            agora = self.relogio()
            self._envios = [t for t in self._envios if agora - t < 60]
            espera = 0.0
            if self._envios:
                espera = max(espera, self._envios[-1] + self.intervalo - agora)
            if len(self._envios) >= self.por_minuto:
                espera = max(espera, self._envios[-self.por_minuto] + 60 - agora)
            if espera > 0:
                self.dormir(espera)
            self._envios.append(self.relogio())
            return max(espera, 0.0)


class Backoff:
    """Reconnect delays: 5 s, 10 s, 20 s ... capped at 15 min; reset after a stable connection."""

    def __init__(self, inicial: float = 5, maximo: float = 900):
        self.inicial, self.maximo, self.n = inicial, maximo, 0

    def proximo(self) -> float:
        d = min(self.maximo, self.inicial * (2 ** self.n))
        self.n += 1
        return d

    def zerar(self) -> None:
        self.n = 0


class WhatsApp(Canal):
    """Channel adapter. `cliente` is a small transport object (see _ClienteNeonize) with:
       on_message(cb(remetentes: list[str], texto: str, de_mim: bool, grupo: bool)), on_fatal(cb(motivo: str)),
       send(numero: str, texto: str), connect() (blocking until disconnected), disconnect().
    Tests pass a fake; production builds the neonize-backed one."""

    nome = "whatsapp"
    suporta_html = False

    def __init__(self, dono: str | int, cliente, por_minuto: int = 12, limite: LimiteEnvio | None = None,
                 relogio=time.time, dormir=time.sleep, max_out: int = 3500):
        self.dono = so_digitos(dono)
        if not self.dono:
            raise WhatsAppErro("whatsapp.owner_number is not set")
        super().__init__(int(self.dono))
        self.cliente = cliente
        self.limite = limite or LimiteEnvio(por_minuto)
        self.relogio, self.dormir, self.max_out = relogio, dormir, max_out
        self._fila: queue.Queue = queue.Queue()
        self._seq = int(relogio() * 1000)  # update ids keep growing across restarts (the service persists the offset)
        self._seq_lock = threading.Lock()
        self._envio_lock = threading.Lock()
        self._opcoes_lock = threading.Lock()
        self._token_opcoes = ""
        self._opcoes: list[str] = []  # callback_data of the last numbered options shown
        self._parar = threading.Event()
        self.fatal: str | None = None
        self.backoff = Backoff()
        self._fio: threading.Thread | None = None
        cliente.on_message(self._recebido)
        cliente.on_fatal(self._fatal)

    # ------------------------------------------------------------ outgoing
    @staticmethod
    def renderizar(texto: str, botoes: Botoes | None) -> tuple[str, list[str]]:
        """Text + numbered options. Returns (text, callback_data in option order)."""
        opcoes = [(r, d) for linha in (botoes or []) for r, d in linha]
        if not opcoes:
            return texto, []
        linhas = [f"{i}. {r}" for i, (r, _d) in enumerate(opcoes, 1)]
        return texto + "\n\nReply with a number:\n" + "\n".join(linhas), [d for _r, d in opcoes]

    def enviar(self, texto: str, botoes: Botoes | None = None, html: bool = False) -> int | None:
        if self.fatal:
            raise WhatsAppErro(f"channel stopped: {self.fatal}")
        # Serialize sends; invalidate before exposing any part of the next message.
        # Resolution never waits on the transport, which may call us back synchronously.
        with self._envio_lock:
            with self._opcoes_lock:
                self._opcoes = []
                self._token_opcoes = ""
            corpo, opcoes = self.renderizar(texto, botoes)
            token = secrets.token_hex(4) if opcoes else ""
            if opcoes:
                corpo = corpo.replace("Reply with a number:", "Reply with the exact action code:")
                corpo += "\n" + " / ".join(f"{token}:{i}" for i in range(1, len(opcoes) + 1))
            blocos = [corpo[i:i + self.max_out] for i in range(0, max(len(corpo), 1), self.max_out)] or [""]
            for b in blocos:
                self.limite.esperar()
                try:
                    self.cliente.send(self.dono, b or "(empty)")
                except Exception as e:
                    raise WhatsAppErro(f"send failed ({type(e).__name__})") from None
            with self._opcoes_lock:
                self._opcoes, self._token_opcoes = opcoes, token
        return None

    # ------------------------------------------------------------ incoming
    def _proximo_id(self) -> int:
        with self._seq_lock:
            self._seq += 1
            return self._seq

    def _do_dono(self, remetentes: list[str]) -> bool:
        return any(so_digitos(r) == self.dono for r in remetentes if r)

    def _recebido(self, remetentes: list[str], texto: str, de_mim: bool, grupo: bool) -> None:
        """Library callback. Anything not from the owner's private chat is dropped silently."""
        if de_mim or grupo or not self._do_dono(remetentes):
            return
        texto = (texto or "").strip()
        if not texto:
            return
        uid = self._proximo_id()
        m = re.fullmatch(r"([a-f0-9]{8}):(\d{1,2})", texto)
        data = None
        with self._opcoes_lock:
            if m and m.group(1) == self._token_opcoes and 1 <= int(m.group(2)) <= len(self._opcoes):
                data = self._opcoes[int(m.group(2)) - 1]
                self._opcoes, self._token_opcoes = [], ""
        if data is not None:
            self._fila.put({"update_id": uid, "callback_query": {
                "id": f"wa-{uid}", "from": {"id": self.chat_id}, "data": data,
                "message": {"chat": {"id": self.chat_id}, "message_id": uid}}})
            return
        self._fila.put({"update_id": uid, "message": {"chat": {"id": self.chat_id, "type": "private"},
                                                      "from": {"id": self.chat_id}, "text": texto, "message_id": uid}})

    def atualizacoes(self, offset: int, timeout: int = 25) -> list[dict]:
        self._garantir_conexao()
        saida = []
        try:
            saida.append(self._fila.get(timeout=timeout))
            while True:
                saida.append(self._fila.get_nowait())
        except queue.Empty:
            pass
        return [u for u in saida if u["update_id"] >= offset]

    # ------------------------------------------------------------ connection
    def _fatal(self, motivo: str) -> None:
        self.fatal = motivo
        self._parar.set()

    def _garantir_conexao(self) -> None:
        if self._fio is None and not self._parar.is_set():
            self._fio = threading.Thread(target=self.laco_conexao, daemon=True, name="whatsapp")
            self._fio.start()

    def laco_conexao(self, max_tentativas: int | None = None) -> None:
        """Connects; when the connection drops, waits with exponential backoff and tries again. Stops on fatal events
        (logout, temporary ban) instead of looping."""
        tentativas = 0
        while not self._parar.is_set():
            inicio = self.relogio()
            try:
                self.cliente.connect()
            except Exception:
                pass
            tentativas += 1
            if self._parar.is_set() or (max_tentativas is not None and tentativas >= max_tentativas):
                return
            if self.relogio() - inicio > 600:
                self.backoff.zerar()  # it was stable for a while: start the backoff again from the beginning
            self.dormir(self.backoff.proximo())

    def fechar(self) -> None:
        self._parar.set()
        try:
            self.cliente.disconnect()
        except Exception:
            pass


# ---------------------------------------------------------------- session files
def preparar_sessao(pasta: Path) -> Path:
    """<state_dir>/whatsapp with mode 0700; the session database is created/kept at 0600."""
    pasta.mkdir(parents=True, exist_ok=True)
    os.chmod(pasta, 0o700)
    db = pasta / "session.sqlite3"
    if db.exists():
        os.chmod(db, 0o600)
    return db


def proteger_sessao(pasta: Path) -> None:
    for f in pasta.glob("*"):
        try:
            os.chmod(f, 0o600)
        except OSError:
            pass


class _ClienteNeonize:  # pragma: no cover - needs the optional dependency and a real WhatsApp account
    """Thin wrapper over neonize (optional extra: pip install 'ai-agents-orchestrator[whatsapp] @ git+https://github.com/JonasaAugusto/ai-agents-orchestrator')."""

    def __init__(self, pasta: Path):
        try:
            from neonize.client import NewClient
            from neonize.events import ConnectedEv, LoggedOutEv, MessageEv, TemporaryBanEv
        except ImportError as e:
            raise WhatsAppErro("the 'neonize' package is not installed: install the optional extra: pip install 'ai-agents-orchestrator[whatsapp] @ git+https://github.com/JonasaAugusto/ai-agents-orchestrator'") from e
        self.pasta = pasta
        db = preparar_sessao(pasta)
        os.umask(0o077)
        self._cliente = NewClient(str(db))
        self._cb_msg = None
        self._cb_fatal = None

        @self._cliente.event(MessageEv)
        def _mensagem(_c, m):
            try:
                src = m.Info.MessageSource
                remetentes = [src.Sender.User, getattr(getattr(src, "SenderAlt", None), "User", ""), src.Chat.User]
                texto = m.Message.conversation or m.Message.extendedTextMessage.text
                if self._cb_msg:
                    self._cb_msg(remetentes, texto, bool(src.IsFromMe), bool(src.IsGroup))
            except Exception:
                pass

        @self._cliente.event(ConnectedEv)
        def _conectado(_c, _ev):
            proteger_sessao(self.pasta)

        @self._cliente.event(LoggedOutEv)
        def _saiu(_c, _ev):
            if self._cb_fatal:
                self._cb_fatal("logged out (pair again with `aao setup`)")

        @self._cliente.event(TemporaryBanEv)
        def _banido(_c, _ev):
            if self._cb_fatal:
                self._cb_fatal("temporary ban reported by WhatsApp (stop using this number for automation)")

    def on_message(self, cb) -> None:
        self._cb_msg = cb

    def on_fatal(self, cb) -> None:
        self._cb_fatal = cb

    def send(self, numero: str, texto: str) -> None:
        self._cliente.send_message(numero, texto)

    def connect(self) -> None:
        self._cliente.connect()

    def disconnect(self) -> None:
        self._cliente.disconnect()

    def parear_codigo(self, numero: str) -> str:
        """Pairing code to type in WhatsApp > Linked devices > Link with phone number."""
        return self._cliente.PairPhone(numero, True)


def criar(cfg) -> WhatsApp:  # pragma: no cover - production factory
    if not cfg.whatsapp_accept_risk:
        raise WhatsAppErro("WhatsApp channel disabled: set whatsapp.accept_unofficial_risk = true. " + AVISO_RISCO)
    return WhatsApp(cfg.whatsapp_owner, _ClienteNeonize(cfg.whatsapp_dir), por_minuto=cfg.whatsapp_max_per_minute)
