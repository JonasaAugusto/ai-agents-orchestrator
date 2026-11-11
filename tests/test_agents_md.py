"""`<templates_dir>/AGENTS.<repo>.md` is linked into each repo workspace (Codex reads the AGENTS.md of its cwd)."""
from ai_agents_orchestrator import workspace
from tests.helpers import Base, sh


class AgentsMdPorRepo(Base):
    def setUp(self):
        super().setUp()
        self.criar_repo("web-app")
        self.criar_repo("api")
        self.templ = self.dir / "templates"
        self.templ.mkdir()
        (self.templ / "AGENTS.web-app.md").write_text("# rules for the SITE\n")
        (self.templ / "AGENTS.api.md").write_text("# rules for the BACKEND\n")

    def ws(self, repo, tid):
        ws, _, _ = workspace.preparar_workspace(self.cfg, repo, tid, "x")
        return ws

    def test_cada_repo_recebe_o_proprio_template_fora_do_commit(self):
        site, back = self.ws("web-app", 1), self.ws("api", 2)
        self.assertTrue(workspace.instalar_agents_md(self.cfg, site, "web-app", self.templ))
        self.assertTrue(workspace.instalar_agents_md(self.cfg, back, "api", self.templ))
        self.assertEqual((site / "AGENTS.md").read_text(), "# rules for the SITE\n")
        self.assertEqual((back / "AGENTS.md").read_text(), "# rules for the BACKEND\n")
        (site / "app.py").write_text("print('novo')\n")
        info = workspace.finalizar(self.cfg, site, "web-app", 1, "mudanca", "main", "ok")
        self.assertEqual(info["arquivos"], ["app.py"])  # AGENTS.md is not committed
        self.assertEqual(sh("git", "status", "--porcelain", cwd=site), "")

    def test_nao_sobrescreve_agents_md_versionado_pelo_repo(self):
        site = self.ws("web-app", 3)
        (site / "AGENTS.md").write_text("# from the repo\n")
        sh("git", "add", "-f", "AGENTS.md", cwd=site)  # as if the repo versioned it
        self.assertFalse(workspace.instalar_agents_md(self.cfg, site, "web-app", self.templ))
        self.assertEqual((site / "AGENTS.md").read_text(), "# from the repo\n")

    def test_repo_sem_template_nao_ganha_arquivo(self):
        site = self.ws("web-app", 4)
        self.assertFalse(workspace.instalar_agents_md(self.cfg, site, "site", self.templ))
        self.assertFalse(workspace.instalar_agents_md(self.cfg, site, "web-app", self.dir / "nao-existe"))

    def test_preparar_workspace_usa_templates_dir_da_config_e_atualiza_na_retomada(self):
        self.cfg.templates_dir = self.templ
        site = self.ws("web-app", 5)
        self.assertEqual((site / "AGENTS.md").read_text(encoding="utf-8"), "# rules for the SITE\n")
        (site / "AGENTS.md").write_text("stale\n")
        self.ws("web-app", 5)  # workspace already exists: re-applies the current template
        self.assertEqual((site / "AGENTS.md").read_text(encoding="utf-8"), "# rules for the SITE\n")
