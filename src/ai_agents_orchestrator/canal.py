"""Chat channel interface.

The service and the bot only talk to a channel through this small interface, so channels other than Telegram can
be added. Incoming updates use the Telegram update shape, which every adapter must produce:

  {"update_id": int, "message": {"chat": {"id": <owner id>, "type": "private"}, "from": {"id": <owner id>},
                                 "text": str, "message_id": int}}
  {"update_id": int, "callback_query": {"id": str, "from": {"id": <owner id>}, "data": <button data>,
                                        "message": {"chat": {"id": <owner id>}, "message_id": int}}}

Buttons are passed as rows of (label, callback_data). Channels without native buttons render them as numbered
options and turn a numeric reply into a `callback_query` (see whatsapp.py). The allowed-user check happens twice:
the adapter only emits updates from the configured owner, and the bot re-checks the ids.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

Botoes = list[list[tuple[str, str]]]


class CanalErro(Exception):
    """Any channel failure. Messages must never contain secrets (tokens, URLs with tokens)."""


class Canal(ABC):
    nome = "canal"
    suporta_html = False

    def __init__(self, chat_id: int):
        self.chat_id = int(chat_id)

    def permitido(self, user_id) -> bool:
        """Only the configured owner may talk to the bot."""
        try:
            return int(user_id) == self.chat_id and self.chat_id != 0
        except (TypeError, ValueError):
            return False

    @abstractmethod
    def enviar(self, texto: str, botoes: Botoes | None = None, html: bool = False) -> int | None:
        """Sends text (optionally with buttons). Returns a message id when the channel has one."""

    def editar(self, message_id: int, texto: str, botoes: Botoes | None = None, html: bool = False) -> None:
        """Replaces a sent message. Channels that cannot edit just send a new one."""
        self.enviar(texto, botoes, html=html)

    def remover_botoes(self, message_id: int, nota: str | None = None) -> None:
        if nota:
            self.enviar(nota)

    def responder_callback(self, cid: str, texto: str = "") -> None:
        """Acknowledges a button press (no-op where the channel has no such concept)."""

    def registrar_comandos(self) -> None:
        """Registers the slash-command menu (no-op where unsupported)."""

    @abstractmethod
    def atualizacoes(self, offset: int, timeout: int = 25) -> list[dict]:
        """Blocks up to `timeout` s and returns new updates (Telegram shape) with update_id >= offset."""

    def fechar(self) -> None:
        """Releases resources (connections, sessions)."""
