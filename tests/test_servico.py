import json
import unittest
from pathlib import Path

from ai_agents_orchestrator import workspace
from ai_agents_orchestrator.db import padroes_colidem
from ai_agents_orchestrator.engines import LimiteInfo, Resultado
from ai_agents_orchestrator.servico import extrair_json
from tests.helpers import Base, sh


def contexto(limite="sim", tarefas=None, reset=1000 + 3600):
    return json.dumps({"limite": limite, "evidencia": "You've hit your session limit · resets 2:40pm", "reset_epoch": reset,
                       "tipo_limite": "five_hour", "briefing_horario": "2026-11-09T12:00:00Z", "divergencias": ["cauda mostra tarefa extra"],
                       "resumo_curto": "Backend v2.11 em andamento.", "contexto_md": "# ctx", "pendentes_md": "- item",
                       "tarefas": tarefas if tarefas is not None else [
                           {"titulo": "Terminar filtro", "descricao": "d", "repo": "web-app", "arquivos": ["app.py"], "dono": "codex", "motivo": "isolada"},
                           {"titulo": "Redesenhar tela", "descricao": "d", "repo": "web-app", "arquivos": [], "dono": "claude", "motivo": "exige skill de design"}]})


def escreve_arquivo(prompt, cwd):
    (Path(cwd) / "novo.py").write_text("print('feito')\n")
    return Resultado(ok=True, texto="Feito: novo.py. Testes: ok. Falta: nada. Para o Claude: nada.", thread_id="T9", segundos=3)


class Fila(Base):
    def test_fila_serial_uma_tarefa_por_vez_e_resultado(self):
        self.criar_repo()
        self.codex.respostas = [escreve_arquivo]
        a = self.quadro.criar("tarefa A", "faz A", "web-app")
        b = self.quadro.criar("tarefa B", "faz B", "web-app")
        self.assertTrue(self.svc.ciclo_worker())
        self.assertEqual(len(self.codex.chamadas), 1)  # so uma por ciclo
        ta = self.quadro.ver(a["id"])
        self.assertEqual(ta["estado"], "em_revisao")
        self.assertEqual(self.quadro.ver(b["id"])["estado"], "pendente")
        self.assertTrue(ta["branch"].startswith("agent/"))
        self.assertEqual(self.codex.chamadas[0]["sandbox"], "workspace-write")
        out = self.cfg.resultados / str(a["id"])
        self.assertTrue((out / "HANDOFF.md").exists())
        self.assertTrue(list((out / "patches").glob("*.patch")))
        self.assertIn("novo.py", ta["alterados"])
        self.assertIn("Publish", [r for r, _ in self.botoes_enviados()])

    def test_commit_sem_coautor_nem_mencao_a_ia(self):
        self.criar_repo()
        self.codex.respostas = [escreve_arquivo]
        t = self.quadro.criar("Ajusta filtro", "d", "web-app")
        self.svc.ciclo_worker()
        ws = Path(self.quadro.ver(t["id"])["workspace"])
        msg = sh("git", "log", "-1", "--format=%B%an <%ae>", cwd=ws)
        for proibido in ("Co-Authored-By", "Claude", "Codex", "OpenAI", "IA "):
            self.assertNotIn(proibido, msg)

    def test_notas_novas_na_raiz_ficam_fora_do_commit(self):
        self.criar_repo()

        def com_notas(prompt, cwd):
            (Path(cwd) / "novo.py").write_text("x=1\n")
            (Path(cwd) / "CONTEXTO.md").write_text("nota\n")
            (Path(cwd) / "notas.md").write_text("nota\n")
            return Resultado(ok=True, texto="Feito.", thread_id="T1", segundos=1)
        self.codex.respostas = [com_notas]
        t = self.quadro.criar("Com notas", "d", "web-app")
        self.svc.ciclo_worker()
        tv = self.quadro.ver(t["id"])
        ws = Path(tv["workspace"])
        self.assertEqual(tv["alterados"], ["novo.py"])
        arquivos = sh("git", "ls-tree", "-r", "--name-only", "HEAD", cwd=ws).split()
        self.assertNotIn("CONTEXTO.md", arquivos)
        self.assertNotIn("notas.md", arquivos)
        self.assertIn("CONTEXTO.md", tv["resumo"])
        self.assertTrue((self.cfg.resultados / str(t["id"]) / "excluidos" / "CONTEXTO.md").exists())

    def test_nota_ja_versionada_no_repo_e_preservada(self):
        self.criar_repo()
        src = self.dir / "src"
        (src / "CONTEXTO.md").write_text("original\n")
        sh("git", "add", "-A", cwd=src)
        sh("git", "commit", "-q", "-m", "ctx", cwd=src)
        sh("git", "push", "-q", str(self.cfg.repos_dir / "web-app.git"), "main", cwd=src)

        def edita(prompt, cwd):
            (Path(cwd) / "CONTEXTO.md").write_text("original\nmais\n")
            return Resultado(ok=True, texto="Feito.", thread_id="T1", segundos=1)
        self.codex.respostas = [edita]
        t = self.quadro.criar("Edita ctx", "d", "web-app")
        self.svc.ciclo_worker()
        tv = self.quadro.ver(t["id"])
        self.assertEqual(tv["alterados"], ["CONTEXTO.md"])
        self.assertNotIn("out of the commit", tv["resumo"])

    def test_pausado_e_limite_por_hora(self):
        self.criar_repo()
        self.quadro.criar("A", "", "web-app")
        self.estado.set("pausado", True)
        self.assertFalse(self.svc.ciclo_worker())
        self.estado.set("pausado", False)
        self.cfg.tarefas_por_hora = 1
        self.estado.registrar_execucao("tarefa", "codex", 1, True, "x", self.t[0])
        self.assertFalse(self.svc.ciclo_worker())
        self.t[0] += 3700
        self.assertTrue(self.svc.ciclo_worker())

    def test_codex_no_limite_devolve_a_fila_e_avisa_uma_vez(self):
        self.criar_repo()
        t = self.quadro.criar("A", "", "web-app")
        lim = Resultado(ok=False, limite=LimiteInfo("usage limit", reset_epoch=int(self.t[0]) + 7200), erro="usage limit")
        self.codex.respostas = [lim]
        self.svc.ciclo_worker()
        self.assertEqual(self.quadro.ver(t["id"])["estado"], "pendente")
        self.assertTrue(any("plan limit" in x for x in self.tr.textos()))
        n = len(self.codex.chamadas)
        self.assertFalse(self.svc.ciclo_worker())  # bloqueado ate o reset
        self.assertEqual(len(self.codex.chamadas), n)
        self.t[0] += 7300
        self.codex.respostas = [escreve_arquivo]
        self.assertTrue(self.svc.ciclo_worker())

    def test_sem_login_pausa_a_fila(self):
        self.criar_repo()
        self.quadro.criar("A", "", "web-app")
        self.codex.respostas = [Resultado(ok=False, auth_erro=True, erro="Not logged in")]
        self.svc.ciclo_worker()
        self.assertTrue(self.estado.get("pausado"))
        self.assertTrue(any("login" in x for x in self.tr.textos()))

    def test_timeout_e_cancelamento(self):
        self.criar_repo()
        t = self.quadro.criar("A", "", "web-app")
        self.codex.respostas = [Resultado(ok=False, timeout=True, erro="timeout", texto="parcial")]
        self.svc.ciclo_worker()
        self.assertEqual(self.quadro.ver(t["id"])["estado"], "bloqueada")
        t2 = self.quadro.criar("B", "", "web-app")
        self.codex.respostas = [Resultado(ok=False, cancelado=True)]
        self.svc.ciclo_worker()
        self.assertEqual(self.quadro.ver(t2["id"])["estado"], "cancelada")

    def test_desligamento_recoloca_na_fila(self):
        self.criar_repo()
        t = self.quadro.criar("A", "", "web-app")
        self.codex.respostas = [lambda p, c: (setattr(self.svc, "parando", True), Resultado(ok=False, cancelado=True))[1]]
        self.svc.ciclo_worker()
        self.assertEqual(self.quadro.ver(t["id"])["estado"], "pendente")

    def test_repo_fora_da_allowlist_nao_executa(self):
        r = self.svc.delegar("empresa", "x")
        self.assertFalse(r["ok"])
        self.assertEqual(self.quadro.listar(), [])


class LimiteBase(Base):
    def setUp(self):
        super().setUp()
        self.criar_repo()
        self.cfg.armado_flag.touch()
        (self.cfg.state_dir / "para-codex").mkdir()
        (self.cfg.state_dir / "para-codex" / "BRIEFING.md").write_text("# briefing\nem andamento: x\n")

    def hook(self):
        return {"error": "rate_limit", "last_assistant_message": "You've hit your session limit · resets 2:40pm (America/Montevideo)"}


class Limite(LimiteBase):

    def test_auto_ligado_continua_sem_perguntar_e_avisa(self):
        self.codex.respostas = [Resultado(ok=True, texto=contexto(), segundos=5)]
        r = self.svc.claude_limite(self.hook())
        self.assertTrue(r["ok"])
        self.assertEqual(self.codex.chamadas[0]["sandbox"], "read-only")  # fase de contexto e somente leitura
        self.assertTrue(self.codex.chamadas[0]["schema"])
        self.assertEqual(self.root.chamadas[0][0], "espelhar")
        txt = self.tr.textos()[-1]
        self.assertIn("Claude hit its limit; continuing task #", txt)
        self.assertNotIn("Backend", txt)  # sem resumo/contexto
        self.assertIn("Terminar filtro", txt)
        self.assertNotIn("Redesenhar tela", txt)  # tarefa de design vai para o Claude, nao para o Codex
        tarefas = {t["titulo"]: t for t in self.quadro.listar()}
        self.assertEqual((tarefas["Terminar filtro"]["dono"], tarefas["Terminar filtro"]["estado"]), ("codex", "pendente"))
        self.assertEqual((tarefas["Redesenhar tela"]["dono"], tarefas["Redesenhar tela"]["origem"]), ("claude", "codex"))
        pasta = Path(self.estado.get("ultimo_handoff"))
        self.assertIn("cauda mostra tarefa extra", (pasta / "CONTEXTO.md").read_text())
        self.assertTrue((pasta / "PENDENTES.md").exists())
        self.assertTrue(self.estado.get("claude_limite")["ativo"])
        self.assertEqual(self.estado.get("claude_limite")["reset_epoch"], 4600)

    def test_prompt_manda_ler_briefing_primeiro_e_so_conferir(self):
        self.codex.respostas = [Resultado(ok=True, texto=contexto("nao", []), segundos=1)]
        self.svc.claude_limite(self.hook())
        p = self.codex.chamadas[0]["prompt"]
        self.assertLess(p.index("BRIEFING.md"), p.index("transcript-cauda"))
        self.assertIn("CHECK it", p)
        self.assertIn("Never read the whole history", p.replace("nao leia historico inteiro", "Never read the whole history"))

    def test_briefing_velho_pesa_a_cauda(self):
        from datetime import datetime
        from ai_agents_orchestrator.config import fuso; TZ = fuso()
        b = self.cfg.state_dir / "para-codex" / "BRIEFING.md"  # a idade vem do horario DECLARADO, nao do mtime
        declarado = datetime.fromtimestamp(self.t[0] - 8 * 3600, TZ)
        b.write_text(f"# briefing\nAtualizado em: {declarado:%Y-%m-%d %H:%M}\n")
        self.codex.respostas = [Resultado(ok=True, texto=contexto("nao", []), segundos=1)]
        self.svc.claude_limite(self.hook())
        self.assertIn("stale", self.codex.chamadas[0]["prompt"])

    def test_auto_desligado_pede_botao(self):
        self.estado.set("auto", False)
        self.codex.respostas = [Resultado(ok=True, texto=contexto(), segundos=5)]
        self.svc.claude_limite(self.hook())
        self.assertEqual([r for r, _ in self.botoes_enviados()], ["Continue", "Adjust", "Wait"])
        t = [x for x in self.quadro.listar() if x["origem"] == "handoff"][0]
        self.assertEqual(t["estado"], "bloqueada")
        self.assertFalse(self.svc.ciclo_worker())  # nada roda antes do botao
        self.apertar("Continue")
        self.assertEqual(self.quadro.ver(t["id"])["estado"], "pendente")

    def test_veredito_nao_nao_dispara_nada(self):
        self.codex.respostas = [Resultado(ok=True, texto=contexto("nao", []), segundos=1)]
        r = self.svc.claude_limite(self.hook())
        self.assertEqual(r["limite"], "nao")
        self.assertEqual(self.quadro.listar(), [])
        self.assertFalse(self.estado.get("claude_limite", {}).get("ativo"))

    def test_json_invalido_nao_executa_nada(self):
        self.codex.respostas = [Resultado(ok=True, texto="limite: sim!", segundos=1)]
        r = self.svc.claude_limite(self.hook())
        self.assertFalse(r["ok"])
        self.assertEqual(self.quadro.listar(), [])

    def test_nao_armado_ignora_o_hook(self):
        self.cfg.armado_flag.unlink()
        self.assertFalse(self.svc.claude_limite(self.hook())["ok"])
        self.assertEqual(self.codex.chamadas, [])

    def test_mesmo_episodio_nao_acorda_o_codex_de_novo_e_teto_diario(self):
        self.codex.respostas = [Resultado(ok=True, texto=contexto(), segundos=1)]
        self.svc.claude_limite(self.hook())
        self.svc.claude_limite(self.hook())
        self.assertEqual(len(self.codex.chamadas), 1)
        self.t[0] += 1200
        # teto por dia: outro episodio so ate o limite
        self.estado.set("claude_limite", {})
        self.cfg.acordadas_por_dia = 1
        r = self.svc.claude_limite(self.hook())
        self.assertEqual(r["motivo"], "daily cap")

    def test_codex_sem_login(self):
        self.codex._logado = False
        self.assertFalse(self.svc.claude_limite(self.hook())["ok"])
        self.assertIn("not logged in", self.tr.textos()[-1])

    def test_codex_tambem_no_limite(self):
        self.codex.respostas = [Resultado(ok=False, limite=LimiteInfo("usage limit", int(self.t[0]) + 5000), erro="x")]
        r = self.svc.claude_limite(self.hook())
        self.assertFalse(r["ok"])
        self.assertIn("at its limit too", self.tr.textos()[-1])

    def test_sem_tarefas_avisa_e_aguarda(self):
        self.codex.respostas = [Resultado(ok=True, texto=contexto("sim", []), segundos=1)]
        self.svc.claude_limite(self.hook())
        self.assertIn("No development tasks", self.tr.textos()[-1])

    def test_extrair_json(self):
        self.assertEqual(extrair_json('texto {"a": 1} fim'), {"a": 1})
        self.assertIsNone(extrair_json("sem json"))


class Volta(Base):
    def setUp(self):
        super().setUp()
        self.cfg.armado_flag.touch()
        self.estado.set("claude_limite", {"ativo": True, "reset_epoch": 2000, "acordadas": 0, "ultima_acordada": 0, "evidencia": "x"})
        self.estado.set("fallback_ativo", True)

    def test_so_depois_do_horario_acorda_o_claude_e_avisa_com_botoes(self):
        self.t[0] = 2030
        self.assertFalse(self.svc.tick_volta())  # ainda dentro da margem de 60 s
        self.t[0] = 2100
        self.assertTrue(self.svc.tick_volta())
        self.assertEqual(self.root.chamadas[-1][0], "acordar_claude")
        self.assertIn("claude-resumed", self.root.chamadas[-1][1])
        self.assertIn("Claude is available again", self.tr.textos()[-1])
        self.assertEqual([r for r, _ in self.botoes_enviados()], ["Pause Codex", "Keep running"])
        self.assertTrue((self.cfg.para_claude / "HANDOFF-PARA-CLAUDE.md").exists())
        self.assertFalse(self.svc.tick_volta())  # nao insiste antes de 30 min
        self.t[0] += 1900
        self.assertTrue(self.svc.tick_volta())
        self.assertEqual(len(self.tr.textos()), 1)  # o aviso ao dono sai uma vez so

    def test_tentativas_limitadas(self):
        for i in range(6):
            self.t[0] = 2100 + i * 2000
            self.svc.tick_volta()
        self.assertEqual(sum(1 for c in self.root.chamadas if c[0] == "acordar_claude"), 3)

    def test_falha_ao_acordar_ainda_avisa_o_dono(self):
        self.root.acordar_ok = False
        self.t[0] = 2100
        self.svc.tick_volta()
        self.assertIn("could not wake", self.tr.textos()[-1])

    def test_claude_assumiu_nao_pausa_mas_devolve_o_que_nao_comecou(self):
        t = self.quadro.criar("fallback pendente", "", "", "codex", "handoff")
        self.svc.tarefa_atual = 77
        r = self.svc.claude_assumiu()
        self.assertFalse(self.estado.get("pausado", False))  # NAO pausa
        self.assertEqual(r["codex_trabalhando"], 77)
        self.assertEqual(self.quadro.ver(t["id"])["dono"], "claude")
        self.assertFalse(self.estado.get("claude_limite")["ativo"])
        self.assertIn("keeps working on task #77", self.tr.textos()[-1])
        self.assertTrue(json.loads((self.cfg.para_claude / "estado.json").read_text())["lido"])

    def test_bastao_so_encerra_quando_a_tarefa_termina(self):
        self.svc.claude_assumiu()
        self.estado.set("fallback_ativo", True)
        self.svc.tarefa_atual = 5  # ainda trabalhando: nao encerra
        self.svc.quadro.criar("em curso", dono="codex", origem="handoff")
        self.svc._fim_de_bastao()
        self.assertTrue(self.estado.get("fallback_ativo"))
        for t in self.quadro.listar(("pendente",)):
            self.quadro.atualizar(t["id"], estado="em_revisao")
        self.svc._fim_de_bastao()
        self.assertFalse(self.estado.get("fallback_ativo"))
        self.assertFalse(any("baton handed back" in t for t in self.tr.textos()))  # nao vai ao Telegram
        self.assertTrue(any("baton handed back" in l for l in self.svc.ring))  # fica so no log

    def test_botao_pausar_o_codex(self):
        self.t[0] = 2100
        self.svc.tick_volta()
        self.apertar("Pause Codex")
        self.assertTrue(self.estado.get("pausado"))


class Quadro_(Base):
    def test_travas_de_arquivos(self):
        self.assertTrue(padroes_colidem(["app/*.py"], ["app/main.py"]))
        self.assertTrue(padroes_colidem(["web"], ["web/index.html"]))
        self.assertFalse(padroes_colidem(["docs/a.md"], ["web/index.html"]))
        self.assertTrue(padroes_colidem([], ["x"]))  # sem arquivos declarados = repo inteiro
        a = self.quadro.criar("A", repo="r", arquivos=["app/main.py"])
        self.quadro.atualizar(a["id"], estado="em_andamento")
        self.assertEqual(self.quadro.conflitos(None, "r", ["app/main.py"])[0]["id"], a["id"])
        self.assertEqual(self.quadro.conflitos(None, "r", ["docs/x.md"]), [])
        self.assertEqual(self.quadro.conflitos(None, "outro", ["app/main.py"]), [])

    def test_tarefa_em_conflito_espera_a_trava(self):
        self.cfg.repos = {"r": {}}
        ativo = self.quadro.criar("claude mexendo", repo="r", dono="claude", arquivos=["app/*.py"])
        self.quadro.atualizar(ativo["id"], estado="em_andamento")
        r = self.svc.delegar("r", "do codex", arquivos=["app/x.py"])
        self.assertIn("File overlap", r["aviso"])
        self.assertIsNone(self.quadro.proxima_para_codex({"r"}))
        self.quadro.atualizar(ativo["id"], estado="em_revisao")
        self.assertEqual(self.quadro.proxima_para_codex({"r"})["id"], r["id"])

    def test_dependencias(self):
        self.cfg.repos = {"r": {}}
        a = self.quadro.criar("A", repo="r")
        b = self.quadro.criar("B", repo="r", depende=[a["id"]])
        self.assertEqual(self.quadro.proxima_para_codex({"r"})["id"], a["id"])
        self.quadro.atualizar(a["id"], estado="em_revisao")
        self.assertEqual(self.quadro.proxima_para_codex({"r"})["id"], b["id"])

    def test_cli_delegar_listar_ver_reivindicar_cancelar(self):
        self.cfg.repos = {"r": {}}
        r = self.svc.comando({"cmd": "delegar", "repo": "r", "titulo": "T", "descricao": "D", "arquivos": ["a.py"],
                              "briefing": "# briefing da tarefa"})
        tid = r["id"]
        self.assertTrue((self.cfg.state_dir / "para-codex" / "tarefas" / f"{tid}.md").exists())
        self.assertEqual(self.svc.comando({"cmd": "listar"})["tarefas"][0]["id"], tid)
        self.assertEqual(self.svc.comando({"cmd": "ver", "id": tid})["tarefa"]["titulo"], "T")
        rv = self.svc.comando({"cmd": "reivindicar", "id": tid, "dono": "claude"})
        self.assertEqual((rv["tarefa"]["dono"], rv["tarefa"]["estado"]), ("claude", "em_andamento"))
        self.assertTrue(self.svc.comando({"cmd": "cancelar", "id": tid})["ok"])
        self.assertEqual(self.quadro.ver(tid)["estado"], "cancelada")
        self.assertFalse(self.svc.comando({"cmd": "xyz"})["ok"])

    def test_revisao_cruzada_le_o_branch_sem_escrever(self):
        self.criar_repo()
        r = self.svc.comando({"cmd": "revisar", "repo": "web-app", "branch": "feature/x"})
        self.assertEqual(self.root.chamadas[-1][0], "espelhar")  # traz os branches locais do Claude
        self.codex.respostas = [Resultado(ok=True, texto="Veredito: ajustar", segundos=2)]
        self.svc.ciclo_worker()
        self.assertEqual(self.codex.chamadas[-1]["sandbox"], "read-only")
        t = self.quadro.ver(r["id"])
        self.assertEqual((t["estado"], t["kind"]), ("concluida", "revisao"))
        self.assertIn("ajustar", t["resumo"])
        self.assertEqual(t["visto_claude"], 0)  # aparece no recado do Claude

    def test_codex_propoe_tarefa_ao_claude_e_aparece_no_recado_e_no_hook(self):
        t = self.quadro.criar("Redesenhar tela", "exige skill", "", "claude", "codex")
        self.assertEqual(self.quadro.recado_pendente()[0]["id"], t["id"])
        txt = self.svc.recado_para_hook()
        self.assertIn("Redesenhar tela", txt)
        self.assertIn("HANDOFF-PARA-CLAUDE.md", txt)
        self.svc.claude_assumiu()
        self.assertEqual(self.svc.recado_para_hook(), "")  # lido: nada a injetar

    def test_recado_vazio_sem_pendencias(self):
        self.assertEqual(self.svc.recado_para_hook(), "")

    def test_esperar_termina_quando_a_tarefa_conclui(self):
        from ai_agents_orchestrator import cli
        self.cfg.repos = {"r": {}}
        tid = self.svc.delegar("r", "T")["id"]
        self.assertNotIn(self.quadro.ver(tid)["estado"], cli.FINAIS)
        self.quadro.atualizar(tid, estado="em_revisao")
        self.assertIn(self.quadro.ver(tid)["estado"], cli.FINAIS)

    def test_briefing_status(self):
        self.assertFalse(self.svc.comando({"cmd": "briefing_status"})["existe"])
        d = self.cfg.state_dir / "para-codex"
        d.mkdir()
        (d / "BRIEFING.md").write_text("a\nb\n")
        r = self.svc.comando({"cmd": "briefing_status"})
        self.assertTrue(r["existe"])
        self.assertEqual(r["linhas"], 2)


class Publicar(Base):
    def setUp(self):
        super().setUp()
        self.gh = self.criar_repo()
        self.codex.respostas = [escreve_arquivo]
        self.t_ = self.quadro.criar("Muda coisa", "", "web-app")
        self.svc.ciclo_worker()

    def test_publicar_exige_botao_merge_push_e_producao_so_depois(self):
        self.assertEqual(sh("git", "--git-dir", str(self.gh), "log", "--oneline", "main").count("\n"), 1)  # nada publicado
        self.apertar("Publish")
        import time
        for _ in range(50):
            if self.quadro.ver(self.t_["id"])["estado"] == "concluida":
                break
            time.sleep(0.1)
        self.assertEqual(self.quadro.ver(self.t_["id"])["estado"], "concluida")
        log = sh("git", "--git-dir", str(self.gh), "log", "--oneline", "main")
        self.assertIn("Merge agent/", log)
        self.assertTrue((self.cfg.logs / "auditoria.jsonl").read_text().count("merge+push") == 1)
        self.assertIn("Deploy to production", [r for r, _ in self.botoes_enviados()])
        self.assertFalse(any(c[0] == "publicar" for c in self.root.chamadas))  # nada em producao ainda
        self.apertar("Deploy to production")
        for _ in range(50):
            if any(c[0] == "publicar" for c in self.root.chamadas):
                break
            time.sleep(0.1)
        pub = [c for c in self.root.chamadas if c[0] == "publicar"][0]
        self.assertEqual((pub[1], pub[3]), ("web-app", "deploy"))

    def test_revisar_antes_cria_pedido_ao_claude_e_nao_publica(self):
        self.apertar("Review first")
        propostas = [t for t in self.quadro.listar() if t["dono"] == "claude"]
        self.assertEqual(len(propostas), 1)
        self.assertEqual(sh("git", "--git-dir", str(self.gh), "log", "--oneline", "main").count("\n"), 1)

    def test_nao_publicar(self):
        self.apertar("Don't publish")
        self.assertEqual(self.quadro.ver(self.t_["id"])["estado"], "em_revisao")

    def test_push_de_branch_vai_pelo_daemon_root_e_modo_deploy_key_fica_desativado(self):
        ws = Path(self.quadro.ver(self.t_["id"])["workspace"])
        br = self.quadro.ver(self.t_["id"])["branch"]
        h = workspace.push_branch(self.cfg, "web-app", ws, br, root=self.root)
        self.assertEqual(sh("git", "--git-dir", str(self.gh), "rev-parse", f"refs/heads/{br}").strip(), h)
        self.assertEqual(self.root.chamadas[-1][0], "push_branch")
        with self.assertRaises(workspace.GitErro):
            workspace.push_branch(self.cfg, "web-app", ws, br)  # sem daemon nao ha push (nada de chave de deploy por padrao)
        self.assertEqual(self.cfg.push_modo, "root")

    def test_guardas_de_git(self):
        ws = Path(self.quadro.ver(self.t_["id"])["workspace"])
        with self.assertRaises(workspace.GitErro):
            workspace.git(self.cfg, ["push", "--force", "github", "main"], ws)
        with self.assertRaises(workspace.GitErro):
            workspace.git(self.cfg, ["push", "github", "+main:main"], ws)
        with self.assertRaises(workspace.GitErro):
            workspace.git(self.cfg, ["push", "github", ":main"], ws)
        with self.assertRaises(workspace.GitErro):
            workspace.merge_e_push(self.cfg, "web-app", ws, "main")  # so branches agent/*
        with self.assertRaises(workspace.GitErro):
            workspace.merge_e_push(self.cfg, "empresa", ws, "agent/x")  # repo fora da allowlist
        with self.assertRaises(workspace.GitErro):
            workspace.preparar_workspace(self.cfg, "other-repo", 99, "x")

    def test_push_nao_fast_forward_e_recusado_e_auditado(self):
        # alguem avanca a main remota: o merge refaz a base a partir do remoto, entao integra; o conflito vira erro
        ws = Path(self.quadro.ver(self.t_["id"])["workspace"])
        outro = self.dir / "outro"
        sh("git", "clone", "-q", str(self.gh), str(outro))
        (outro / "app.py").write_text("print('conflict')\n")
        sh("git", "commit", "-qam", "remoto mudou", cwd=outro)
        sh("git", "push", "-q", "origin", "main", cwd=outro)
        # o branch do agente mexe no mesmo arquivo -> conflito, nunca force
        (ws / "app.py").write_text("print('agente')\n")
        sh("git", "commit", "-qam", "agente", cwd=ws)
        with self.assertRaises(workspace.GitErro):
            workspace.merge_e_push(self.cfg, "web-app", ws, self.quadro.ver(self.t_["id"])["branch"], root=self.root)
        self.assertIn("conflict", (self.cfg.logs / "auditoria.jsonl").read_text())


if __name__ == "__main__":
    unittest.main()


class TetoSemSpam(LimiteBase):
    def test_aviso_de_teto_so_uma_vez_por_dia_e_sobrevive_a_restart(self):
        self.cfg.acordadas_por_dia = 1
        self.estado.incr_dia("acordadas", self.svc.hoje())
        for _ in range(5):
            self.t[0] += 1200
            self.estado.set("claude_limite", {})
            self.svc.claude_limite(self.hook())
        self.assertEqual(sum("Daily cap" in x for x in self.tr.textos()), 1)
        self.assertEqual(self.codex.chamadas, [])
        # restart: novo Servico sobre o mesmo banco
        from ai_agents_orchestrator.servico import Servico
        svc2 = Servico(self.cfg, self.estado, self.quadro, self.tg, self.codex, None, self.root, relogio=lambda: self.t[0])
        svc2.claude_limite(self.hook())
        self.assertEqual(sum("Daily cap" in x for x in self.tr.textos()), 1)
        self.t[0] += 86400  # outro dia, o aviso volta
        self.estado.set("ultima_conferencia_hook", 0)
        self.estado.incr_dia("acordadas", svc2.hoje())
        svc2.claude_limite(self.hook())
        self.assertEqual(sum("Daily cap" in x for x in self.tr.textos()), 2)

    def test_hook_repetido_em_janela_curta_nao_conta_nem_chama_codex(self):
        self.codex.respostas = [Resultado(ok=True, texto=contexto("nao", []), segundos=1)]
        self.svc.claude_limite(self.hook())
        for _ in range(4):
            self.svc.claude_limite(self.hook())
        self.assertEqual(len(self.codex.chamadas), 1)
        self.assertEqual(self.estado.contagem_dia("acordadas", self.svc.hoje()), 1)

    def test_teto_ajustado_por_estado(self):
        self.estado.set("teto_acordadas", 3)
        self.assertEqual(self.svc.teto_acordadas(), 3)

    def test_briefing_sem_permissao_de_leitura_nao_derruba(self):
        from unittest import mock
        with mock.patch("pathlib.Path.read_text", side_effect=PermissionError("denied")):
            bi = self.svc.briefing_info()
        self.assertTrue(bi["existe"])
        self.codex.respostas = [Resultado(ok=True, texto=contexto("nao", []), segundos=1)]
        with mock.patch("pathlib.Path.read_text", side_effect=PermissionError("denied")):
            self.assertTrue(self.svc.claude_limite(self.hook())["ok"])

    def test_aviso_sem_login_uma_vez_por_dia(self):
        self.codex._logado = False
        for _ in range(3):
            self.t[0] += 1200
            self.svc.claude_limite(self.hook())
        self.assertEqual(sum("not logged in" in x for x in self.tr.textos()), 1)


class HandoffCurto(LimiteBase):
    def test_handoff_envia_uma_unica_mensagem_curta_no_inicio_e_conclusao_com_publicar(self):
        tarefas = [{"titulo": "Backend v2", "descricao": "d", "repo": "web-app", "arquivos": ["app.py"],
                    "dono": "codex", "motivo": "m"}]
        self.codex.respostas = [Resultado(ok=True, texto=contexto("sim", tarefas), segundos=1), escreve_arquivo]
        self.svc.claude_limite(self.hook())
        inicio = self.tr.textos()
        self.assertEqual(len(inicio), 1)
        self.assertTrue(inicio[0].startswith("Claude hit its limit; continuing task #"))
        self.assertIn("Backend v2", inicio[0])
        self.assertNotIn("Backend v2 em andamento", inicio[0])  # sem resumo
        self.svc.ciclo_worker()
        novos = self.tr.textos()[1:]
        self.assertFalse(any("Starting" in x for x in novos))
        self.assertTrue(any("completed" in x for x in novos))
        self.assertIn("Publish", [r for r, _ in self.botoes_enviados()])
