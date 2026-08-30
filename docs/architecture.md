# Arquitetura da V1

## Componentes

- `models`: dataclasses e enums do dominio, sem dependencias de infraestrutura.
- `services`: providers de task, prompts, parsing de resultados, validacao, debug isolado e maquina de workflow.
- `clients`: Trello HTTP, GitHub Issues/PRs via `gh`, API de sessoes Jules, Codex e Git.
- `repositories`: entidades SQLAlchemy, serializacao e `WorkflowRepository`.
- `config`: settings de ambiente, merge YAML e fabrica de sessoes.
- `cli`: composicao das dependencias e interface Typer.

## Sequencia

1. A CLI seleciona exatamente um `TaskProvider`: Trello ou GitHub Issues.
2. O workflow cria um registro minimo e solicita ao provider um `DevelopmentTask` comum.
3. `task.repository` ou o custom field Trello `Repository` resolve uma entrada do registro global.
4. Defaults, configuracao registrada e `.orchestrator.yml` do alvo sao mesclados.
5. Jules cria uma sessao remota read-only com `AUTOMATION_MODE_UNSPECIFIED`; seu ID e URL sao
   persistidos antes do polling. A spec parte da branch base e o review parte da feature branch.
6. Ambiguidade bloqueante encerra em `NEEDS_HUMAN`.
7. Git cria uma branch nova a partir da branch base, recusando qualquer branch protegida.
8. Codex recebe a task original e a spec, e trabalha apenas nessa branch.
9. Validacoes configuradas/detectadas rodam antes de commit e push.
10. Git cria commit e push; o `gh` autenticado cria o PR; Jules revisa task, spec, diff, PR e checks.
11. O review devolve decisão estruturada (`APPROVED`, `CHANGES_REQUESTED` ou `NEEDS_HUMAN`).
    JSON inválido/contraditório ou estados Jules aguardando intervenção humana viram
    `NEEDS_HUMAN`; falhas operacionais viram `FAILED`.

O comando `debug` usa uma `WorkflowExecution` persistida como contrato de entrada e chama apenas a
etapa selecionada. Ele não atualiza `workflow_executions`/`workflow_steps` nem inicia etapas
seguintes; `--dry-run` valida os snapshots e a configuração sem executar integrações.

Cada operacao logica cria uma linha em `workflow_steps`. `current_stage` e atualizado antes de
cada etapa, permitindo que uma V2 selecione um ponto de retomada sem mudar o schema conceitual.

## Persistencia

`workflow_executions` guarda `task_source`, `task_id`, `task_url`, identificadores operacionais,
IDs/URLs das sessoes Jules e
snapshots JSON de task, spec, implementacao, validacao e review. `workflow_steps` guarda inicio, fim, estado e erro de cada
etapa. As colunas Trello legadas permanecem para compatibilidade. As foreign keys usam
`ON DELETE CASCADE`; indices atendem busca por origem/ID, card legado, status e etapa.

## Limites intencionais

Nao ha auto-merge, aprovação formal no GitHub, GitHub App/bot, retomada automatica, processamento
em lote, polling de quota, notificacoes ou ciclo de correcao apos review. Falhas externas sao
terminais e ficam persistidas para diagnostico e futura retomada manual.
