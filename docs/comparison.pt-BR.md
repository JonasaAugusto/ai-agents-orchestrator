# Comparação honesta

Conferido nos READMEs oficiais vinculados em **12/11/2026**; não instalamos nem medimos as alternativas.
As indicações de preferência são nossa interpretação dos fluxos documentados, sem ranking de qualidade.
Aqui Hydra significa `Geraldlol/hydra` e codex-router significa `gitguffaw/codex-router`; existem homônimos.

| Alternativa e fonte | O que faz bem segundo a documentação | Quando preferir |
| --- | --- | --- |
| [openai/codex-plugin-cc](https://github.com/openai/codex-plugin-cc#readme) | Revisão, delegação em segundo plano, resgate e transferência de sessão dentro do Claude Code. | Integração direta da OpenAI sem adotar nosso modelo de serviço/daemon root. Já tem handoff; isso não é exclusivo nosso. |
| [kingbootoshi/codex-orchestrator](https://github.com/kingbootoshi/codex-orchestrator#readme) | Agentes em tmux, paralelismo, acompanhamento e direcionamento de sessões. | Supervisionar vários workers de terminal é a prioridade e Bun/tmux cabem no ambiente. |
| [jarrodwatts/claude-delegator](https://github.com/jarrodwatts/claude-delegator#readme) | Papéis especialistas e roteamento via MCP. O README atual informa arquivamento e incompatibilidade do Codex atual. | Estudar os prompts ou manter ambiente legado fixado; para adoção nova com Codex atual, o autor recomenda o plugin oficial acima. |
| [mustafaakben/agent-codex](https://github.com/mustafaakben/agent-codex#readme) | Comandos, agentes, skills e templates de bootstrap/MCP com ativação explícita. | Fluxo centrado no plugin com integrações MCP, sem serviço persistente de revezamento. |
| [madd86/codex-bridge](https://github.com/madd86/codex-bridge#readme) | Prompts contextualizados, delegação tmux e revisão supervisionada; o lançamento documentado usa `--yolo`. | Ponte pequena em ambiente local confiável, aceitando esse modo de permissão. |
| [LegCLI](https://legcli.com/) | Monitora janelas de uso e troca modelos/agentes por uma escada configurada, preservando contexto no terminal. | Continuidade interativa entre CLIs é o principal; confira os termos comerciais atuais. |
| [Hydra](https://github.com/Geraldlol/hydra#readme) | Sala persistente no VS Code, transcrição compartilhada e fases de planejamento, execução, verificação e revisão. | Colaboração dentro do editor e decisões registradas são mais úteis que um quadro com systemd. |
| [codex-router](https://github.com/gitguffaw/codex-router#readme) | Delegação/revisão por políticas, seleção de modelo/esforço e jobs persistidos para Claude Code/Antigravity. | Roteamento e integração de hosts são o foco e você não precisa do nosso canal remoto. |

Nosso foco é a combinação de **reserva com devolução ao coordenador**, branches isolados, aprovação de publicação
presa ao SHA revisado e destino, canal remoto do dono e equilíbrio de uso medido. Veja [o fluxo](../README.pt-BR.md#como-funciona),
[as limitações de segurança](../SECURITY.md) e a [medição](../README.md#usage-measurement-and-balance-110).
Equilíbrio usa contadores e cotas configuradas: não otimiza valores em dinheiro nem promete economia.
Um agente root pode burlar a aprovação da aplicação e o isolamento entre processos do mesmo UID é limitado.
Serviço, daemon root e configuração têm custo de adoção; uma integração menor pode atender melhor uma sessão de revisão.


Voltar ao [README](../README.pt-BR.md#início-rápido-modo-leve-121) · [política de suporte](support-policy.pt-BR.md).
