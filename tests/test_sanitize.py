import json
import tempfile
import sys
import unittest
from pathlib import Path

from ai_agents_orchestrator import sanitize, espelho
from ai_agents_orchestrator.config import Config

# Todas as amostras abaixo sao SINTETICAS (nenhum valor real).
FAKE_SK = "sk-" + "Ab1Cd2Ef3Gh4Ij5Kl6Mn7"
FAKE_TG = "987654321:" + "Zz9" * 12
FAKE_GH = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3"
FAKE_JWT = "eyJ" + "a" * 12 + "." + "b" * 12 + "." + "c" * 8
FAKE_PEM = "-----BEGIN OPENSSH " + "PRIVATE KEY-----\nAAAAFAKEFAKEFAKE\n-----END OPENSSH " + "PRIVATE KEY-----"  # synthetic, split for secret scanners


class MascarasPII(unittest.TestCase):
    def test_pii_sempre_mascarado(self):
        t = ("CPF 123.456.789-09, CNPJ 12.345.678/0001-95, fone (11) 99999-0000, +55 11 99999-0001, "
             "5511999990002@c.us, 11999990003, fixo (11) 3333-4444, e-mail fulano@exemplo.org")
        m = sanitize.mascarar(t)
        for sobra in ("123.456.789-09", "12.345.678/0001-95", "99999-0000", "99999-0001", "5511999990002", "11999990003",
                      "3333-4444", "fulano@exemplo.org"):
            self.assertNotIn(sobra, m)
        self.assertIn("[cpf-removed]", m)
        self.assertIn("[email-removed]", m)

    def test_email_do_dono_permitido_por_dominio_e_lista(self):
        self.assertIn("ok@example.com", sanitize.mascarar("ok@example.com"))  # default allowed domain
        self.assertNotIn("ok@mycompany.test", sanitize.mascarar("ok@mycompany.test"))
        dominios = set(sanitize.DOMINIOS_PERMITIDOS)
        sanitize.configurar(dominios=["mycompany.test"], emails=["dono@exemplo.org"])
        try:
            self.assertIn("ok@mycompany.test", sanitize.mascarar("ok@mycompany.test"))
            self.assertIn("dono@exemplo.org", sanitize.mascarar("dono@exemplo.org"))
        finally:
            sanitize.configurar(dominios=dominios, emails=[])

    def test_numeros_comuns_e_hash_git_ficam(self):
        t = "commit 0123456789abcdef0123456789abcdef01234567 porta 8797 epoch 1794229673 versao 2.1.318"
        self.assertEqual(sanitize.mascarar(t), t)

    def test_idempotente(self):
        t = "mail a@b.co cpf 111.222.333-44"
        self.assertEqual(sanitize.mascarar(sanitize.mascarar(t)), sanitize.mascarar(t))


class MascarasSegredos(unittest.TestCase):
    def test_outros_servicos_sempre(self):
        t = (f"bot {FAKE_TG} chave {FAKE_PEM} senha de app: abcd efgh ijkl mnop RESTIC_PASSWORD=hunter2 "
             "AKIA" + "A" * 16)
        m = sanitize.mascarar(t)
        for sobra in (FAKE_TG, "AAAAFAKEFAKEFAKE", "abcd efgh ijkl mnop", "hunter2", "AKIA" + "A" * 16):
            self.assertNotIn(sobra, m)

    def test_palavras_comuns_nao_viram_senha_de_app(self):
        t = "para como mais mesmo isso aqui sera bem"
        self.assertEqual(sanitize.mascarar(t), t)

    def test_segredos_de_dev_mascarados_por_padrao_e_liberados_so_se_configurado(self):
        t = f"OPENAI_API_KEY={FAKE_SK} token {FAKE_GH} Authorization: Bearer {FAKE_JWT}"
        padrao = sanitize.mascarar(t)  # privacy.mask_dev_secrets = true (default)
        for sobra in (FAKE_SK, FAKE_GH, FAKE_JWT):
            self.assertNotIn(sobra, padrao)
        self.assertIn(FAKE_SK, sanitize.mascarar(t, dev=False))  # operator opted out
        sanitize.configurar(mascarar_dev=False)
        try:
            self.assertIn(FAKE_SK, sanitize.mascarar(t))
        finally:
            sanitize.configurar(mascarar_dev=True)

    def test_mascarar_obj_mantem_json_valido(self):
        ev = {"type": "item.completed", "item": {"text": f"export OPENAI_API_KEY={FAKE_SK}", "n": 3, "l": [FAKE_GH]}}
        m = sanitize.mascarar_obj(ev)
        s = json.dumps(m)
        self.assertNotIn(FAKE_SK, s)
        self.assertNotIn(FAKE_GH, s)
        self.assertEqual(m["item"]["n"], 3)

    def test_contagem_para_relatorio(self):
        _, c = sanitize.mascarar_com_contagem("a@b.co 111.222.333-44 " + FAKE_PEM)
        self.assertIn("email", c)
        self.assertIn("cpf", c)
        self.assertIn("chave-privada", c)


class RegistroTranscript(unittest.TestCase):
    def test_resultado_de_gmail_e_de_arquivo_sensivel_cai(self):
        tools = {}
        sanitize.registro_para_espelho({"type": "assistant", "timestamp": "t", "message": {"content": [
            {"type": "tool_use", "id": "1", "name": "mcp__claude_ai_Gmail__search_threads", "input": {"q": "vaga"}}]}}, tools)
        r = sanitize.registro_para_espelho({"type": "user", "timestamp": "t", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "1", "content": "Assunto: candidatura de Fulano"}]}}, tools)
        self.assertIn("removed", r[0]["texto"])
        self.assertNotIn("Fulano", json.dumps(r))
        sanitize.registro_para_espelho({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "2", "name": "Bash", "input": {"command": "cat /etc/mail-service/.env"}}]}}, tools)
        r = sanitize.registro_para_espelho({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "2", "content": "SENHA=xyz"}]}}, tools)
        self.assertIn("removed", r[0]["texto"])

    def test_erro_de_limite_preserva_reset_e_tipo(self):
        d = {"type": "assistant", "timestamp": "2026-11-08T03:53:11Z", "isApiErrorMessage": True, "error": "rate_limit",
             "apiError": "usage_limit_reached", "apiErrorStatus": 429, "uuid": "u1",
             "apiErrorParams": {"rate_limit_info": {"status": "rejected", "resetsAt": 1794200400, "rateLimitType": "seven_day"}},
             "message": {"content": [{"type": "text", "text": "You've hit your weekly limit · resets Nov 9, 2am (America/Montevideo)"}]}}
        r = sanitize.registro_para_espelho(d, {})[0]
        self.assertEqual((r["papel"], r["reset_epoch"], r["tipo_limite"], r["apiError"]), ("erro", 1794200400, "seven_day", "usage_limit_reached"))
        self.assertIn("weekly limit", r["texto"])

    def test_subagente_e_anexos_ignorados(self):
        self.assertEqual(sanitize.registro_para_espelho({"type": "assistant", "isSidechain": True, "message": {"content": "x"}}, {}), [])
        self.assertEqual(sanitize.registro_para_espelho({"type": "attachment"}, {}), [])


def jsonl(registros):
    return "".join(json.dumps(r) + "\n" for r in registros)


class Cauda(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = Path(self.tmp.name)

    def sessao(self, n=100):
        regs = []
        for i in range(n):
            regs.append({"type": "user", "timestamp": f"2026-11-08T10:{i % 60:02d}:00Z", "uuid": f"u{i}",
                         "message": {"content": f"pergunta {i} de fulano@exemplo.org"}})
            regs.append({"type": "assistant", "timestamp": f"2026-11-08T10:{i % 60:02d}:30Z", "uuid": f"a{i}",
                         "message": {"content": [{"type": "text", "text": f"resposta {i}"},
                                                 {"type": "tool_use", "id": f"t{i}", "name": "Bash", "input": {"command": "x" * 5000}}]}})
        regs.append({"type": "assistant", "timestamp": "2026-11-08T12:00:00Z", "uuid": "lim", "isApiErrorMessage": True,
                     "error": "rate_limit", "apiError": "usage_limit_reached",
                     "apiErrorParams": {"rate_limit_info": {"resetsAt": 1794200400, "rateLimitType": "five_hour"}},
                     "message": {"content": [{"type": "text", "text": "You've hit your session limit · resets 2:40pm (America/Montevideo)"}]}})
        p = self.d / "s.jsonl"
        p.write_text(jsonl(regs))
        return p

    def test_so_a_cauda_sem_saida_de_ferramenta_e_mascarada(self):
        c = espelho.cauda_transcript(self.sessao(), max_msgs=10)
        textos = [r for r in c if r["papel"] in ("usuario", "assistente")]
        self.assertEqual(len(textos), 10)
        self.assertTrue(all(r["papel"] in ("usuario", "assistente", "erro") for r in c))
        self.assertTrue(any(r["papel"] == "erro" and r["tipo_limite"] == "five_hour" for r in c))
        self.assertNotIn("fulano@exemplo.org", json.dumps(c))
        self.assertLess(len(json.dumps(c)), 5000)  # nada de 5000 chars de comando

    @unittest.skipIf(sys.platform == "win32", "full mode and the rules checker target Linux; Windows runs lite")
    @unittest.skipIf(sys.platform == "win32", "Linux-only: POSIX paths, pwd/uid or full mode")
    def test_espelhar_grava_cauda_memoria_hook_e_leiame(self):
        src = self.d / "proj"
        (src / "memory").mkdir(parents=True)
        (src / "x.jsonl").write_text(jsonl([{"type": "user", "timestamp": "2026-11-08T10:00:00Z", "message": {"content": "oi"}}]))
        (src / "memory" / "a.md").write_text("fone (11) 99999-0000 regra: python")
        cfg = Config(state_dir=self.d / "st", fonte_transcripts=src, fonte_memoria=src / "memory", repos={})
        r = espelho.espelhar(cfg, hook_payload={"error": "rate_limit", "last_assistant_message": "API Error: Rate limit reached"},
                             com_repos=False)
        self.assertTrue(r["ok"])
        self.assertTrue((cfg.mirror / "transcript-cauda.jsonl").exists())
        self.assertNotIn("99999-0000", (cfg.mirror / "memoria" / "a.md").read_text())
        self.assertEqual(json.loads((cfg.mirror / "hook-sinal.json").read_text())["error"], "rate_limit")
        self.assertIn("BRIEFING.md", (cfg.mirror / "LEIAME.md").read_text())


if __name__ == "__main__":
    unittest.main()
