"""History of the chat channel messages (owner <-> bot) so Claude can look them up.

Bot APIs do not return old messages, so everything that goes through the authorized chat is recorded here, in
JSONL (`logs/historico.jsonl`), already masked (sanitize.mascarar with the dev group on: sk- keys, bot tokens,
JWTs, private keys, KEY/TOKEN/SECRET=value). 30-day retention. File mode 0640 (root and the service group).
Button presses record only the action label, never the HMAC/payload.
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path

from . import config, sanitize

RETENCAO_DIAS = 30
MAX_TEXTO = 8000
DONO, BOT = "dono", "bot"  # direction: owner | bot


def redigir(texto: str) -> str:
    return sanitize.mascarar(texto or "", dev=True)


class Historico:
    def __init__(self, caminho: Path, relogio=time.time, retencao_dias: int = RETENCAO_DIAS):
        self.caminho, self.agora, self.retencao = Path(caminho), relogio, retencao_dias
        self._lock = threading.Lock()
        self._ultima_poda = 0.0

    def gravar(self, direcao: str, texto: str, ref: str | int | None = None) -> None:
        """Never raises: history is auxiliary and must not bring the bot down."""
        try:
            texto = redigir(texto)
            if len(texto) > MAX_TEXTO:
                texto = texto[:MAX_TEXTO] + f" [... {len(texto) - MAX_TEXTO} characters omitted]"
            reg = {"t": round(self.agora(), 3), "dir": direcao, "ref": None if ref is None else str(ref), "texto": texto}
            linha = (json.dumps(reg, ensure_ascii=False) + "\n").encode()
            with self._lock:
                self.caminho.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(self.caminho, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o640)
                try:
                    if hasattr(os, "fchmod"):
                        os.fchmod(fd, 0o640)  # the service umask 0077 would remove group read
                    else:
                        os.chmod(self.caminho, 0o640)
                    os.write(fd, linha)
                finally:
                    os.close(fd)
                if self.agora() - self._ultima_poda > 3600:
                    self._podar()
        except Exception:
            pass

    def _podar(self) -> None:
        self._ultima_poda = self.agora()
        corte = self.agora() - self.retencao * 86400
        regs = [r for r in ler(self.caminho) if r.get("t", 0) >= corte]
        tmp = self.caminho.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for r in regs:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        try:
            st = os.stat(self.caminho)
            os.chown(tmp, st.st_uid, st.st_gid)
        except (OSError, AttributeError):
            pass
        os.chmod(tmp, 0o640)
        os.replace(tmp, self.caminho)

    def podar(self) -> None:
        with self._lock:
            try:
                if self.caminho.exists():
                    self._podar()
            except Exception:
                pass


def ler(caminho: Path) -> list[dict]:
    regs = []
    try:
        with open(caminho, encoding="utf-8") as f:
            for linha in f:
                try:
                    r = json.loads(linha)
                except ValueError:
                    continue
                if isinstance(r, dict) and isinstance(r.get("t"), (int, float)):
                    regs.append(r)
    except PermissionError:
        raise
    except OSError:
        return []
    regs.sort(key=lambda r: r["t"])
    return regs


def consultar(caminho: Path, ultimas: int = 50, desde: str | None = None, busca: str | None = None) -> list[dict]:
    """Records in chronological order; `desde` = YYYY-MM-DD (midnight, configured timezone); `busca` is case-insensitive."""
    regs = ler(caminho)
    if desde:
        ini = datetime.strptime(desde, "%Y-%m-%d").replace(tzinfo=config.fuso()).timestamp()
        regs = [r for r in regs if r["t"] >= ini]
    if busca:
        b = busca.lower()
        regs = [r for r in regs if b in str(r.get("texto", "")).lower()]
    return regs[-ultimas:] if ultimas and ultimas > 0 else regs


def formatar(r: dict, max_texto: int | None = None) -> str:
    quando = datetime.fromtimestamp(r["t"], config.fuso()).strftime("%Y-%m-%d %H:%M:%S")
    ref = f" #{r['ref']}" if r.get("ref") else ""
    texto = str(r.get("texto", ""))
    if max_texto is not None:
        texto = " ".join(texto.split())
        if len(texto) > max_texto:
            texto = texto[:max_texto] + "..."
    return f"[{quando}] {r.get('dir', '?')}{ref}: {texto}"
