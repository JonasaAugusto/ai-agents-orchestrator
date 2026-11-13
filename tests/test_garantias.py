"""Garantias do servico: sincronizacao obrigatoria, snapshot em todo prompt, estado honesto e gate de entrega."""
import json
from datetime import date
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ai_agents_orchestrator import cli, config, engines, espelho, garantias, prompts, workspace
from ai_agents_orchestrator.config import Config
from ai_agents_orchestrator.engines import Resultado
from tests.helpers import Base, sh, needs_root

REL_OK = "Estado: concluida\nFeito: x. Testes: ok. Falta: nada. Para o Claude: nada. Riscos: nenhum."


def escreve(texto, arquivos=None):
    def f(prompt, cwd):
        for nome, conteudo in (arquivos or {"novo.py": "print('feito')\n"}).items():
            p = Path(cwd) / nome
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(conteudo)
        return Resultado(ok=True, texto=texto, thread_id="T1", segundos=1)
    return f


def sha_main(bare: Path) -> str:
    return sh("git", "--git-dir", str(bare), "rev-parse", "refs/heads/main").strip()


class Sincronizacao(Base):
    def test_falha_na_atualizacao_bloqueia_a_tarefa_sem_rodar_o_codex(self):
        self.criar_repo()
        self.root.espelho_status["web-app"] = {"ok": False, "erro": "sem rede"}
        t = self.quadro.criar("tarefa", "d", "web-app")
        self.svc.ciclo_worker()
        v = self.quadro.ver(t["id"])
        self.assertEqual(v["estado"], "bloqueada")
        self.assertIn("mirror", v["erro"])
        self.assertEqual(self.codex.chamadas, [])
        self.assertFalse((self.cfg.workspaces / "web-app" / str(t["id"])).exists())
        self.assertIn("blocked", self.tr.textos()[-1])
        self.assertNotIn("Publish", [r for r, _ in self.botoes_enviados()])

    def test_sha_divergente_do_github_bloqueia(self):
        self.criar_repo()
        self.root.espelho_status["web-app"] = {"ok": True, "status": {"ok": True, "sha_origem": "f" * 40, "confirmado": True}}
        t = self.quadro.criar("tarefa", "d", "web-app")
        self.svc.ciclo_worker()
        v = self.quadro.ver(t["id"])
        self.assertEqual(v["estado"], "bloqueada")
        self.assertIn("GitHub", v["erro"])
        self.assertEqual(self.codex.chamadas, [])

    def test_espelho_desatualizado_ou_nao_confirmado_ou_daemon_fora_bloqueiam(self):
        self.criar_repo()
        casos = [{"ok": True, "status": {"ok": True, "desatualizado": True, "motivo": "GitHub indisponivel"}},
                 {"ok": True, "status": {"ok": True, "confirmado": False}},
                 OSError("socket")]
        for caso in casos:
            self.root.espelho_status["web-app"] = caso
            t = self.quadro.criar("tarefa", "d", "web-app")
            self.svc.ciclo_worker()
            self.assertEqual(self.quadro.ver(t["id"])["estado"], "bloqueada", caso)
        self.assertEqual(self.codex.chamadas, [])

    def test_sucesso_registra_sha_de_cada_repo_inclusive_docs(self):
        self.criar_repo("web-app")
        self.criar_repo("docs")
        self.codex.respostas = [escreve(REL_OK)]
        t = self.quadro.criar("tarefa", "atualize tambem a docs", "web-app")
        self.svc.ciclo_worker()
        self.assertEqual(self.quadro.ver(t["id"])["estado"], "em_revisao")
        snap = json.loads((self.cfg.resultados / str(t["id"]) / "snapshot.json").read_text())
        self.assertEqual(sorted(snap["sincronizados"]), ["docs", "web-app"])
        for repo in ("web-app", "docs"):
            sha = sha_main(self.cfg.repos_dir / f"{repo}.git")
            self.assertEqual(snap["sincronizados"][repo], sha)
            self.assertIn(sha[:12], self.codex.chamadas[0]["prompt"])
        self.assertIn(("espelhar_repo", "docs"), self.root.chamadas)
        handoff = (self.cfg.resultados / str(t["id"]) / "HANDOFF.md").read_text()
        self.assertIn("docs=", handoff)

    def test_chat_com_espelho_velho_bloqueia_sem_cair_em_leitura(self):
        self.criar_repo()
        self.root.espelho_status["web-app"] = {"ok": False, "erro": "sem rede"}
        self.bot.processar(self.msg("ajusta o web-app"))
        self.assertEqual(self.codex.chamadas, [])
        self.assertEqual(self.quadro.listar()[0]["estado"], "bloqueada")
        self.assertIn("did not open the editing session", self.tr.textos()[-1])

    def test_retomada_tambem_exige_sincronizacao(self):
        self.criar_repo()
        self.codex.respostas = [Resultado(ok=False, erro="o Codex foi interrompido pelo SIGTERM", exit_code=-15, thread_id="T7")]
        t = self.quadro.criar("tarefa", "d", "web-app")
        self.svc.ciclo_worker()
        self.assertEqual(self.quadro.ver(t["id"])["estado"], "pendente")
        self.root.espelho_status["web-app"] = {"ok": False, "erro": "sem rede"}
        n = len(self.codex.chamadas)
        self.svc.ciclo_worker()
        self.assertEqual(self.quadro.ver(t["id"])["estado"], "bloqueada")
        self.assertEqual(len(self.codex.chamadas), n)

    @needs_root
    def test_espelho_registra_sha_e_origem(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            gh = d / "gh"
            sh("git", "init", "-q", "-b", "main", str(gh))
            (gh / "a.py").write_text("x")
            sh("git", "add", "-A", cwd=gh)
            sh("git", "commit", "-q", "-m", "c", cwd=gh)
            cfg = Config(state_dir=d / "st", root_state=d / "root", repos={"r": {"github": "X/y", "branch_base": "main"}})
            with mock.patch.object(espelho, "_url_github", lambda s: str(gh)):
                st = espelho.atualizar_repos(cfg)["r"]
            self.assertTrue(st["ok"])
            self.assertEqual(st["sha"], sh("git", "rev-parse", "main", cwd=gh).strip())
            self.assertEqual(st["sha_origem"], st["sha"])
            self.assertTrue(st["confirmado"])
            with mock.patch.object(espelho, "_url_github", lambda s: str(d / "nao-existe")):  # sem origem alcancavel: sem confirmacao
                self.assertFalse(espelho.atualizar_repos(cfg)["r"].get("confirmado"))


class Snapshot(Base):
    def _briefing(self, texto):
        p = self.svc.briefing_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(texto, encoding="utf-8")

    def test_prompt_de_tarefa_traz_horario_sha_e_capacidades(self):
        self.criar_repo()
        self.codex.respostas = [escreve(REL_OK)]
        self.quadro.criar("tarefa", "d", "web-app")
        self.cfg.codex_full_access = True  # explicit opt-in (codex.full_access)
        self.svc.ciclo_worker()
        p = self.codex.chamadas[0]["prompt"]
        self.assertIn("EXECUTION SNAPSHOT", p)
        self.assertIn("Time (America/Montevideo)", p)
        self.assertIn("uid=", p)
        self.assertIn("sandbox=danger-full-access (configured workspace-write)", p)
        self.assertIn("network=yes", p)
        self.assertIn("model=", p)
        self.assertIn("effort=", p)
        self.assertIn(sha_main(self.cfg.repos_dir / "web-app.git")[:12], p)

    def test_snapshot_em_contexto_chat_conversa_e_retomada(self):
        self.criar_repo()
        self.svc.claude_limite(manual=True)
        self.assertIn("EXECUTION SNAPSHOT", self.codex.chamadas[0]["prompt"])
        self.assertIn("sandbox=read-only", self.codex.chamadas[0]["prompt"])
        self.codex.chamadas.clear()
        self.codex.respostas = [escreve("Estado: concluida\nok")]
        self.bot.processar(self.msg("ajusta o web-app"))
        self.assertIn("EXECUTION SNAPSHOT", self.codex.chamadas[0]["prompt"])
        self.bot.processar(self.msg("e agora?"))
        self.assertIn("EXECUTION SNAPSHOT", self.codex.chamadas[1]["prompt"])  # ate a mensagem seguinte da sessao
        # retomada de tarefa com instrucoes do dono
        t = self.quadro.criar("tarefa", "d", "web-app")
        self.codex.respostas = [Resultado(ok=False, erro="x", exit_code=-15, thread_id="T8")]
        self.svc.ciclo_worker()
        self.quadro.atualizar(t["id"], instrucoes="continue do ponto X")
        self.codex.respostas = [escreve(REL_OK)]
        self.svc.ciclo_worker()
        ultimo = self.codex.chamadas[-1]
        self.assertEqual(ultimo["resume"], "T8")
        self.assertIn("EXECUTION SNAPSHOT", ultimo["prompt"])
        self.assertIn("continue do ponto X", ultimo["prompt"])
        self.root.espelho_status.clear()
        self.cfg.repos = {}
        self.bot.processar(self.msg("oi, tudo bem?"))
        self.assertIn("EXECUTION SNAPSHOT", self.codex.chamadas[-1]["prompt"])

    def test_idade_do_briefing_vem_do_horario_declarado_nao_do_mtime(self):
        agora = 1_800_000_000.0
        dec = garantias.datetime.fromtimestamp(agora - 10 * 3600, config.fuso()).strftime("%Y-%m-%d %H:%M")
        r = garantias.avisos_briefing(f"# B\nAtualizado em: {dec}.\n", agora, {})
        self.assertEqual(r["idade_min"], 600)
        self.assertTrue(any("stale" in a for a in r["avisos"]))
        fresco = garantias.datetime.fromtimestamp(agora - 30 * 60, config.fuso()).strftime("%Y-%m-%d %H:%M")
        self.assertEqual(garantias.avisos_briefing(f"Atualizado em: {fresco}\n", agora, {})["avisos"], [])

    def test_briefing_com_horario_futuro_ou_sem_horario_ou_sha_divergente(self):
        agora = 1_800_000_000.0
        fut = garantias.datetime.fromtimestamp(agora + 3 * 3600, config.fuso()).strftime("%Y-%m-%d %H:%M")
        self.assertTrue(any("FUTURE" in a for a in garantias.avisos_briefing(f"Atualizado em: {fut}\n", agora, {})["avisos"]))
        self.assertTrue(any("does not declare" in a for a in garantias.avisos_briefing("sem horario\n", agora, {})["avisos"]))
        ok = garantias.datetime.fromtimestamp(agora, config.fuso()).strftime("%Y-%m-%d %H:%M")
        shas = {"web-app": "a" * 40, "site": "b" * 40}
        txt = f"Atualizado em: {ok}\n- web-app esta em aaaaaaa (ok)\n- site @ 1234567 (velho)\n"
        av = garantias.avisos_briefing(txt, agora, shas)["avisos"]
        self.assertEqual(len(av), 1)
        self.assertIn("site", av[0])
        self.assertIn("1234567", av[0])

    def test_briefing_velho_avisa_no_prompt_e_no_resultado(self):
        self.criar_repo()
        velho = garantias.datetime.fromtimestamp(self.t[0] - 8 * 3600, config.fuso()).strftime("%Y-%m-%d %H:%M")
        self._briefing(f"# BRIEFING\nAtualizado em: {velho}.\n")  # mtime = agora, declarado = 8 h atras
        self.codex.respostas = [escreve(REL_OK)]
        t = self.quadro.criar("tarefa", "d", "web-app")
        self.svc.ciclo_worker()
        self.assertIn("WARNING: the briefing was updated", self.codex.chamadas[0]["prompt"])
        v = self.quadro.ver(t["id"])
        self.assertIn("stale", v["aviso"])
        self.assertIn("SNAPSHOT WARNING", v["resumo"])
        self.assertIn("SNAPSHOT WARNING", (self.cfg.resultados / str(t["id"]) / "resumo.txt").read_text())
        self.assertTrue(self.svc.briefing_info()["velho"])
        self.assertEqual(self.svc.briefing_info()["fonte_idade"], "declarado")


class EstadoHonesto(Base):
    def _rodar(self, relatorio, origem="dono"):
        self.criar_repo()
        self.codex.respostas = [escreve(relatorio)]
        t = self.quadro.criar("tarefa", "d", "web-app", origem=origem)
        self.svc.ciclo_worker()
        return self.quadro.ver(t["id"]), self.cfg.resultados / str(t["id"])

    def test_processo_normal_com_relatorio_bloqueado_nao_vira_revisao(self):
        v, out = self._rodar("Estado: bloqueada\nFeito: nada. Falta: acesso ao banco. Para o Claude: liberar.")
        self.assertEqual(v["estado"], "bloqueada")
        self.assertIn("block", v["erro"])
        self.assertNotIn("Publish", [r for r, _ in self.botoes_enviados()])
        self.assertIn("Estado: bloqueada", (out / "HANDOFF.md").read_text())
        self.assertNotIn("Estado: ok", (out / "HANDOFF.md").read_text())
        self.assertIn("blocked", self.tr.textos()[-1])

    def test_relatorio_parcial_fica_em_revisao_sem_botao_publicar(self):
        v, out = self._rodar("Estado: parcial\nFeito: metade. Falta: o resto.")
        self.assertEqual(v["estado"], "em_revisao")
        self.assertIn("PARTIAL", v["aviso"])
        self.assertIn("Estado: parcial", (out / "HANDOFF.md").read_text())
        rotulos = [r for r, _ in self.botoes_enviados()]
        self.assertNotIn("Publish", rotulos)
        self.assertIn("Review first", rotulos)

    def test_relatorio_concluido_ou_sem_campo_segue_como_antes(self):
        v, out = self._rodar(REL_OK)
        self.assertEqual(v["estado"], "em_revisao")
        self.assertIn("Estado (state): ok", (out / "HANDOFF.md").read_text())
        self.assertIn("Publish", [r for r, _ in self.botoes_enviados()])

    def test_parser_do_estado(self):
        e = garantias.estado_relatorio
        self.assertEqual(e("Estado: Bloqueada"), "bloqueada")
        self.assertEqual(e("**Estado:** concluída\nx"), "concluida")
        self.assertEqual(e("blabla\n- Status: parcial\n"), "parcial")
        self.assertIsNone(e("Feito: tudo. O estado do banco: ruim"))
        self.assertIsNone(e("sem campo"))

    def test_prompt_pede_o_campo_de_estado(self):
        p = prompts.prompt_tarefa({"id": 1, "titulo": "t", "repo": "r"}, branch="b", base="main", briefing_tarefa=False)
        self.assertIn("Status: blocked", p)
        for linha, estado in (("Status: completed", "concluida"), ("Status: partial", "parcial"), ("Status: blocked", "bloqueada")):
            self.assertEqual(garantias.estado_relatorio(linha + "\nDone"), estado)


class GateDeEntrega(Base):
    def _rodar(self, arquivos, repo_antes=None):
        self.criar_repo()
        if repo_antes:  # arquivos ja versionados na base (origem e espelho)
            ws = self.dir / "src"
            for n, c in repo_antes.items():
                (ws / n).parent.mkdir(parents=True, exist_ok=True)
                (ws / n).write_text(c)
            sh("git", "add", "-A", cwd=ws)
            sh("git", "commit", "-q", "-m", "docs", cwd=ws)
            sh("git", "--git-dir", str(self.cfg.repos_dir / "web-app.git"), "fetch", "-q", str(ws), "+refs/heads/main:refs/heads/main")
        self.codex.respostas = [escreve(REL_OK, arquivos)]
        t = self.quadro.criar("tarefa", "d", "web-app")
        self.svc.ciclo_worker()
        return self.quadro.ver(t["id"]), self.cfg.resultados / str(t["id"])

    def test_notas_em_qualquer_diretorio_saem_do_commit(self):
        v, out = self._rodar({"app2.py": "x=1\n", "docs/NOTAS.md": "n\n", "sub/pasta/handoff.md": "h\n", "CONTEXTO.md": "c\n"})
        self.assertEqual(v["alterados"], ["app2.py"])
        self.assertTrue((out / "excluidos" / "docs__NOTAS.md").exists())
        self.assertTrue((out / "excluidos" / "CONTEXTO.md").exists())
        self.assertIn("docs/NOTAS.md", v["resumo"])
        ws = Path(v["workspace"])
        tracked = sh("git", "ls-files", cwd=ws)
        self.assertNotIn("NOTAS.md", tracked)
        self.assertNotIn("handoff.md", tracked)

    def test_nota_ja_versionada_na_base_nao_e_mexida(self):
        v, out = self._rodar({"docs/NOTAS.md": "novo conteudo\n"}, repo_antes={"docs/NOTAS.md": "velho\n"})
        self.assertEqual(v["alterados"], ["docs/NOTAS.md"])

    def test_remocao_grande_em_documento_marca_revisar_remocoes(self):
        base = "".join(f"linha {i}\n" for i in range(50))
        v, out = self._rodar({"docs/guia.md": "so isto\n"}, repo_antes={"docs/guia.md": base})
        self.assertIn("REVIEW DELETIONS", v["resumo"])
        self.assertIn("REVIEW DELETIONS", v["aviso"])
        self.assertEqual(v["estado"], "em_revisao")  # nao bloqueia
        self.assertIn("Publish (I confirm the deletions)", [r for r, _ in self.botoes_enviados()])
        self.assertIn("REVIEW DELETIONS", (out / "resumo.txt").read_text())

    def test_mais_de_30_linhas_removidas_marca_mesmo_com_percentual_baixo(self):
        base = "".join(f"linha {i}\n" for i in range(200))
        novo = "".join(f"linha {i}\n" for i in range(160, 200)) + "".join(f"linha {i}\n" for i in range(100))
        v, _ = self._rodar({"docs/g.md": novo}, repo_antes={"docs/g.md": base})
        self.assertIn("REVIEW DELETIONS", v["resumo"])

    def test_edicao_pequena_em_documento_nao_marca(self):
        base = "".join(f"linha {i}\n" for i in range(50))
        v, _ = self._rodar({"docs/g.md": base.replace("linha 3\n", "linha tres\n")}, repo_antes={"docs/g.md": base})
        self.assertNotIn("REVIEW DELETIONS", v["resumo"])
        self.assertIn("Publish", [r for r, _ in self.botoes_enviados()])

    def test_remocao_fora_de_markdown_nao_marca(self):
        base = "".join(f"x{i}=1\n" for i in range(50))
        v, _ = self._rodar({"m.py": "y=1\n"}, repo_antes={"m.py": base})
        self.assertNotIn("REVIEW DELETIONS", v["resumo"])

    def test_teste_com_sleep_ou_tempo_anexa_aviso(self):
        v, _ = self._rodar({"tests/test_a.py": "import time\ndef test_x():\n    time.sleep(0.05)\n    t = time.perf_counter()\n",
                            "app.py": "import time\nx = time.time()\n"})
        self.assertIn("sleep/time.time()/perf_counter", v["resumo"])
        self.assertIn("tests/test_a.py", v["resumo"])
        self.assertNotIn("app.py,", v["resumo"].split("sleep/time.time()")[-1])

    def test_sleep_fora_de_teste_nao_avisa(self):
        v, _ = self._rodar({"app.py": "import time\ntime.sleep(1)\n"})
        self.assertNotIn("sleep/time.time()/perf_counter", v["resumo"])


class PromptsECli(Base):
    def test_prompt_chat_sem_negacoes_antigas_e_sem_lista_fixa_de_repos(self):
        with mock.patch.object(prompts, "regras_base", lambda *a: ""):  # as regras (regras/) sao testadas a parte
            p = prompts.prompt_chat("oi", primeira=True, repo="r", branch="agent/x", base="main", agora_iso="x", snapshot="SNAP")
        for velho in ("nada em /etc", "nao leia .env", "/root"):
            self.assertNotIn(velho, p)
        self.assertIn("snapshot", p)
        self.assertTrue(p.startswith("SNAP"))
        for repo in ("web-app", "docs", "site"):
            self.assertNotIn(repo, p)
        self.assertEqual(prompts.prompt_chat("e?", primeira=False, repo="r", branch="b", base="m", agora_iso="x", snapshot="SNAP"), "SNAP\n\ne?")

    def test_repos_da_pergunta_vem_da_configuracao(self):
        p = prompts.prompt_conversa("faz", primeira=True, agora_iso="x", repos=["alfa", "beta"])
        self.assertIn("alfa, beta", p)

    def test_sandbox_efetivo(self):
        self.assertEqual(engines.sandbox_efetivo("workspace-write", True), "danger-full-access")
        self.assertEqual(engines.sandbox_efetivo("read-only", True), "read-only")
        self.assertEqual(engines.sandbox_efetivo("workspace-write"), "workspace-write")  # safe default
        r = engines.CodexRunner(self.cfg.state_dir)
        cmd = r.comando("/w", "workspace-write", "/o")
        self.assertIn("workspace-write", cmd)
        self.assertNotIn("danger-full-access", cmd)
        self.assertIn("sandbox_workspace_write.network_access=false", cmd)

    def test_verificar_regras_le_o_arquivo_e_falha_so_em_inconsistencia(self):
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            raiz = Path(tmp)
            arq = raiz / "BASE_RULES.md"
            self.assertEqual(cli.verificar_regras(arq), 1)  # unreadable
            arq.write_text("# Rules\nReview by: 2026-12-09\n")
            self.assertEqual(cli.verificar_regras(arq, hoje=date(2026, 11, 9)), 0)
            arq.write_text("# Rules\nReview by: 2026-12-09\nSee scripts/missing.py\n")
            self.assertEqual(cli.verificar_regras(arq, hoje=date(2026, 11, 9)), 1)


if __name__ == "__main__":
    unittest.main()
