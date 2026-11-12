import sys
import unittest
if sys.platform == "win32":
    raise unittest.SkipTest('full Linux service security: root-owned install and process uid')

import io
import json
import os
import re
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from ai_agents_orchestrator import engines, cli
from tests.helpers import Base, TOKEN, CHAT
from ai_agents_orchestrator.engines import Resultado


class SemSegredos(Base):
    def test_log_notificacao_e_telegram_mascaram_segredos(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.svc.log("falha com GITHUB_TOKEN=ghp_" + "x" * 30 + " e senha de app: abcd efgh ijkl mnop")
            self.svc.notificar("resultado: OPENAI_API_KEY=sk-" + "Ab1" * 8)
        saida = buf.getvalue() + " ".join(self.tr.textos())
        self.assertNotIn("ghp_" + "x" * 30, saida)
        self.assertNotIn("abcd efgh ijkl mnop", saida)
        self.assertNotIn("sk-" + "Ab1" * 8, saida)

    def test_token_do_bot_nunca_aparece_em_mensagens(self):
        self.criar_repo()
        self.quadro.criar("T", "", "web-app")
        self.codex.respostas = [Resultado(ok=True, texto=f"vazou {TOKEN} aqui", segundos=1)]
        self.svc.ciclo_worker()
        self.assertFalse(any(TOKEN in x for x in self.tr.textos()))
        self.assertFalse(any(TOKEN in json.dumps(p) for m, p in self.tr.chamadas))

    def test_ambiente_dos_motores_e_minimo(self):
        os.environ["AAO_TELEGRAM_TOKEN"] = TOKEN
        try:
            e = engines.env_filhos(self.cfg.state_dir)
            self.assertFalse(any(TOKEN in v for v in e.values()))
            self.assertEqual(set(e) & {"AAO_TELEGRAM_TOKEN", "AAO_PUBLISH_KEY"}, set())
        finally:
            del os.environ["AAO_TELEGRAM_TOKEN"]

    def test_dry_run_nao_chama_codex_nem_telegram(self):
        os.environ["AAO_PATHS_STATE_DIR"] = str(self.cfg.state_dir)
        try:
            svc = cli.montar_servico(self.cfg, dry_run=True, sem_telegram=True)
            self.assertTrue(svc.sem_telegram)
            svc.notificar("oi")
            self.assertEqual(svc.saida, ["oi"])
            r = svc.codex.executar("x", self.dir)
            self.assertIn("dry-run", r.texto)
        finally:
            os.environ.pop("AAO_PATHS_STATE_DIR")
            svc.estado.c.close(); svc.quadro.c.close()


class HookPrompt(Base):
    def test_hook_so_imprime_quando_ha_recado(self):
        self.quadro.criar("Redesenhar tela", "d", "", "claude", "codex")
        os.environ["AAO_PATHS_STATE_DIR"] = str(self.cfg.state_dir)
        calls = []
        orig = cli.chamar
        def falso(cfg, req, timeout=30):
            calls.append(req["cmd"])
            return self.svc.comando(req)
        cli.chamar = falso
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                cli._hook_prompt(self.cfg, "UserPromptSubmit")
            self.assertNotIn("Redesenhar tela", buf.getvalue())
            self.assertIn("#1 state=pending", buf.getvalue())
            self.assertNotIn("recado_lido", calls)  # delivery atomically acknowledges only the delivered delta
            self.assertEqual(self.quadro.ver(1)["visto_claude"], 1)
            self.assertNotIn("claude_assumiu", calls)
            self.assertEqual(self.tr.enviados(), [])
            buf = io.StringIO()
            with redirect_stdout(buf):
                cli._hook_prompt(self.cfg, "UserPromptSubmit")  # ja lido: silencio (e lembrete so se briefing velho + trabalho)
            self.assertNotIn("Redesenhar tela", buf.getvalue())
        finally:
            cli.chamar = orig
            os.environ.pop("AAO_PATHS_STATE_DIR")

    def test_lembrete_do_briefing_no_maximo_a_cada_30_min(self):
        self.quadro.criar("em curso", "d", "", "codex", "dono")
        orig = cli.chamar
        cli.chamar = lambda cfg, req, timeout=30: self.svc.comando(req)
        try:
            def rodar():
                b = io.StringIO()
                with redirect_stdout(b):
                    cli._hook_prompt(self.cfg, "UserPromptSubmit")
                return b.getvalue()
            self.assertIn("BRIEFING.md", rodar())  # nao existe + ha trabalho
            self.assertEqual(rodar(), "")  # 2o em menos de 30 min: nada
            m = self.cfg.state_dir / "lembrete-ts"
            os.utime(m, (m.stat().st_mtime - 1900,) * 2)
            self.assertIn("BRIEFING.md", rodar())
        finally:
            cli.chamar = orig

    def test_sem_trabalho_sem_lembrete(self):
        orig = cli.chamar
        cli.chamar = lambda cfg, req, timeout=30: self.svc.comando(req)
        try:
            b = io.StringIO()
            with redirect_stdout(b):
                cli._hook_prompt(self.cfg, "SessionStart")
            self.assertEqual(b.getvalue(), "")
        finally:
            cli.chamar = orig


    def test_em_fallback_o_hook_aciona_claude_assumiu(self):
        self.quadro.criar("Redesenhar tela", "d", "", "claude", "codex")
        self.estado.set("fallback_ativo", True)
        calls = []
        orig = cli.chamar
        def falso(cfg, req, timeout=30):
            calls.append(req["cmd"])
            return self.svc.comando(req)
        cli.chamar = falso
        try:
            with redirect_stdout(io.StringIO()):
                cli._hook_prompt(self.cfg, "SessionStart")
            self.assertIn("claude_assumiu", calls)
            self.assertTrue(any("Claude took over" in x for x in self.tr.textos()))
        finally:
            cli.chamar = orig


class CliLeve(Base):
    def test_claude_limite_nunca_falha_nem_trava_o_hook(self):
        # servico fora do ar: o hook so repassa e sai com 0
        os.environ["AAO_SOCKET"] = str(self.dir / "nao.sock")
        os.environ["AAO_PATHS_STATE_DIR"] = str(self.cfg.state_dir)
        os.environ["AAO_ENV_FILE"] = str(self.dir / "sem-env")
        try:
            import sys
            ant = sys.stdin
            sys.stdin = io.StringIO('{"error":"rate_limit"}')
            self.assertEqual(cli.main(["claude-limite"]), 0)
            sys.stdin = ant
        finally:
            for k in ("AAO_SOCKET", "AAO_PATHS_STATE_DIR", "AAO_ENV_FILE"):
                os.environ.pop(k)


if __name__ == "__main__":
    unittest.main()


class SeparacaoDoCodigoRoot(unittest.TestCase):
    def setUp(self):
        import subprocess
        import tempfile
        from pathlib import Path
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = Path(self.tmp.name)
        self.src = self.d / "src"
        (self.src / "src" / "ai_agents_orchestrator").mkdir(parents=True)
        (self.src / "examples").mkdir()
        (self.src / "README.md").write_text("x")
        (self.src / "src" / "ai_agents_orchestrator" / "__init__.py").write_text("v=1\n")
        (self.src / "examples" / "repos.example.json").write_text("{}")
        self.git = lambda *a: subprocess.run(["git", "-C", str(self.src), "-c", "user.name=t", "-c", "user.email=t@t", *a],
                                              check=True, capture_output=True, text=True)
        self.git("init", "-q", "-b", "main")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "um")

    def test_instalar_copia_so_o_commit_sem_git_e_sem_escrita_de_grupo(self):
        from ai_agents_orchestrator.cli import instalar
        from ai_agents_orchestrator.config import codigo_inseguro
        (self.src / "src" / "ai_agents_orchestrator" / "__init__.py").write_text("v=2\n")  # sujo: recusa
        self.assertEqual(instalar(self.src, self.d / "opt", exigir_root=False), 1)
        self.git("checkout", "--", ".")
        self.assertEqual(instalar(self.src, self.d / "opt", exigir_root=False), 0)
        self.assertEqual((self.d / "opt" / "src" / "ai_agents_orchestrator" / "__init__.py").read_text(), "v=1\n")
        self.assertFalse((self.d / "opt" / ".git").exists())
        self.assertTrue((self.d / "opt" / "VERSION").read_text().startswith(self.git("rev-parse", "HEAD").stdout.strip()))
        self.assertEqual([p for p in codigo_inseguro(self.d / "opt") if not self._meu(p)], [])

    def _meu(self, p):  # nos testes o dono e o usuario atual, nao root
        import os
        return os.getuid() != 0

    def test_instalar_recusa_branch_que_nao_e_main(self):
        from ai_agents_orchestrator.cli import instalar
        self.git("checkout", "-q", "-b", "agent/chat-1")
        self.assertEqual(instalar(self.src, self.d / "opt", exigir_root=False), 1)
        self.assertFalse((self.d / "opt").exists())

    def test_instalar_exige_root(self):
        import os
        from ai_agents_orchestrator.cli import instalar
        if os.geteuid() != 0:
            self.assertEqual(instalar(self.src, self.d / "opt"), 1)

    @unittest.skipUnless(os.geteuid() == 0, "as non-root every file is already 'not root-owned'")
    def test_codigo_gravavel_por_grupo_e_inseguro(self):
        import os
        from ai_agents_orchestrator.config import codigo_inseguro
        arq = self.src / "src" / "ai_agents_orchestrator" / "__init__.py"
        os.chmod(arq, 0o664)
        self.assertTrue(any(p.endswith("__init__.py") for p in codigo_inseguro(self.src)))

    def test_repos_sem_remoto_nao_vao_ao_github(self):
        from ai_agents_orchestrator import workspace
        from ai_agents_orchestrator.config import Config
        cfg = Config(repos={"orchestrator": {"fonte": "/x", "local": True}, "site": {"github": "a/b"}})
        self.assertTrue(workspace.sem_remoto(cfg, "orchestrator"))
        self.assertFalse(workspace.sem_remoto(cfg, "site"))
        with self.assertRaises(workspace.GitErro):
            workspace.merge_e_push(cfg, "orchestrator", self.src, "agent/x", root=object())

    def test_exemplo_de_repos_carrega_e_normaliza_chaves(self):
        import json
        from pathlib import Path
        from ai_agents_orchestrator import config
        arq = Path(__file__).resolve().parent.parent / "examples" / "repos.example.json"
        self.assertTrue(json.loads(arq.read_text()))
        repos = config.carregar_repos({"repos_file": str(arq)})
        self.assertTrue(repos)
        for nome, spec in repos.items():
            self.assertIn("branch_base", spec, nome)  # English "base_branch" normalized
            self.assertTrue(spec.get("github") or spec.get("fonte") or spec.get("fonte_url"), nome)
        local = [s for s in repos.values() if s.get("local")]
        self.assertTrue(all("github" not in s for s in local))
