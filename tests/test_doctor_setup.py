"""`aao doctor` and `aao setup` with fakes: no real codex/claude/git/network, no credential file is ever read."""
import io
import os
import stat
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from ai_agents_orchestrator import assistente, config, doctor, engines
from ai_agents_orchestrator.config import Config
from ai_agents_orchestrator.testing import CodexFalso
from tests.helpers import Base


class AmbienteFalso(doctor.Ambiente):
    def __init__(self, progs=("codex", "claude", "git"), login_ok=True, git_ok=True, usuario=True, arquivos=None,
                 modulos=()):
        self.progs, self.login_ok, self.git_ok, self.usuario = set(progs), login_ok, git_ok, usuario
        self.arquivos = arquivos or {}
        self.modulos = set(modulos)
        self.comandos = []

    def which(self, prog): return f"/usr/bin/{prog}" if prog in self.progs else None

    def rodar(self, cmd, env=None, timeout=30):
        self.comandos.append(cmd)
        if "login" in cmd:
            return (0, "Logged in using ChatGPT") if self.login_ok else (1, "Not logged in")
        if cmd[0] == "git" and "ls-remote" in cmd:
            return (0, "abc\tHEAD") if self.git_ok else (128, "fatal")
        return 1, ""

    def usuario_existe(self, nome): return self.usuario
    def euid(self): return 1000
    def socket_ativo(self, path): return True

    def stat(self, p):
        p = str(p)
        if p in self.arquivos:
            modo, uid = self.arquivos[p]
            return os.stat_result((modo | stat.S_IFREG, 0, 0, 1, uid, 0, 0, 0, 0, 0))
        return None

    def ler(self, p): return self.arquivos.get(str(p) + ":conteudo")
    def modulo(self, nome): return nome in self.modulos


def estados(itens):
    return {i.nome: i.estado for i in itens}


class Doctor(unittest.TestCase):
    def setUp(self):
        self.cfg = Config(state_dir=Path("/var/lib/x"), channel="none",
                          repos={"app": {"github": "o/app", "branch_base": "main"}}, push_allowlist=["o/app"])

    def test_tudo_ok_sem_canal_mostra_o_aviso(self):
        amb = AmbienteFalso(arquivos={str(config.CONFIG_FILE): (0o644, 0), str(self.cfg.rules_file): (0o644, 0),
                                      "/h/settings.json:conteudo": '{"hooks": "aao claude-limit"}'})
        saida = []
        rc = doctor.main(self.cfg, amb=amb, saida=saida.append, settings=[Path("/h/settings.json")])
        self.assertEqual(rc, 0, saida)
        texto = "\n".join(saida)
        self.assertIn("Without a chat channel you cannot send prompts to Codex from your phone", texto)
        self.assertIn("Telegram is the channel that enables this", texto)

    def test_codex_sem_login_mostra_so_o_comando_oficial(self):
        amb = AmbienteFalso(login_ok=False)
        itens = doctor.checar_codex(self.cfg, amb)
        self.assertEqual(itens[0].estado, doctor.FALHA)
        self.assertIn("codex login --device-auth", itens[0].correcao)
        self.assertIn(f"sudo -u {self.cfg.service_user}", itens[0].correcao)
        self.assertEqual(amb.comandos[-1][-3:], ["codex", "login", "status"])
        self.assertFalse(any("auth.json" in " ".join(c) for c in amb.comandos))

    def test_codex_nao_instalado(self):
        itens = doctor.checar_codex(self.cfg, AmbienteFalso(progs=()))
        self.assertEqual((itens[0].nome, itens[0].estado), ("codex CLI", doctor.FALHA))

    def test_telegram_sem_token_e_env_inseguro(self):
        self.cfg.channel = "telegram"
        amb = AmbienteFalso(arquivos={str(config.ENV_FILE): (0o644, 0)})
        e = estados(doctor.checar_canal(self.cfg, amb))
        self.assertEqual(e["telegram token"], doctor.FALHA)
        self.assertEqual(e["telegram user id"], doctor.FALHA)
        self.assertEqual(e["secrets env file"], doctor.FALHA)
        itens = doctor.checar_canal(self.cfg, amb)
        self.assertTrue(any("@BotFather" in i.correcao for i in itens))
        self.assertTrue(any("@userinfobot" in i.correcao for i in itens))
        self.cfg.token, self.cfg.chat_id = "1:abc", 5
        amb = AmbienteFalso(arquivos={str(config.ENV_FILE): (0o600, 0)})
        self.assertTrue(all(i.estado == doctor.OK for i in doctor.checar_canal(self.cfg, amb)))

    def test_whatsapp_mostra_o_risco_e_exige_opt_in(self):
        self.cfg.channel = "whatsapp"
        itens = doctor.checar_canal(self.cfg, AmbienteFalso())
        texto = " ".join(i.detalhe for i in itens)
        for trecho in ("UNOFFICIAL", "not affiliated with WhatsApp or Meta", "banned", "secondary number", "own risk"):
            self.assertIn(trecho, texto)
        e = estados(itens)
        self.assertEqual(e["whatsapp opt-in"], doctor.FALHA)
        self.assertEqual(e["whatsapp library"], doctor.FALHA)

    def test_repos_inacessiveis_e_fora_da_allowlist(self):
        e = estados(doctor.checar_repos(self.cfg, AmbienteFalso(git_ok=False)))
        self.assertEqual(e["repo app"], doctor.FALHA)
        self.cfg.push_allowlist = []
        e = estados(doctor.checar_repos(self.cfg, AmbienteFalso()))
        self.assertEqual(e["repo app"], doctor.AVISO)
        self.cfg.repos = {}
        self.assertEqual(doctor.checar_repos(self.cfg, AmbienteFalso())[0].estado, doctor.FALHA)

    def test_full_access_e_wakeup_geram_aviso(self):
        self.cfg.codex_full_access = True
        self.cfg.claude_wakeup = True
        e = estados(doctor.checar_base(self.cfg, AmbienteFalso()))
        self.assertEqual(e["codex sandbox"], doctor.AVISO)
        self.assertEqual(e["claude wake-up"], doctor.AVISO)

    def test_json(self):
        saida = []
        doctor.main(self.cfg, como_json=True, amb=AmbienteFalso(), saida=saida.append, settings=[Path("/nao")])
        self.assertIn('"nome"', saida[0])


class LoginOficial(unittest.TestCase):
    def test_status_login_usa_o_comando_oficial(self):
        visto = {}

        def rodar(cmd, env):
            visto["cmd"], visto["env"] = cmd, env
            return type("R", (), {"returncode": 0, "stdout": "Logged in using ChatGPT", "stderr": ""})()
        r = engines.status_login_codex(Path("/var/lib/x"), "codex", runner=rodar)
        self.assertTrue(r["logado"])
        self.assertEqual(visto["cmd"], ["codex", "login", "status"])
        self.assertEqual(visto["env"]["CODEX_HOME"], "/var/lib/x/.codex")

    def test_nao_logado_e_nao_instalado(self):
        nao = lambda cmd, env: type("R", (), {"returncode": 1, "stdout": "Not logged in", "stderr": ""})()
        self.assertFalse(engines.status_login_codex(Path("/x"), runner=nao)["logado"])

        def ausente(cmd, env):
            raise FileNotFoundError
        r = engines.status_login_codex(Path("/x"), runner=ausente)
        self.assertEqual((r["instalado"], r["logado"]), (False, False))


class LoginExpirado(Base):
    def test_login_expirado_pausa_a_fila_e_diz_como_relogar(self):
        from ai_agents_orchestrator.engines import Resultado
        self.criar_repo()
        self.quadro.criar("T", "", "web-app", origem="dono")
        self.codex.respostas = [Resultado(ok=False, auth_erro=True, erro="401", segundos=1)]
        self.svc.ciclo_worker()
        self.assertTrue(self.estado.get("pausado"))
        txt = self.tr.textos()[-1]
        self.assertIn("codex login --device-auth", txt)
        self.assertIn("Queue paused", txt)


class Setup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = Path(self.tmp.name)

    def rodar(self, respostas, parear=None):
        it = iter(respostas)
        saida = []
        a = assistente.Assistente(entrada=lambda _p: next(it), saida=saida.append, config_file=self.d / "config.toml",
                                  env_file=self.d / "env", parear=parear)
        self.assertEqual(a.executar(), 0)
        return "\n".join(saida)

    def test_telegram_grava_config_e_segredos_0600(self):
        token = "123456:" + "A" * 30
        out = self.rodar(["aao", str(self.d / "st"), "UTC", "", "telegram", token, "777",
                          "app", "o/app", "main", "y", ""])
        cfg = tomllib.loads((self.d / "config.toml").read_text())
        self.assertEqual(cfg["service"]["channel"], "telegram")
        self.assertEqual(cfg["telegram"]["chat_id"], 777)
        self.assertEqual(cfg["publish"]["push_allowlist"], ["o/app"])
        self.assertFalse(cfg["codex"]["full_access"])
        self.assertNotIn(token, (self.d / "config.toml").read_text())
        env = (self.d / "env").read_text()
        self.assertIn(f"AAO_TELEGRAM_TOKEN={token}", env)
        self.assertEqual(stat.S_IMODE(os.stat(self.d / "env").st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.d / "publish.key").st_mode), 0o600)
        self.assertNotIn(token, out)  # never echoed
        self.assertIn("@BotFather", out)
        self.assertIn("codex login --device-auth", out)
        c = config.carregar({"AAO_CONFIG": str(self.d / "config.toml")})
        self.assertEqual(c.repos["app"]["branch_base"], "main")
        config.definir_fuso("America/Montevideo")

    def test_sem_canal_mostra_o_aviso(self):
        out = self.rodar(["aao", str(self.d / "st"), "UTC", "", "none", ""])
        self.assertIn("Without a chat channel you cannot send prompts to Codex from your phone", out)
        self.assertEqual(tomllib.loads((self.d / "config.toml").read_text())["service"]["channel"], "none")

    def test_whatsapp_exige_aceite_explicito(self):
        out = self.rodar(["aao", str(self.d / "st"), "UTC", "", "whatsapp", "yes", ""])
        self.assertIn("not affiliated with WhatsApp or Meta", out)
        self.assertEqual(tomllib.loads((self.d / "config.toml").read_text())["service"]["channel"], "none")
        pareados = []
        self.rodar(["aao", str(self.d / "st"), "UTC", "", "whatsapp", "I ACCEPT THE RISK", "15550100000", ""],
                   parear=pareados.append)
        cfg = tomllib.loads((self.d / "config.toml").read_text())
        self.assertTrue(cfg["whatsapp"]["accept_unofficial_risk"])
        self.assertEqual(cfg["whatsapp"]["owner_number"], "15550100000")
        self.assertEqual(len(pareados), 1)


if __name__ == "__main__":
    unittest.main()
