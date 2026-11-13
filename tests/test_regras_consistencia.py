"""Rules linter: limits, review date, contradictions and cited paths; negative fixtures never touch the shipped files."""
import tempfile
import unicodedata
import sys
import unittest
import warnings
from datetime import date
from pathlib import Path

from ai_agents_orchestrator import garantias, prompts
from ai_agents_orchestrator.regras import aviso_regras, problemas_regras as _problemas, problemas_template

RAIZ = Path(__file__).resolve().parents[1]
REGRAS = RAIZ / "rules" / "BASE_RULES.example.md"
TEMPLATES = ("AGENTS.example.md",)


def problemas_regras(texto, raiz, hoje):
    """Same as the linter, but an expired review date is surfaced as a warning (like `aao check-rules`)."""
    aviso = aviso_regras(texto, hoje)
    if aviso:
        warnings.warn(aviso, UserWarning, stacklevel=2)
    return _problemas(texto, raiz, hoje)


class RegrasPublicadas(unittest.TestCase):
    def test_regras_base_consistentes(self):
        texto = REGRAS.read_text(encoding="utf-8")
        self.assertEqual(problemas_regras(texto, REGRAS.parent, date(2026, 11, 9)), [])

    def test_regras_concordam_com_o_servico_no_estado_do_relatorio(self):
        texto = REGRAS.read_text(encoding="utf-8")
        for linha, estado in (("Status: completed", "concluida"), ("Status: partial", "parcial"), ("Status: blocked", "bloqueada")):
            self.assertIn(f"`{linha}`", texto)
            self.assertEqual(garantias.estado_relatorio(f"{linha}\n\nDone"), estado)
        self.assertIn("FIRST line", texto)
        prompt = prompts.prompt_tarefa({"id": 1, "titulo": "t", "repo": "r"}, branch="b", base="main", briefing_tarefa=False)
        self.assertIn("FIRST line is `Status: completed`", prompt)

    def test_proibicoes_essenciais_continuam_nas_regras(self):
        texto = unicodedata.normalize("NFKD", REGRAS.read_text(encoding="utf-8").casefold())
        for trecho in ("[publish]", "force-push", "no co-author", "mention of ai", "secrets", "personal data",
                       "production", "ssh", "dono: \"claude\"", "never create", "only on the branch", "review by"):
            with self.subTest(trecho=trecho):
                self.assertIn(trecho, texto)

    def test_templates_presentes_e_curtos(self):
        nomes = set(TEMPLATES) | {p.name for p in (RAIZ / "templates").glob("AGENTS.*.md")}
        for nome in sorted(nomes):
            with self.subTest(template=nome):
                self.assertEqual(problemas_template(RAIZ / "templates" / nome), [])

    def test_exemplo_de_divisao_existe(self):
        self.assertIn("serialize", (RAIZ / "rules" / "DIVISION.example.md").read_text(encoding="utf-8"))


class CenariosNegativos(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.raiz = Path(self.tmp.name)
        self.hoje = date(2026, 11, 9)
        self.base = "# Rules (valid from 2026-11-09; review by 2026-12-09)\n"

    def verificar(self, extra="", hoje=None):
        return problemas_regras(self.base + extra, self.raiz, hoje or self.hoje)

    def test_limite_de_linhas_inclusivo(self):
        self.assertEqual(self.verificar("rule\n" * 59), [])
        self.assertIn("more than 60 lines", self.verificar("rule\n" * 60))

    def test_limite_em_bytes_utf8_inclusivo(self):
        resto = 32 * 1024 - len(self.base.encode("utf-8"))
        texto = self.base + "a" * resto
        self.assertEqual(problemas_regras(texto, self.raiz, self.hoje), [])
        self.assertIn("more than 32 KiB", problemas_regras(texto + "á", self.raiz, self.hoje))

    def test_validade_inclui_o_dia_da_revisao(self):
        self.assertEqual(self.verificar(hoje=date(2026, 12, 9)), [])
        with self.assertWarnsRegex(UserWarning, "rules expired, please review"):
            self.assertEqual(self.verificar(hoje=date(2026, 12, 10)), [])

    def test_validade_em_portugues_tambem_vale(self):
        self.assertEqual(problemas_regras("# Regras\nRevisar até: 2026-12-09\n", self.raiz, self.hoje), [])

    def test_validade_ausente_ou_invalida(self):
        for texto in ("# No date", "Review by 2026-02-30"):
            with self.subTest(texto=texto):
                self.assertIn("missing or invalid review date", problemas_regras(texto, self.raiz, self.hoje))

    def test_contradicao_de_acesso(self):
        for caminho in ("/root", "/etc"):
            for permitir, negar in (
                ("Full access to", "Never read anything in"),
                ("You may read", "Access denied to"),
                ("Acesso total a", "Não leia nada em"),
                ("Pode acessar", "Nunca acesse"),
            ):
                with self.subTest(caminho=caminho, negar=negar):
                    for separador in ("; ", ". ", "\n- ", "\n"):
                        texto = f"{permitir} `{caminho}`{separador}{negar} `{caminho}`."
                        self.assertIn(f"contradictory access: {caminho}", self.verificar(texto))
                        self.assertIn(f"contradictory access: {caminho}", self.verificar(texto.replace("`", "")))

    @unittest.skipIf(sys.platform == "win32", "full mode and the rules checker target Linux; Windows runs lite")
    def test_negar_sozinho_nao_e_erro(self):
        self.assertEqual(self.verificar("Never read `/root`."), [])
        self.assertEqual(self.verificar("Full access to /root and /etc."), [])
        self.assertEqual(self.verificar("Secrets: never print the content of files under /root."), [])

    @unittest.skipIf(sys.platform == "win32", "full mode and the rules checker target Linux; Windows runs lite")
    def test_caminhos_relativos_absolutos_e_parametrizados(self):
        script = self.raiz / "scripts" / "verificar.py"
        script.parent.mkdir()
        script.touch()
        tarefa = self.raiz / "tarefas" / "20.md"
        tarefa.parent.mkdir()
        tarefa.touch()
        self.assertEqual(self.verificar(f"`scripts/verificar.py` and `{script}`."), [])
        self.assertEqual(self.verificar("`tarefas/<id>.md`"), [])
        script.unlink()
        erros = self.verificar(f"`scripts/verificar.py` and {script}")
        self.assertIn("path does not exist: scripts/verificar.py", erros)
        self.assertIn(f"path does not exist: {script}", erros)
        self.assertIn("path does not exist: scripts/verificar.py", self.verificar("See scripts/verificar.py."))

    def test_arquivo_simples_e_url(self):
        for texto in ("ausente.py", "ausente.py.", "`ausente.py`"):
            self.assertIn("path does not exist: ausente.py", self.verificar(texto))
        self.assertEqual(self.verificar("https://example.invalid/nao-e-arquivo.py"), [])

    def test_template_ausente_e_limite_inclusivo(self):
        caminho = self.raiz / "AGENTS.exemplo.md"
        self.assertEqual(problemas_template(caminho), ["template missing: AGENTS.exemplo.md"])
        caminho.write_text("rule\n" * 40, encoding="utf-8")
        self.assertEqual(problemas_template(caminho), [])
        caminho.write_text("rule\n" * 41, encoding="utf-8")
        self.assertEqual(problemas_template(caminho), ["template longer than 40 lines: AGENTS.exemplo.md"])


if __name__ == "__main__":
    unittest.main()
