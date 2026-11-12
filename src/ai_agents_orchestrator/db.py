"""SQLite persistence: service state (estado.db) and the shared task board (quadro.db).

Task states (internal names): pendente=pending, em_andamento=in progress, em_revisao=in review,
concluida=completed, bloqueada=blocked, cancelada=cancelled. Owners (dono): claude | codex.
Origins (origem): claude (CLI delegate/review), dono (owner via chat), handoff (taken over on Claude's limit).
"""
from __future__ import annotations

import fnmatch
import json
import secrets
import sqlite3
import threading
import time
from pathlib import Path

ESTADOS = ("pendente", "em_andamento", "em_revisao", "concluida", "bloqueada", "cancelada")
ESTADOS_ATIVOS = ("em_andamento",)
ESTADOS_FINAIS = ("concluida", "cancelada")
DONOS = ("claude", "codex")


def _conn(caminho: Path) -> sqlite3.Connection:
    c = sqlite3.connect(str(caminho), check_same_thread=False, timeout=30, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=30000")
    return c


class Estado:
    """kv + pending buttons + run log (for hourly limits and /usage)."""

    def __init__(self, caminho: Path):
        self.caminho = Path(caminho)
        self._lock = threading.RLock()
        self.c = _conn(self.caminho)
        with self._lock:
            self.c.executescript("""
            CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS acoes (
              id TEXT PRIMARY KEY, grupo TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL,
              op TEXT NOT NULL, criado REAL NOT NULL, expira REAL NOT NULL, usado INTEGER NOT NULL DEFAULT 0,
              msg_id INTEGER);
            CREATE INDEX IF NOT EXISTS acoes_grupo ON acoes(grupo);
            CREATE TABLE IF NOT EXISTS execucoes (
              id INTEGER PRIMARY KEY AUTOINCREMENT, quando REAL NOT NULL, tipo TEXT NOT NULL,
              motor TEXT NOT NULL, segundos REAL NOT NULL DEFAULT 0, ok INTEGER NOT NULL DEFAULT 1, ref TEXT);
            """)
            cols = {r["name"] for r in self.c.execute("PRAGMA table_info(execucoes)")}
            for nome, tipo in (("tokens_in", "INTEGER NOT NULL DEFAULT 0"),
                               ("tokens_out", "INTEGER NOT NULL DEFAULT 0"), ("usage_key", "TEXT")):
                if nome not in cols:
                    self.c.execute(f"ALTER TABLE execucoes ADD COLUMN {nome} {tipo}")
            self.c.execute("CREATE UNIQUE INDEX IF NOT EXISTS execucoes_usage ON execucoes(usage_key)")
            self.c.execute("CREATE INDEX IF NOT EXISTS execucoes_quando ON execucoes(quando)")
        try:
            self.caminho.chmod(0o600)
        except OSError:
            pass

    # ---- kv
    def get(self, k: str, padrao=None):
        with self._lock:
            r = self.c.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return json.loads(r["v"]) if r else padrao

    def set(self, k: str, v) -> None:
        with self._lock:
            self.c.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                           (k, json.dumps(v, ensure_ascii=False)))

    def delete(self, k: str) -> None:
        with self._lock:
            self.c.execute("DELETE FROM kv WHERE k=?", (k,))

    def incr_dia(self, nome: str, hoje: str) -> int:
        """Per-day counter (e.g. Codex limit checks)."""
        with self._lock:
            d = self.get(nome, {})
            if d.get("dia") != hoje:
                d = {"dia": hoje, "n": 0}
            d["n"] += 1
            self.set(nome, d)
            return d["n"]

    def contagem_dia(self, nome: str, hoje: str) -> int:
        d = self.get(nome, {})
        return d.get("n", 0) if d.get("dia") == hoje else 0

    # ---- botoes
    def nova_acao(self, kind: str, payload: dict, ops: list[str], expira_seg: int, agora: float | None = None) -> dict[str, str]:
        """Creates a group of buttons: one id per option. Returns {op: id}."""
        agora = agora if agora is not None else time.time()
        grupo = secrets.token_hex(4)
        ids: dict[str, str] = {}
        with self._lock:
            for op in ops:
                aid = secrets.token_hex(4)
                self.c.execute("INSERT INTO acoes(id,grupo,kind,payload,op,criado,expira) VALUES(?,?,?,?,?,?,?)",
                               (aid, grupo, kind, json.dumps(payload, ensure_ascii=False), op, agora, agora + expira_seg))
                ids[op] = aid
        return ids

    def acao(self, aid: str):
        with self._lock:
            return self.c.execute("SELECT * FROM acoes WHERE id=?", (aid,)).fetchone()

    def consumir_acao(self, aid: str, agora: float | None = None):
        """Atomically marks the GROUP as used. Returns the row or (None, reason)."""
        agora = agora if agora is not None else time.time()
        with self._lock:
            r = self.acao(aid)
            if not r:
                return None, "desconhecida"
            if r["usado"]:
                return None, "usada"
            if r["expira"] < agora:
                return None, "expirada"
            cur = self.c.execute("UPDATE acoes SET usado=1 WHERE grupo=? AND usado=0", (r["grupo"],))
            if cur.rowcount == 0:
                return None, "usada"
            return r, "ok"

    def limpar_acoes(self, agora: float | None = None) -> None:
        agora = agora if agora is not None else time.time()
        with self._lock:
            self.c.execute("DELETE FROM acoes WHERE expira < ?", (agora - 3600,))

    # ---- runs
    def registrar_execucao(self, tipo: str, motor: str, segundos: float, ok: bool, ref: str = "", agora: float | None = None,
                           *, tokens_in: int = 0, tokens_out: int = 0, usage_key: str | None = None) -> None:
        with self._lock:
            self.c.execute("""INSERT INTO execucoes(quando,tipo,motor,segundos,ok,ref,tokens_in,tokens_out,usage_key)
                           VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(usage_key) DO UPDATE SET
                           tokens_in=MAX(tokens_in,excluded.tokens_in), tokens_out=MAX(tokens_out,excluded.tokens_out)""",
                           (agora if agora is not None else time.time(), tipo, motor, segundos, int(ok), ref,
                            tokens_in, tokens_out, usage_key))

    def tokens_entre(self, desde: float, ate: float) -> dict:
        with self._lock:
            rows = self.c.execute("""SELECT motor,SUM(tokens_in) AS tokens_in,SUM(tokens_out) AS tokens_out
                                   FROM execucoes WHERE quando>=? AND quando<=? GROUP BY motor""", (desde, ate)).fetchall()
        return {r["motor"]: {k: r[k] for k in ("tokens_in", "tokens_out")} for r in rows}

    def ultimas_execucoes(self, limite: int = 10) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self.c.execute("""SELECT id,quando,tipo,motor,ref,tokens_in,tokens_out
                                                    FROM execucoes ORDER BY id DESC LIMIT ?""", (limite,))]

    def execucoes_desde(self, desde: float, tipo: str | None = None) -> list[sqlite3.Row]:
        with self._lock:
            if tipo:
                return self.c.execute("SELECT * FROM execucoes WHERE quando>=? AND tipo=?", (desde, tipo)).fetchall()
            return self.c.execute("SELECT * FROM execucoes WHERE quando>=?", (desde,)).fetchall()


# ---------------------------------------------------------------- board
def padroes_colidem(a: list[str], b: list[str]) -> list[str]:
    """Patterns of `a` colliding with any of `b`. An empty list means the whole repo."""
    if not a or not b:
        return ["*"]
    achados = []
    for x in a:
        for y in b:
            xn, yn = x.rstrip("/"), y.rstrip("/")
            if (fnmatch.fnmatch(xn, yn) or fnmatch.fnmatch(yn, xn) or xn == yn
                    or xn.startswith(yn + "/") or yn.startswith(xn + "/")):
                achados.append(x if x == y else f"{x} ~ {y}")
    return achados


class Quadro:
    """Shared Claude <-> Codex board."""

    def __init__(self, caminho: Path):
        self.caminho = Path(caminho)
        self._lock = threading.RLock()
        self.c = _conn(self.caminho)
        with self._lock:
            self.c.executescript("""
            CREATE TABLE IF NOT EXISTS tarefas (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              titulo TEXT NOT NULL, descricao TEXT NOT NULL DEFAULT '', repo TEXT NOT NULL DEFAULT '',
              kind TEXT NOT NULL DEFAULT 'dev',
              dono TEXT NOT NULL DEFAULT 'codex', estado TEXT NOT NULL DEFAULT 'pendente',
              origem TEXT NOT NULL DEFAULT 'dono',
              branch TEXT, patch TEXT, resumo TEXT, testes TEXT, erro TEXT,
              arquivos TEXT NOT NULL DEFAULT '[]', alterados TEXT NOT NULL DEFAULT '[]',
              depende TEXT NOT NULL DEFAULT '[]', alvo TEXT, thread_id TEXT, instrucoes TEXT,
              visto_claude INTEGER NOT NULL DEFAULT 1, aviso TEXT, workspace TEXT, publicado TEXT,
              criado REAL NOT NULL, atualizado REAL NOT NULL, inicio REAL, fim REAL);
            """)
            # safe migration of old databases: only adds the missing column; a task without origin = claude
            # (the quiet side: no chat notifications)
            cols = {r["name"] for r in self.c.execute("PRAGMA table_info(tarefas)").fetchall()}
            if "origem" not in cols:
                self.c.execute("ALTER TABLE tarefas ADD COLUMN origem TEXT NOT NULL DEFAULT 'claude'")
            for nome, tipo in (("claude_thread_id", "TEXT"), ("motor_execucao", "TEXT"),
                               ("retomada", "TEXT"), ("prioridade", "INTEGER NOT NULL DEFAULT 0"),
                               ("retomadas", "INTEGER NOT NULL DEFAULT 0"), ("diffstat", "TEXT")):
                if nome not in cols:
                    self.c.execute(f"ALTER TABLE tarefas ADD COLUMN {nome} {tipo}")
            self.c.execute("UPDATE tarefas SET origem='claude' WHERE origem IS NULL OR origem=''")
        try:
            self.caminho.chmod(0o600)
        except OSError:
            pass

    @staticmethod
    def _d(r):
        if r is None:
            return None
        d = dict(r)
        for k in ("arquivos", "alterados", "depende"):
            d[k] = json.loads(d[k] or "[]")
        return d

    def criar(self, titulo: str, descricao: str = "", repo: str = "", dono: str = "codex", origem: str = "dono",
              arquivos: list[str] | None = None, depende: list[int] | None = None, kind: str = "dev",
              alvo: str | None = None, estado: str = "pendente", instrucoes: str | None = None,
              agora: float | None = None, prioridade: int = 0) -> dict:
        assert dono in DONOS and estado in ESTADOS
        agora = agora if agora is not None else time.time()
        with self._lock:
            cur = self.c.execute(
                "INSERT INTO tarefas(titulo,descricao,repo,dono,estado,origem,arquivos,depende,kind,alvo,instrucoes,visto_claude,criado,atualizado,prioridade)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (titulo.strip()[:200], descricao, repo, dono, estado, origem, json.dumps(arquivos or []),
                 json.dumps(depende or []), kind, alvo, instrucoes,
                 0 if dono == "claude" and origem == "codex" else 1, agora, agora, int(prioridade)))
            return self.ver(cur.lastrowid)

    def ver(self, tid: int) -> dict | None:
        with self._lock:
            return self._d(self.c.execute("SELECT * FROM tarefas WHERE id=?", (tid,)).fetchone())

    def listar(self, estados: tuple[str, ...] | None = None, dono: str | None = None, limite: int = 50) -> list[dict]:
        q, p = "SELECT * FROM tarefas WHERE 1=1", []
        if estados:
            q += f" AND estado IN ({','.join('?' * len(estados))})"
            p += list(estados)
        if dono:
            q += " AND dono=?"
            p.append(dono)
        q += " ORDER BY id DESC LIMIT ?"
        p.append(limite)
        with self._lock:
            return [self._d(r) for r in self.c.execute(q, p).fetchall()]

    def atualizar(self, tid: int, agora: float | None = None, **campos) -> dict | None:
        if not campos:
            return self.ver(tid)
        agora = agora if agora is not None else time.time()
        for k in ("arquivos", "alterados", "depende"):
            if k in campos and not isinstance(campos[k], str):
                campos[k] = json.dumps(campos[k])
        if "estado" in campos:
            assert campos["estado"] in ESTADOS
        if "dono" in campos:
            assert campos["dono"] in DONOS
        cols = ",".join(f"{k}=?" for k in campos)
        with self._lock:
            self.c.execute(f"UPDATE tarefas SET {cols}, atualizado=? WHERE id=?", (*campos.values(), agora, tid))
            return self.ver(tid)

    # ---- locks / conflicts
    def ativas(self, ignorar: int | None = None) -> list[dict]:
        """Tasks holding a lock: in progress (any owner)."""
        with self._lock:
            rs = self.c.execute("SELECT * FROM tarefas WHERE estado IN ('em_andamento') AND kind='dev'").fetchall()
        return [self._d(r) for r in rs if ignorar is None or r["id"] != ignorar]

    def conflitos(self, tid: int | None, repo: str, arquivos: list[str]) -> list[dict]:
        """Active tasks in the same repo whose files collide with `arquivos`."""
        saida = []
        for t in self.ativas(ignorar=tid):
            if t["repo"] != repo:
                continue
            c = padroes_colidem(arquivos, t["arquivos"] or t["alterados"])
            if c:
                saida.append({"id": t["id"], "titulo": t["titulo"], "dono": t["dono"], "colisao": c[:5]})
        return saida

    def proxima_para_codex(self, repos_ok: set[str] | None = None) -> dict | None:
        """First pending Codex task whose dependencies are done and with no conflicting lock."""
        with self._lock:
            rs = self.c.execute("SELECT * FROM tarefas WHERE estado='pendente' AND dono='codex' ORDER BY prioridade DESC, id").fetchall()
        for r in rs:
            t = self._d(r)
            if repos_ok is not None and t["repo"] and t["repo"] not in repos_ok:
                continue
            if any((self.ver(d) or {}).get("estado") not in ("concluida", "em_revisao") for d in t["depende"]):
                continue
            if t["kind"] == "dev" and t["repo"] and self.conflitos(t["id"], t["repo"], t["arquivos"]):
                continue
            return t
        return None

    def recado_pendente(self) -> list[dict]:
        """What Claude has not seen yet: Codex results and dono=claude proposals."""
        with self._lock:
            rs = self.c.execute("SELECT * FROM tarefas WHERE visto_claude=0 AND estado!='cancelada' ORDER BY id").fetchall()
        return [self._d(r) for r in rs]

    def marcar_visto(self, ids: list[int] | None = None) -> int:
        with self._lock:
            if ids is None:
                cur = self.c.execute("UPDATE tarefas SET visto_claude=1 WHERE visto_claude=0")
            else:
                cur = self.c.execute(f"UPDATE tarefas SET visto_claude=1 WHERE id IN ({','.join('?' * len(ids))})", ids) if ids else None
            return cur.rowcount if cur else 0
