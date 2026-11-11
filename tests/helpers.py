import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from ai_agents_orchestrator import config
from ai_agents_orchestrator.db import Estado, Quadro
from ai_agents_orchestrator.engines import Resultado
from ai_agents_orchestrator.servico import Servico
from ai_agents_orchestrator.bot import Bot
from ai_agents_orchestrator.telegram import Telegram
from ai_agents_orchestrator.testing import CodexFalso, RootFalso

TOKEN = "123456789:" + "A" * 35  # token sintetico
CHAT = 424242


def sh(*args, cwd=None):
    env = {"PATH": os.environ["PATH"], "HOME": os.environ.get("TMPDIR", "/tmp"), "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x.com"}
    return subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True, check=True).stdout


class FakeTransport:
    """Registra as chamadas ao Telegram; nenhuma rede."""

    def __init__(self):
        self.chamadas = []
        self.updates = []

    def __call__(self, metodo, payload, timeout):
        self.chamadas.append((metodo, payload))
        if metodo == "getUpdates":
            r, self.updates = self.updates, []
            return {"ok": True, "result": r}
        if metodo == "sendMessage":
            return {"ok": True, "result": {"message_id": len(self.chamadas)}}
        return {"ok": True, "result": True}

    def enviados(self):
        return [p for m, p in self.chamadas if m == "sendMessage"]

    def textos(self):
        return [p["text"] for p in self.enviados()]


class _Sincrono:
    def submit(self, fn, *a):
        fn(*a)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.cfg = config.Config(state_dir=self.dir / "state", root_state=self.dir / "root-state", token=TOKEN, chat_id=CHAT, channel="telegram")
        self.cfg.claude_wakeup = True  # the experimental wake-up is exercised by the tests (default is off)
        self.cfg.preparar_dirs()
        self.cfg.mirror.mkdir(exist_ok=True)
        self.cfg.repos = {}
        self.t = [1000.0]
        self.estado = Estado(self.cfg.db_estado)
        self.quadro = Quadro(self.cfg.db_quadro)
        self.addCleanup(self.estado.c.close)
        self.addCleanup(self.quadro.c.close)
        self.tr = FakeTransport()
        self.tg = Telegram(TOKEN, CHAT, transporte=self.tr)
        self.codex = CodexFalso(self.cfg.state_dir)
        self.root = RootFalso(self.cfg)
        self.svc = Servico(self.cfg, self.estado, self.quadro, self.tg, self.codex, None, self.root, relogio=lambda: self.t[0])
        self.bot = Bot(self.svc)
        self.bot._conversa = _Sincrono()  # conversa com o Codex roda na hora nos testes

    def msg(self, texto, chat=CHAT, tipo="private", de=None):
        return {"update_id": 1, "message": {"chat": {"id": chat, "type": tipo}, "from": {"id": de or chat}, "text": texto, "message_id": 5}}

    def clique(self, data, chat=CHAT, de=None):
        return {"update_id": 2, "callback_query": {"id": "cb1", "from": {"id": de or chat}, "data": data,
                                                  "message": {"chat": {"id": chat}, "message_id": 9}}}

    def botoes_enviados(self):
        out = []
        for p in self.tr.enviados():
            for linha in (p.get("reply_markup") or {}).get("inline_keyboard", []):
                out += [(b["text"], b["callback_data"]) for b in linha]
        return out

    def apertar(self, rotulo):
        for r, d in reversed(self.botoes_enviados()):
            if r == rotulo:
                return self.bot.processar(self.clique(d))
        raise AssertionError(f"botao {rotulo} nao encontrado em {self.botoes_enviados()}")

    def criar_repo(self, nome="web-app"):
        """Repo bare de origem + espelho bare + remoto 'github' local, tudo em disco temporario."""
        origem = self.dir / ("src" if nome == "web-app" else f"src-{nome}")
        sh("git", "init", "-q", "-b", "main", str(origem))
        (origem / "app.py").write_text("print('ola')\n")
        sh("git", "add", "-A", cwd=origem)
        sh("git", "commit", "-q", "-m", "inicial", cwd=origem)
        bare = self.cfg.repos_dir / f"{nome}.git"
        self.cfg.repos_dir.mkdir(parents=True, exist_ok=True)
        sh("git", "clone", "-q", "--mirror", str(origem), str(bare))
        gh = self.dir / f"github-{nome}.git"
        sh("git", "clone", "-q", "--bare", str(origem), str(gh))
        self.cfg.permitir_url_local = True
        self.cfg.root_state = self.dir / "root-state"
        self.cfg.repos[nome] = {"descricao": "x", "branch_base": "main", "github_url": str(gh)}
        return gh
