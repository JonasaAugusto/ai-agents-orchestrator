import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ai_agents_orchestrator import espelho, workspace
from ai_agents_orchestrator.config import Config
from tests.helpers import sh


def repo_com(d: Path, nome: str, arquivo: str) -> Path:
    r = d / nome
    r.mkdir()
    sh("git", "init", "-q", "-b", "main", cwd=r)
    (r / arquivo).write_text("x")
    sh("git", "add", "-A", cwd=r)
    sh("git", "commit", "-q", "-m", "c", cwd=r)
    return r


class EspelhoGithubTest(unittest.TestCase):
    def test_github_vence_copia_local_velha_e_local_so_sem_rede(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            velha = repo_com(d, "velha", "velho.py")      # copia local desatualizada
            gh = repo_com(d, "gh", "filtros.py")            # "GitHub" atual
            spec = {"github": "X/y", "fonte": str(velha), "branch_base": "main"}
            cfg = Config(state_dir=d / "st", root_state=d / "root", repos={"r": spec})
            with mock.patch.object(espelho, "_url_github", lambda s: str(gh)):
                st = espelho.atualizar_repos(cfg)
            self.assertTrue(st["r"]["ok"])
            self.assertEqual(st["r"]["fonte"], "github")
            ws, _b, _base = workspace.preparar_workspace(cfg, "r", 1, "t")
            self.assertTrue((ws / "filtros.py").exists())
            self.assertFalse((ws / "velho.py").exists())
            self.assertEqual(sh("git", "rev-parse", "HEAD", cwd=ws), sh("git", "rev-parse", "main", cwd=gh))
            # sem rede: usa a copia local como fallback, marcando desatualizado, sem mexer na main do GitHub
            with mock.patch.object(espelho, "_url_github", lambda s: str(d / "nao-existe")):
                st = espelho.atualizar_repos(cfg)
            self.assertTrue(st["r"].get("desatualizado"))
            main = sh("git", "--git-dir", str(cfg.repos_dir / "r.git"), "rev-parse", "main").strip()
            self.assertEqual(main, sh("git", "rev-parse", "main", cwd=gh).strip())


if __name__ == "__main__":
    unittest.main()
