import json
import os
import socket
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path

from ai_agents_orchestrator import root_daemon as rd
from ai_agents_orchestrator.config import Config

CHAVE = b"k" * 32


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = Path(self.tmp.name)
        self.cfg = Config(state_dir=self.d / "st", repos={"web-app": {}, "api": {}})
        self.cfg.preparar_dirs()
        rd._usados.clear()

    def pedido(self, repo="web-app", acao="deploy", exp=None, chave=CHAVE, nonce="n1", commit="abc"):
        exp = exp or int(time.time()) + 300
        return {"repo": repo, "commit": commit, "acao": acao, "nonce": nonce, "exp": exp,
                "mac": rd.assinar(chave, repo, commit, acao, nonce, exp)}


class Publicar(Base):
    def setUp(self):
        super().setUp()
        self.marca = self.d / "rodou"
        self.script = self.d / "14-publicar.sh"
        self.script.write_text(f"#!/bin/bash\ntouch {self.marca}\n")
        os.chmod(self.script, 0o755)
        self.man = {"repos": {"web-app": {"deploy": {"argv": ["/usr/bin/touch", str(self.marca)], "timeout": 10}}, "api": {"deploy": None}},
                    "acoes_extras": {"restart-api": {"argv": ["/usr/bin/touch", str(self.marca) + "-api"]}},
                    "scripts": [str(self.script)]}

    def test_assinatura_valida_executa_so_o_comando_da_allowlist(self):
        r = rd.publicar(self.cfg, self.pedido(), CHAVE, self.man)
        self.assertTrue(r["ok"])
        self.assertTrue(self.marca.exists())
        self.assertIn("publicar", (self.cfg.state_dir / "auditoria-root.jsonl").read_text())

    def test_assinatura_invalida_expirado_e_replay(self):
        p = self.pedido()
        p["mac"] = "0" * 64
        self.assertEqual(rd.publicar(self.cfg, p, CHAVE, self.man)["erro"], "invalid signature")
        self.assertEqual(rd.publicar(self.cfg, self.pedido(exp=int(time.time()) - 5), CHAVE, self.man)["erro"], "request expired")
        self.assertEqual(rd.publicar(self.cfg, self.pedido(chave=b"x" * 32), CHAVE, self.man)["erro"], "invalid signature")
        self.assertTrue(rd.publicar(self.cfg, self.pedido(nonce="r1"), CHAVE, self.man)["ok"])
        self.assertEqual(rd.publicar(self.cfg, self.pedido(nonce="r1"), CHAVE, self.man)["erro"], "request already used")
        self.assertEqual(rd.publicar(self.cfg, {"repo": "x"}, CHAVE, self.man)["erro"], "malformed request")
        self.assertFalse(rd.publicar(self.cfg, self.pedido(nonce="v"), b"", self.man)["ok"])  # sem chave nada passa

    def test_acao_fora_da_allowlist_ou_repo_sem_deploy(self):
        self.assertEqual(rd.publicar(self.cfg, self.pedido(acao="rm -rf /", nonce="a"), CHAVE, self.man)["erro"], "action not in allowlist")
        self.assertEqual(rd.publicar(self.cfg, self.pedido(repo="api", nonce="b"), CHAVE, self.man)["erro"], "action not in allowlist")
        self.assertEqual(rd.publicar(self.cfg, self.pedido(repo="other-repo", nonce="c"), CHAVE, self.man)["erro"], "repo not allowed")
        self.assertFalse(self.marca.exists())

    @unittest.skipUnless(os.geteuid() == 0, "deploy scripts must be root-owned; needs root to create one")
    def test_script_so_se_listado_exato_e_nao_gravavel_por_outros(self):
        ok = rd.publicar(self.cfg, self.pedido(acao=f"script:{self.script}", nonce="s1"), CHAVE, self.man)
        self.assertTrue(ok["ok"])
        outro = self.d / "outro.sh"
        outro.write_text("#!/bin/bash\ntrue\n")
        os.chmod(outro, 0o755)
        self.assertEqual(rd.publicar(self.cfg, self.pedido(acao=f"script:{outro}", nonce="s2"), CHAVE, self.man)["erro"], "action not in allowlist")
        os.chmod(self.script, 0o777)  # gravavel por todos: recusa
        self.assertEqual(rd.publicar(self.cfg, self.pedido(acao=f"script:{self.script}", nonce="s3"), CHAVE, self.man)["erro"], "action not in allowlist")

    def test_acao_extra_nomeada(self):
        self.assertTrue(rd.publicar(self.cfg, self.pedido(acao="extra:restart-api", nonce="e1"), CHAVE, self.man)["ok"])
        self.assertEqual(rd.publicar(self.cfg, self.pedido(acao="extra:shell", nonce="e2"), CHAVE, self.man)["erro"], "action not in allowlist")

    def test_cliente_assina_como_o_daemon_verifica(self):
        c = rd.RootClient(chave_hex=CHAVE.hex())
        capt = {}
        c.chamar = lambda req, timeout=0: capt.update(req) or {"ok": True}
        c.publicar("web-app", "abc", "deploy")
        self.assertEqual(capt["mac"], rd.assinar(CHAVE, "web-app", "abc", "deploy", capt["nonce"], capt["exp"]))


class Processar(Base):
    def test_so_uid_do_agente_ou_root(self):
        self.assertEqual(rd.processar(self.cfg, {"op": "espelhar"}, 1234, 980, CHAVE)["erro"], "uid not authorized")
        self.assertEqual(rd.processar(self.cfg, {"op": "rm"}, 980, 980, CHAVE)["erro"], "unknown operation")

    def test_servidor_real_por_socket(self):
        sock = str(self.d / "r.sock")
        import pwd
        self.cfg.service_user = pwd.getpwuid(os.getuid()).pw_name  # CI runs as a normal user: it plays the service user
        t = threading.Thread(target=rd.servir, args=(self.cfg, sock, False), daemon=True)
        t.start()
        for _ in range(50):
            if os.path.exists(sock):
                break
            time.sleep(0.1)
        r = rd.RootClient(sock).chamar({"op": "nada"}, 5)
        self.assertEqual(r["erro"], "unknown operation")
        self.assertEqual(stat.S_IMODE(os.stat(sock).st_mode), 0o660)
        self.assertEqual(rd.RootClient(str(self.d / "naoexiste")).chamar({"op": "x"}, 1)["ok"], False)


class AcordarClaude(Base):
    def setUp(self):
        super().setUp()
        self.sess = self.d / "sessions"
        self.sess.mkdir()
        self.sock = str(self.d / "s.sock")
        self.recebido = []
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.srv.bind(self.sock)
        self.srv.listen(2)
        self.addCleanup(self.srv.close)
        threading.Thread(target=self._aceitar, daemon=True).start()

    def _aceitar(self):
        try:
            c, _ = self.srv.accept()
            dados = b""
            while True:
                p = c.recv(65536)
                if not p:
                    break
                dados += p
            self.recebido.append(dados)
        except OSError:
            pass

    def registra(self, **kw):
        d = {"pid": os.getpid(), "sessionId": "sid-1", "kind": "interactive", "name": "VPS", "updatedAt": 5,
             "messagingSocketPath": self.sock, **kw}
        (self.sess / f"{d['pid']}.json").write_text(json.dumps(d))

    def test_descobre_sessao_viva_interativa(self):
        self.registra()
        self.assertEqual(rd.sessao_claude(self.sess)["sessionId"], "sid-1")
        self.assertIsNone(rd.sessao_claude(self.sess, "outra"))
        (self.sess / f"{os.getpid()}.json").write_text(json.dumps({"pid": 99999999, "kind": "interactive", "messagingSocketPath": self.sock}))
        self.assertIsNone(rd.sessao_claude(self.sess))  # pid morto

    def test_entrega_a_mensagem_no_formato_do_cliente(self):
        self.registra()
        r = rd.acordar_claude("Limite voltou. Leia o recado.", sessoes=self.sess)
        self.assertTrue(r["ok"])
        for _ in range(30):
            if self.recebido:
                break
            time.sleep(0.1)
        msg = json.loads(self.recebido[0].decode())
        self.assertEqual((msg["type"], msg["session_id"], msg["parent_tool_use_id"]), ("user", "sid-1", None))
        self.assertTrue(msg["message"]["content"].startswith("<cross-session-message from="))
        self.assertIn("Limite voltou", msg["message"]["content"])
        self.assertEqual(msg["origin"]["kind"], "peer")
        self.assertTrue(self.recebido[0].endswith(b"\n"))

    def test_sem_sessao_ou_socket_morto(self):
        self.assertFalse(rd.acordar_claude("x", sessoes=self.sess)["ok"])
        self.registra(messagingSocketPath=str(self.d / "morto.sock"))
        self.assertFalse(rd.acordar_claude("x", sessoes=self.sess)["ok"])

    def test_mensagem_e_limitada(self):
        self.registra()
        rd.acordar_claude("a" * 9000, sessoes=self.sess)
        time.sleep(0.3)
        corpo = json.loads(self.recebido[0].decode())["origin"]["body"]
        self.assertEqual(len(corpo), 4000)


import subprocess

TOKEN_FALSO = "ghp_" + "Z" * 36


def _sh(*a, cwd=None, env_extra=None):
    env = {"PATH": os.environ["PATH"], "HOME": os.environ.get("TMPDIR", "/tmp"), "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x.com"}
    return subprocess.run(a, cwd=cwd, env=env, capture_output=True, text=True, check=True).stdout


class Push(Base):
    """Operacao push do daemon root, com remoto 'github' local (bare) e workspace real."""

    def setUp(self):
        super().setUp()
        rd._usados.clear()
        self.cfg.permitir_url_local = True
        self.cfg.root_state = self.d / "root-state"
        orig = self.d / "orig"
        _sh("git", "init", "-q", "-b", "main", str(orig))
        (orig / "a.txt").write_text("a\n")
        _sh("git", "add", "-A", cwd=orig)
        _sh("git", "commit", "-qm", "ini", cwd=orig)
        self.gh = self.d / "gh.git"
        _sh("git", "clone", "-q", "--bare", str(orig), str(self.gh))
        self.cfg.repos = {"docs": {"github_url": str(self.gh), "github": "example-org/docs", "branch_base": "main"}}
        self.ws = self.cfg.workspaces / "docs" / "7"
        self.ws.parent.mkdir(parents=True, exist_ok=True)
        _sh("git", "clone", "-q", str(self.gh), str(self.ws))
        _sh("git", "checkout", "-qb", "agent/7-x", cwd=self.ws)
        (self.ws / "b.txt").write_text("b\n")
        _sh("git", "add", "-A", cwd=self.ws)
        _sh("git", "commit", "-qm", "b", cwd=self.ws)

    def req(self, **kw):
        r = {"repo": "docs", "tid": 7, "branch": "agent/7-x", "modo": "branch"}
        r.update(kw)
        return r

    def assinado(self, nonce="n", branch="agent/7-x", chave=CHAVE, **kw):
        exp = int(time.time()) + 300
        r = self.req(modo="base", nonce=nonce, exp=exp, commit=branch, mac=rd.assinar(chave, "docs", branch, "push-base", nonce, exp))
        r.update(kw)
        return r

    def remoto(self, ref):
        p = subprocess.run(["git", "--git-dir", str(self.gh), "rev-parse", ref], capture_output=True, text=True)
        return p.stdout.strip() if p.returncode == 0 else None

    def audit(self):
        return (self.cfg.state_dir / "auditoria-root.jsonl").read_text()

    def test_push_de_branch_funciona_e_e_auditado(self):
        r = rd.push(self.cfg, self.req())
        self.assertTrue(r["ok"], r)
        self.assertEqual(self.remoto("refs/heads/agent/7-x"), _sh("git", "rev-parse", "HEAD", cwd=self.ws).strip())
        self.assertIn('"op": "push"', self.audit())
        self.assertIsNone(self.remoto("refs/tags/x"))

    def test_recusas(self):
        antes = self.remoto("refs/heads/main")
        for kw, msg in [({"repo": "empresa"}, "repo not allowed"), ({"branch": "main"}, "branch outside"),
                        ({"branch": "agent/../main"}, "branch outside"), ({"branch": "+agent/7-x"}, "branch outside"),
                        ({"branch": "refs/tags/v1"}, "branch outside"), ({"branch": "agent/a b"}, "branch outside"),
                        ({"branch": ""}, "branch outside"), ({"modo": "delete"}, "invalid mode"),
                        ({"tid": "../../x"}, "invalid tid"), ({"tid": 99}, "invalid workspace"),
                        ({"branch": "agent/nao-existe"}, "branch not found")]:
            r = rd.push(self.cfg, self.req(**kw))
            self.assertFalse(r["ok"], kw)
            self.assertIn(msg, r["erro"], kw)
        self.assertEqual(self.remoto("refs/heads/main"), antes)
        self.assertIn("denied", self.audit())

    def test_repo_fora_da_allowlist_fixa_nao_ganha_url_https(self):
        self.cfg.permitir_url_local = False  # production: only GitHub slugs in publish.push_allowlist
        self.cfg.push_allowlist = ["example-org/docs", "example-org/site"]
        self.cfg.repos["x"] = {"github": "example-org/outro", "branch_base": "main"}
        self.assertIsNone(rd.url_do_repo(self.cfg, "x"))
        self.assertIsNone(rd.url_do_repo(self.cfg, "nao"))
        self.cfg.repos["y"] = {"github": "example-org/docs", "github_url": "/tmp/x"}
        self.assertEqual(rd.url_do_repo(self.cfg, "y"), "https://github.com/example-org/docs.git")  # github_url ignored
        for g in self.cfg.push_allowlist:
            self.cfg.repos["z"] = {"github": g}
            self.assertEqual(rd.url_do_repo(self.cfg, "z"), f"https://github.com/{g}.git")
        self.cfg.push_allowlist = []  # empty allowlist (the default): nothing can be pushed
        self.assertIsNone(rd.url_do_repo(self.cfg, "y"))

    def test_nunca_force_branch_divergente_e_recusado_pelo_remoto(self):
        self.assertTrue(rd.push(self.cfg, self.req())["ok"])
        _sh("git", "reset", "-q", "--hard", "HEAD~1", cwd=self.ws)
        (self.ws / "c.txt").write_text("c\n")
        _sh("git", "add", "-A", cwd=self.ws)
        _sh("git", "commit", "-qm", "c", cwd=self.ws)
        antes = self.remoto("refs/heads/agent/7-x")
        r = rd.push(self.cfg, self.req())
        self.assertFalse(r["ok"])
        self.assertEqual(self.remoto("refs/heads/agent/7-x"), antes)  # sem force, o remoto fica intacto

    def test_base_exige_assinatura_e_faz_merge_fast_forward(self):
        r = rd.push(self.cfg, self.req(modo="base"), CHAVE)
        self.assertEqual(r["erro"], "malformed request")
        self.assertEqual(rd.push(self.cfg, self.assinado(chave=b"x" * 32), CHAVE)["erro"], "invalid signature")
        self.assertEqual(rd.push(self.cfg, self.assinado(), b"")["erro"], "invalid signature")
        main0 = self.remoto("refs/heads/main")
        r = rd.push(self.cfg, self.assinado(nonce="ok1"), CHAVE)
        self.assertTrue(r["ok"], r)
        novo = self.remoto("refs/heads/main")
        self.assertNotEqual(novo, main0)
        self.assertEqual(subprocess.run(["git", "--git-dir", str(self.gh), "merge-base", "--is-ancestor", main0, novo]).returncode, 0)
        self.assertEqual(rd.push(self.cfg, self.assinado(nonce="ok1"), CHAVE)["erro"], "request already used")
        self.assertIsNone(self.remoto("refs/heads/agent/7-x"))  # modo base nao cria o branch

    def test_base_remota_avancou_integra_sobre_a_base_atual_e_conflito_nao_publica(self):
        outro = self.d / "outro"
        _sh("git", "clone", "-q", str(self.gh), str(outro))
        (outro / "z.txt").write_text("z\n")
        _sh("git", "add", "-A", cwd=outro)
        _sh("git", "commit", "-qm", "z", cwd=outro)
        _sh("git", "push", "-q", "origin", "main", cwd=outro)
        topo = self.remoto("refs/heads/main")
        self.assertTrue(rd.push(self.cfg, self.assinado(nonce="a"), CHAVE)["ok"])
        self.assertEqual(subprocess.run(["git", "--git-dir", str(self.gh), "merge-base", "--is-ancestor", topo, "main"]).returncode, 0)
        # conflito
        (outro / "a.txt").write_text("remoto\n")
        _sh("git", "commit", "-qam", "r", cwd=outro)
        _sh("git", "pull", "-q", "--no-rebase", "origin", "main", cwd=outro)
        _sh("git", "push", "-q", "origin", "main", cwd=outro)
        (self.ws / "a.txt").write_text("agente\n")
        _sh("git", "commit", "-qam", "conf", cwd=self.ws)
        antes = self.remoto("refs/heads/main")
        r = rd.push(self.cfg, self.assinado(nonce="b"), CHAVE)
        self.assertFalse(r["ok"])
        self.assertIn("conflict", r["erro"])
        self.assertEqual(self.remoto("refs/heads/main"), antes)

    def test_token_nunca_aparece_em_saida_nem_auditoria(self):
        self.assertEqual(rd._limpar(f"fatal: https://x-access-token:{TOKEN_FALSO}@github.com/a password={TOKEN_FALSO} {TOKEN_FALSO}").count("Z" * 10), 0)
        # remoto inexistente: a mensagem de erro do git passa por _limpar e a auditoria nao carrega segredo
        self.cfg.repos["docs"]["github_url"] = str(self.d / "nao-existe.git")
        r = rd.push(self.cfg, self.req())
        self.assertFalse(r["ok"])
        self.assertNotIn("ghp_", json.dumps(r) + self.audit())
        # o daemon nunca le o arquivo de segredos do root: o helper do git e quem le, dentro do processo git
        import inspect
        self.assertNotIn("/root/.env", inspect.getsource(rd))

    def test_processar_roteia_push_e_exige_uid_do_agente(self):
        self.assertEqual(rd.processar(self.cfg, self.req(op="push"), 12345, 980, CHAVE)["erro"], "uid not authorized")
        r = rd.processar(self.cfg, self.req(op="push"), 980, 980, CHAVE)
        self.assertTrue(r["ok"], r)


if __name__ == "__main__":
    unittest.main()
