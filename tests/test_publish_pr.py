"""`aao publish --pr`: push the approved agent/* branch and open a pull request with gh; never merge."""
import io
import json
import subprocess
from contextlib import redirect_stdout
from unittest import mock
from unittest.mock import patch

from ai_agents_orchestrator import cli, compat
from tests.helpers import Base, sh, needs_root
from tests import test_lite
from tests.test_servico import escreve_arquivo


class GhFalso:
    """Stands in for the GitHub CLI: records every call; `instalado`/`logado` control the preflight."""

    def __init__(self, logado=True, falha_pr=False):
        self.logado, self.falha_pr, self.chamadas = logado, falha_pr, []

    def __call__(self, args, cwd=None):
        self.chamadas.append(list(args))
        if args[:2] == ["auth", "status"]:
            return subprocess.CompletedProcess(args, 0 if self.logado else 1, "", "not logged in")
        if args[:2] == ["pr", "create"]:
            if self.falha_pr:
                return subprocess.CompletedProcess(args, 1, "", "boom ghp_" + "a" * 36)
            return subprocess.CompletedProcess(args, 0, "https://github.com/o/r/pull/7\n", "")
        raise AssertionError(args)

    def criados(self):
        return [c for c in self.chamadas if c[:2] == ["pr", "create"]]


def valor(args, flag):
    return args[args.index(flag) + 1]


class PrLite(test_lite.LiteWorkflow):
    def setUp(self):
        super().setUp()
        self.gh = GhFalso()

    def ready(self):
        r = super().ready()
        self.cfg.repos["web-app"]["github"] = "o/r"
        return r

    def remoto(self):
        return self.cfg.repos["web-app"]["github_url"]

    def pr(self, tid, sha, gh="default", **kw):
        out = io.StringIO()
        with patch.object(compat, "euid", return_value=1000), redirect_stdout(out):
            rc = cli._publicar(self.cfg, tid, root=self.backend, interativo=True, pr=True, gh=self.gh if gh == "default" else gh,
                               entrada=kw.pop("entrada", lambda _: f"{tid} {sha}"))
        return rc, out.getvalue()

    def test_pushes_branch_opens_pr_and_never_merges(self):
        tid, ws, branch, sha = self.ready()
        before = sh("git", "--git-dir", self.remoto(), "rev-parse", "main")
        rc, out = self.pr(tid, sha)
        self.assertEqual(rc, 0, out)
        self.assertEqual(sh("git", "--git-dir", self.remoto(), "rev-parse", "main"), before)
        self.assertEqual(sh("git", "--git-dir", self.remoto(), "rev-parse", branch).strip(), sha)
        (args,) = self.gh.criados()
        self.assertEqual((valor(args, "--base"), valor(args, "--head"), valor(args, "--repo")), ("main", branch, "o/r"))
        self.assertIn("pull/7", out)
        t = self.quadro.ver(tid)
        self.assertEqual(t["estado"], "em_revisao")
        self.assertEqual(json.loads(t["publicado"])["commit"], sha)

    def test_pr_body_has_no_secret_or_conversation(self):
        tid, ws, branch, sha = self.ready()
        self.quadro.atualizar(tid, descricao="user said: my password is hunter2", titulo="fix ghp_" + "b" * 36,
                              resumo="narrative from codex")
        self.pr(tid, sha)
        (args,) = self.gh.criados()
        texto = valor(args, "--title") + valor(args, "--body")
        for banned in ("hunter2", "ghp_", "narrative", "password"):
            self.assertNotIn(banned, texto)
        self.assertIn("app.py", texto)
        self.assertIn(sha, texto)

    def test_commit_with_phone_like_digits_is_kept_verbatim(self):
        from ai_agents_orchestrator import config, servico
        sha = "67943642484eb144bfb6968c8f0a2b4d159c711c"  # digit run that the phone masks would eat
        _titulo, corpo = servico.texto_pr(config.Config(), {"id": 1, "origem": "codex"},
                                          {"commit": sha, "destino": "main", "branch": "agent/1-x"}, ["app.py"])
        self.assertIn(sha, corpo)

    def test_chat_task_title_is_generic(self):
        tid, ws, branch, sha = self.ready()
        self.quadro.atualizar(tid, origem="chat", titulo="what the owner typed")
        self.pr(tid, sha)
        (args,) = self.gh.criados()
        self.assertEqual(valor(args, "--title"), f"Task #{tid}")

    def test_missing_gh_fails_without_side_effects(self):
        tid, ws, branch, sha = self.ready()
        with patch("shutil.which", return_value=None):
            rc, out = self.pr(tid, sha, gh=None, entrada=lambda _: self.fail("must not ask"))
        self.assertEqual(rc, 1)
        self.assertIn("gh", out)
        self.assertIn("not found", out)
        self.assertEqual(subprocess.run(["git", "--git-dir", self.remoto(), "rev-parse", "--verify", "-q", branch],
                                        capture_output=True).returncode, 1)
        self.assertFalse(self.quadro.ver(tid)["publicado"])

    def test_logged_out_gh_fails_without_side_effects(self):
        tid, ws, branch, sha = self.ready()
        rc, out = self.pr(tid, sha, gh=GhFalso(logado=False), entrada=lambda _: self.fail("must not ask"))
        self.assertEqual(rc, 1)
        self.assertIn("gh auth login", out)
        self.assertEqual(subprocess.run(["git", "--git-dir", self.remoto(), "rev-parse", "--verify", "-q", branch],
                                        capture_output=True).returncode, 1)

    def test_needs_sha_confirmation_and_interactive_terminal(self):
        tid, ws, branch, sha = self.ready()
        rc, _ = self.pr(tid, sha, entrada=lambda _: str(tid))
        self.assertEqual(rc, 2)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(cli._publicar(self.cfg, tid, root=self.backend, interativo=False, pr=True, gh=self.gh), 2)
        self.assertEqual(self.gh.criados(), [])

    def test_commit_changed_after_approval_is_refused(self):
        tid, ws, branch, sha = self.ready()
        def confirm(_):
            (ws / "app.py").write_text("print('changed')\n")
            sh("git", "add", "app.py", cwd=ws)
            sh("git", "commit", "-qm", "changed", cwd=ws)
            return f"{tid} {sha}"
        rc, _ = self.pr(tid, sha, entrada=confirm)
        self.assertEqual(rc, 1)
        self.assertEqual(self.gh.criados(), [])
        self.assertEqual(subprocess.run(["git", "--git-dir", self.remoto(), "rev-parse", "--verify", "-q", branch],
                                        capture_output=True).returncode, 1)

    def test_never_force_pushes_a_diverged_remote_branch(self):
        tid, ws, branch, sha = self.ready()
        other = self.dir / "other"
        sh("git", "clone", "-q", self.remoto(), str(other))
        sh("git", "checkout", "-q", "-b", branch, cwd=other)
        (other / "x.txt").write_text("remote\n")
        sh("git", "add", "x.txt", cwd=other)
        sh("git", "commit", "-qm", "remote", cwd=other)
        sh("git", "push", "-q", "origin", branch, cwd=other)
        remote_sha = sh("git", "--git-dir", self.remoto(), "rev-parse", branch).strip()
        rc, _ = self.pr(tid, sha)
        self.assertEqual(rc, 1)
        self.assertEqual(sh("git", "--git-dir", self.remoto(), "rev-parse", branch).strip(), remote_sha)
        self.assertEqual(self.gh.criados(), [])

    def test_gh_failure_is_masked_and_retryable(self):
        tid, ws, branch, sha = self.ready()
        rc, out = self.pr(tid, sha, gh=GhFalso(falha_pr=True))
        self.assertEqual(rc, 1)
        self.assertNotIn("ghp_" + "a" * 36, out)
        self.assertFalse(self.quadro.ver(tid)["publicado"])
        rc, _ = self.pr(tid, sha)
        self.assertEqual(rc, 0)

    def test_repo_without_slug_is_refused_before_pushing(self):
        tid, ws, branch, sha = self.ready()
        del self.cfg.repos["web-app"]["github"]
        rc, out = self.pr(tid, sha)
        self.assertEqual(rc, 1)
        self.assertIn("slug", out)


class PrCompleto(Base):
    def setUp(self):
        super().setUp()
        self.remoto = self.criar_repo()
        self.cfg.repos["web-app"]["github"] = "o/r"
        self.codex.respostas = [escreve_arquivo]
        self.tid = self.quadro.criar("Muda coisa", "secret talk", "web-app", "codex", "claude")["id"]
        self.svc.ciclo_worker()
        self.gh = GhFalso()

    def pr(self, gh=None):
        out = io.StringIO()
        with mock.patch("os.geteuid", return_value=0), redirect_stdout(out):
            rc = cli._publicar(self.cfg, self.tid, root=self.root, interativo=True, pr=True, gh=gh or self.gh,
                               entrada=lambda _p: str(self.tid))
        return rc, out.getvalue()

    @needs_root
    def test_full_mode_pushes_branch_only(self):
        branch = self.quadro.ver(self.tid)["branch"]
        before = sh("git", "--git-dir", str(self.remoto), "rev-parse", "main")
        rc, out = self.pr()
        self.assertEqual(rc, 0, out)
        self.assertEqual(sh("git", "--git-dir", str(self.remoto), "rev-parse", "main"), before)
        self.assertTrue(sh("git", "--git-dir", str(self.remoto), "rev-parse", branch).strip())
        self.assertEqual([c[0] for c in self.root.chamadas if c[0].startswith("push")], ["push_branch"])
        (args,) = self.gh.criados()
        self.assertNotIn("secret talk", valor(args, "--body"))
        self.assertEqual(self.quadro.ver(self.tid)["estado"], "em_revisao")

    def test_full_mode_logged_out_gh_has_no_side_effects(self):
        rc, out = self.pr(GhFalso(logado=False))
        self.assertEqual(rc, 1)
        self.assertIn("gh auth login", out)
        self.assertEqual(self.root.chamadas and [c for c in self.root.chamadas if c[0].startswith("push")], [])
        self.assertFalse(self.quadro.ver(self.tid)["publicado"])
