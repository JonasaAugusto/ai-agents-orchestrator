import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from ai_agents_orchestrator import historico
from ai_agents_orchestrator.cli import main as cli_main
from tests.helpers import CHAT, TOKEN
from tests import helpers


class Relogio:
    def __init__(self, t=1_800_000_000.0): self.t = t
    def __call__(self): return self.t


class TestHistorico(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.rel = Relogio()
        self.h = historico.Historico(self.d / "logs" / "historico.jsonl", self.rel)

    def test_redator_mascara(self):
        casos = ["usei sk-abcdefghijklmnopqrstuvwx1234", "bot " + TOKEN, "eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0.abcdEFGH12",
                 "-----BEGIN " + "PRIVATE KEY-----\nMIIabc\n-----END " + "PRIVATE KEY-----", "API_KEY=supersecreto123", "SECRET=xyz987"]
        for c in casos:
            self.h.gravar(historico.DONO, c)
        txt = (self.d / "logs" / "historico.jsonl").read_text()
        for tabu in ("sk-abcdef", TOKEN, "eyJhbGci", "MIIabc", "supersecreto123", "xyz987"):
            self.assertNotIn(tabu, txt)

    @unittest.skipIf(os.name == "nt", "POSIX mode 0640; Windows uses inherited user ACLs")
    def test_permissao_nao_world_readable(self):
        self.h.gravar(historico.BOT, "oi", 7)
        modo = stat.S_IMODE(os.stat(self.h.caminho).st_mode)
        self.assertEqual(modo, 0o640)

    def test_retencao_30_dias(self):
        self.h.gravar(historico.DONO, "velha")
        self.rel.t += 31 * 86400
        self.h.gravar(historico.DONO, "nova")
        self.h.podar()
        textos = [r["texto"] for r in historico.ler(self.h.caminho)]
        self.assertEqual(textos, ["nova"])

    def test_ordem_filtros_e_ref(self):
        for i, t in enumerate(["um", "dois ALVO", "tres"]):
            self.rel.t += 60
            self.h.gravar(historico.BOT if i % 2 else historico.DONO, t, 5 if i == 1 else None)
        regs = historico.consultar(self.h.caminho, 2)
        self.assertEqual([r["texto"] for r in regs], ["dois ALVO", "tres"])
        self.assertEqual([r["texto"] for r in historico.consultar(self.h.caminho, busca="alvo")], ["dois ALVO"])
        self.assertIn("#5", historico.formatar(regs[0]))
        self.assertEqual(historico.consultar(self.h.caminho, desde="2999-01-01"), [])

    def test_cli(self):
        self.h.gravar(historico.DONO, "ola mundo")
        self.h.gravar(historico.BOT, "resposta")
        os.environ["AAO_PATHS_STATE_DIR"] = str(self.d)
        import io, contextlib
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                rc = cli_main(["historico", "--ultimas", "5"])
        finally:
            os.environ.pop("AAO_PATHS_STATE_DIR")
        self.assertEqual(rc, 0)
        linhas = buf.getvalue().strip().splitlines()
        self.assertEqual(len(linhas), 2)
        self.assertIn("dono: ola mundo", linhas[0])
        self.assertIn("bot: resposta", linhas[1])


if __name__ == "__main__":
    unittest.main()
