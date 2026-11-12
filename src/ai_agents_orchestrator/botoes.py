"""Buttons with signed (HMAC) callbacks: single use, expiring, restricted to the owner's chat.

callback_data = "<id>:<mac>"; the id points to a row in `acoes` (kind, payload, operation). The mac covers id,
operation and chat: forging or reusing a button from another message fails. Each GROUP of buttons is consumed
only once (anti-replay) and expires (default 10 min).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time

from .db import Estado


class Botoes:
    def __init__(self, estado: Estado, token: str, chat_id: int, expira_seg: int = 600, relogio=time.time):
        self.estado, self.chat_id, self.expira, self.relogio = estado, int(chat_id), expira_seg, relogio
        self._chave = hashlib.sha256(b"aao-cb|" + token.encode()).digest()

    def _mac(self, aid: str, op: str, kind: str, payload: dict) -> str:
        body = json.dumps([aid, op, self.chat_id, kind, payload], sort_keys=True, separators=(",", ":"))
        return hmac.new(self._chave, body.encode(), hashlib.sha256).hexdigest()[:12]

    def teclado(self, kind: str, payload: dict, opcoes: list[tuple[str, str]], linhas: int | None = None,
                agora: float | None = None) -> list[list[tuple[str, str]]]:
        """opcoes = [(label, op)]. Returns rows of (label, callback_data)."""
        ids = self.estado.nova_acao(kind, payload, [op for _r, op in opcoes], self.expira, agora if agora is not None else self.relogio())
        botoes = [(r, f"{ids[op]}:{self._mac(ids[op], op, kind, payload)}") for r, op in opcoes]
        n = linhas or len(botoes)
        return [botoes[i:i + n] for i in range(0, len(botoes), n)]

    def validar(self, data: str, chat_id: int, agora: float | None = None):
        """(kind, payload, op) or (None, reason, None)."""
        if int(chat_id) != self.chat_id:
            return None, "chat", None
        try:
            aid, mac = data.split(":", 1)
        except ValueError:
            return None, "formato", None
        r = self.estado.acao(aid)
        if not r:
            return None, "desconhecido", None
        if not hmac.compare_digest(mac, self._mac(aid, r["op"], r["kind"], json.loads(r["payload"]))):
            return None, "assinatura", None
        linha, motivo = self.estado.consumir_acao(aid, agora if agora is not None else self.relogio())
        if not linha:
            return None, motivo, None
        if not hmac.compare_digest(mac, self._mac(aid, linha["op"], linha["kind"], json.loads(linha["payload"]))):
            return None, "assinatura", None
        return linha["kind"], json.loads(linha["payload"]), linha["op"]
