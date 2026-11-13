# Agente acompanhante vs. script de fila: quando usar cada um

Os dois conduzem uma sequência de lotes do Codex, um por vez. A diferença é quem espera e quem julga.

| | Agente acompanhante ([skill](../claude-plugin/skills/orchestrator-workflow/companion-agent.md)) | Script de fila (`aao queue`) |
| --- | --- | --- |
| Quem espera | um agente (gasta tokens a cada despertar e relatório) | um processo Python (zero tokens) |
| Quem julga | o agente: lê relatórios e diffs, adapta o próximo briefing | só verificações mecânicas fixas |
| Determinístico | não | sim: mesmas verificações, mesma ordem, estado retomável |
| Melhor para | lotes cujo resultado muda o plano | lotes previsíveis (migrações, refatorações mecânicas, lotes de testes) |
| Fraco em | esperas longas, muitos lotes | tudo que exige julgamento |
| Limite de uso | o agente precisa perceber e parar | detectado por `aao status`/erro da tarefa; para e retoma sem repetir lotes |

## Recomendado: híbrido

O script conduz; um agente revisa nos pontos de parada.

1. `aao queue start lots/` roda lotes até `--pause-every N` (padrão 3) ficarem prontos, aparecer um limite de uso ou a fila acabar.
2. Escreve `NEEDS_REVIEW.md` com os branches e os comandos de revisão. Nenhum token foi gasto esperando.
3. Um agente (ou você) revisa esses lotes: lê `aao result <id> --short`, abre só os arquivos de risco, aceita ou escreve um lote de ajuste.
4. `aao queue reviewed L1 L2 L3`, depois `aao queue resume`.

Os tokens do agente vão para o julgamento, não para a espera.

## Passo a passo

```bash
mkdir lots && cp examples/queue-lots/*.md lots/        # edite repo, repo_path, files
aao status --brief                                      # serviço no ar, esforço medium, nada rodando
aao queue start lots/ --forbid-ai-mentions --pause-every 3
aao queue status            # tabela dos lotes
aao queue log -n 20         # acompanhe o que aconteceu
aao queue stop              # pede para parar na próxima checagem (a tarefa em andamento não é cancelada)
# depois de uma pausa ou de um limite de uso:
cat .aao-queue/NEEDS_REVIEW.md
aao queue reviewed L1 L2 L3
aao queue resume            # recusa enquanto o limite estiver ativo, salvo --force; nunca repete lotes prontos
```

Arquivo de lote: Markdown com cabeçalho curto (`id`, `repo`, `repo_path` opcional, `title`, `depends`, `files`, `effort`,
`model`) e o briefing no corpo. O estado fica em `.aao-queue/state.json` (`--workdir` muda a pasta).

## O que o script verifica

Depois de cada tarefa: terminou sem erro e não foi declarada parcial; o branch existe (exige `repo_path`) com commits novos
sobre `--base`; mensagens de commit sem co-autor nem menção a ferramenta/IA (só com `--forbid-ai-mentions`, mais qualquer
`--forbid-pattern`); `CHANGELOG.md` alterado (`--no-changelog-check` desliga); nenhum arquivo `.env`, chave ou token e nenhum
token, chave privada ou URL de banco com senha real nas linhas adicionadas; relato com testes verdes. Uma falha devolve o lote
uma vez no mesmo branch; a segunda o marca `blocked`, os que dependem dele viram `blocked_by_dependency` e os independentes
seguem. Nunca faz merge, publicação, push nem deploy.

## Notas

- Por que subcomando e não script em examples: só chama `aao` e `git`, não precisa de configuração do serviço e é coberto pelos
  testes do CLI; um exemplo ficaria defasado em relação às flags.
- `AAO_QUEUE_NOTIFY="cmd args"` executa um comando com a mensagem como último argumento em pausas, limites e conclusão (padrão: só log).
- `AAO_QUEUE_POLL` / `--poll` define os segundos entre checagens (padrão 60).
- O texto do relatório é lido por expressões regulares; uma redação incomum pode reprovar uma verificação, e o lote vai para o arquivo de revisão, não para produção.
