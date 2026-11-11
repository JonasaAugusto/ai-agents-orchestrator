"""Telegram channel: minimal Bot API client (stdlib only). The token never appears in logs or exceptions."""
from __future__ import annotations

import http.client
import json
import socket
import threading

from .canal import Canal, CanalErro

COMANDOS = [
    ("help", "list of commands"), ("status", "agent, queue and Claude state"),
    ("model", "show/change the Codex model"), ("usage", "plan usage and limits"),
    ("effort", "show/change Codex reasoning effort"),
    ("engine", "codex, claude or auto"), ("repos", "allowed repositories"),
    ("repo", "choose the default repository"), ("task", "create a task for Codex"),
    ("board", "Claude and Codex task board"), ("context", "Codex checks the briefing now"),
    ("resume", "resume the Codex queue"), ("pause", "pause the Codex queue"),
    ("cancel", "cancel the running task"), ("last", "latest result"),
    ("diff", "what the current branch changed"), ("branches", "agent branches"),
    ("logs", "latest log lines"), ("history", "latest messages of this chat"), ("auto", "automatic continuation on/off"),
    ("limit", "latest evidence of a Claude usage limit"),
    ("cap", "show/adjust the daily cap of Codex limit checks"), ("new", "start a new chat branch"),
]


class TelegramErro(CanalErro):
    pass


class _ConexaoIPv4(http.client.HTTPSConnection):
    """HTTPS always over IPv4. On hosts with flaky IPv6 routes to api.telegram.org, getaddrinfo returns v6 first and each
    stuck attempt waits for the whole timeout before falling back to v4; forcing IPv4 avoids those stalls."""

    def connect(self):
        erro = None
        for fam, tipo, proto, _n, addr in socket.getaddrinfo(self.host, self.port, socket.AF_INET, socket.SOCK_STREAM):
            try:
                sock = socket.socket(fam, tipo, proto)
                sock.settimeout(min(self.timeout or 10, 10))  # connecting never takes more than 10 s
                sock.connect(addr)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                sock.settimeout(self.timeout)
                break
            except OSError as e:
                erro = e
                sock.close()
        else:
            raise erro or OSError("no IPv4 address")
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


_local = threading.local()  # one keep-alive connection per thread (long polling never shares its own)


def _http(token: str, metodo: str, payload: dict, timeout: float) -> dict:
    corpo = json.dumps(payload).encode()
    caminho = f"/bot{token}/{metodo}"
    for tentativa in (0, 1):
        con = getattr(_local, "con", None)
        reaproveitada = con is not None
        if con is None:
            con = _local.con = _ConexaoIPv4("api.telegram.org", 443, timeout=timeout)
        try:
            con.timeout = timeout
            if con.sock:
                con.sock.settimeout(timeout)
            con.request("POST", caminho, body=corpo, headers={"Content-Type": "application/json"})
            r = con.getresponse()
            dados = r.read()
            if r.will_close:
                con.close()
                _local.con = None
            if r.status != 200:
                try:
                    desc = json.loads(dados).get("description", "")
                except Exception:
                    desc = ""
                raise TelegramErro(f"{metodo}: HTTP {r.status} {desc}"[:200])
            return json.loads(dados)
        except TelegramErro:
            raise
        except Exception as e:  # never propagate the original text (it may contain the URL with the token)
            con.close()
            _local.con = None
            # stale connection closed by the server before any answer: retry once on a fresh connection
            if reaproveitada and tentativa == 0 and isinstance(e, (http.client.RemoteDisconnected, ConnectionResetError, BrokenPipeError)):
                continue
            raise TelegramErro(f"{metodo}: {type(e).__name__}") from None
    raise TelegramErro(f"{metodo}: failed")  # pragma: no cover


class Telegram(Canal):
    nome = "telegram"
    suporta_html = True

    def __init__(self, token: str, chat_id: int, max_out: int = 3500, transporte=None):
        super().__init__(chat_id)
        self.token, self.max_out = token, max_out
        self._t = transporte or (lambda m, p, t: _http(self.token, m, p, t))

    def chamar(self, metodo: str, timeout: float = 10, **payload) -> dict:
        d = self._t(metodo, payload, timeout)
        if not d.get("ok"):
            raise TelegramErro(f"{metodo}: {str(d.get('description', ''))[:150]}")
        return d.get("result")

    def enviar(self, texto: str, botoes: list[list[tuple[str, str]]] | None = None, html: bool = False) -> int | None:
        """Sends text (split into chunks); buttons = rows of (label, callback_data), only on the last chunk."""
        # HTML is never split (it would cut a tag in half): HTML cards are short by construction
        blocos = [texto] if html else ([texto[i:i + self.max_out] for i in range(0, max(len(texto), 1), self.max_out)] or [""])
        ultimo = None
        for i, b in enumerate(blocos):
            p: dict = {"chat_id": self.chat_id, "text": b or "(empty)", "disable_web_page_preview": True}
            if html:
                p["parse_mode"] = "HTML"
            if botoes and i == len(blocos) - 1:
                p["reply_markup"] = {"inline_keyboard": [[{"text": r, "callback_data": d} for r, d in linha] for linha in botoes]}
            ultimo = self.chamar("sendMessage", **p).get("message_id")
        return ultimo

    def editar(self, message_id: int, texto: str, botoes=None, html: bool = False) -> None:
        """Edits a sent message (only the first chunk fits); raises TelegramErro on failure."""
        p: dict = {"chat_id": self.chat_id, "message_id": message_id, "text": (texto if html else texto[:self.max_out]) or "(empty)",
                   "disable_web_page_preview": True, **({"parse_mode": "HTML"} if html else {}),
                   "reply_markup": {"inline_keyboard": [[{"text": r, "callback_data": d} for r, d in linha] for linha in (botoes or [])]}}
        self.chamar("editMessageText", **p)

    def remover_botoes(self, message_id: int, nota: str | None = None) -> None:
        try:
            if nota:
                self.chamar("editMessageReplyMarkup", chat_id=self.chat_id, message_id=message_id, reply_markup={"inline_keyboard": []})
                self.enviar(nota)
            else:
                self.chamar("editMessageReplyMarkup", chat_id=self.chat_id, message_id=message_id, reply_markup={"inline_keyboard": []})
        except TelegramErro:
            pass

    def responder_callback(self, cid: str, texto: str = "") -> None:
        try:
            self.chamar("answerCallbackQuery", callback_query_id=cid, text=texto[:190])
        except TelegramErro:
            pass

    def registrar_comandos(self) -> None:
        self.chamar("setMyCommands", commands=[{"command": c, "description": d} for c, d in COMANDOS])

    def atualizacoes(self, offset: int, timeout: int = 25) -> list[dict]:
        d = self._t("getUpdates", {"offset": offset, "timeout": timeout,
                                   "allowed_updates": ["message", "callback_query"]}, timeout + 10)  # 35 s socket for a 25 s long poll
        if not d.get("ok"):
            raise TelegramErro("getUpdates: " + str(d.get("description", ""))[:120])
        return d.get("result", [])
