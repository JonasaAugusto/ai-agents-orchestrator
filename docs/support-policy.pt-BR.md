# Política de versão e suporte

- **SemVer**: em 1.x, comandos/configurações documentados e o contrato de tarefa/relatório/aprovação formam a interface
  de compatibilidade. Internos Python, tabelas SQLite e protocolo do socket não são API pública. Correções de segurança
  podem recusar operações inseguras antes aceitas; leia o CHANGELOG ao atualizar.
- Só a **última versão publicada** recebe correções, inclusive de segurança. Sem LTS ou prazo garantido de resposta.
  O badge aponta para releases publicados; o checkout pode conter mudanças ainda não publicadas. Não é selo de auditoria.
- CI com alvo **Python 3.11–3.14**: suíte completa em **Linux** e modo leve (sem root, sem systemd) em **Linux, macOS e
  Windows nativo**. Serviço/daemon e unidades exigem **Linux/systemd**; passar testes no macOS ou no Windows não
  certifica a operação privilegiada ali. WSL2 roda as instruções do modo leve do Linux e não tem certificação
  separada. Informe também versões dos CLIs externos ao relatar problemas.
- Núcleo suportado: delegação, revisão, defaults restritos e publicação aprovada pelo dono. **`/codex-pane` e wake-up
  interativo são experimentais**, desligados por padrão e sem garantia de compatibilidade. WhatsApp é opcional e
  não oficial. Equilíbrio exige calibração; cota desconhecida continua desconhecida.

Para repetir o teste automatizado: `sh scripts/clean-install-test.sh --empty`.
Ele cria venv/HOME/estado/Git descartáveis, instala o pacote, roda setup sem interação e doctor, simula uma tarefa
com Codex falso e valida o plugin estaticamente. Doctor deve indicar os pré-requisitos ausentes; o teste confere
exatamente essas falhas, sem ocultar outras. Não valida login real, serviços operacionais ou carregamento no Claude.
CI também testa instalação a partir de wheel/sdist e usa containers Python slim sem agentes instalados.
O setup sem interação exige diretório do usuário atual, sem escrita para grupo/outros, e cria configuração de forma
exclusiva, preservando arquivos existentes.


Voltar ao [README](../README.pt-BR.md#início-rápido-modo-leve-121) · [comparação honesta](comparison.pt-BR.md).
