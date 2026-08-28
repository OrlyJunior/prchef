# Arquitetura da V1

## Componentes

- `models`: dataclasses e enums do dominio, sem dependencias de infraestrutura.
- `services`: prompts, parsing de resultados, validacao e maquina de workflow.
- `clients`: Trello HTTP, API oficial de sessoes Jules, Codex, Git e GitHub CLI.
- `repositories`: entidades SQLAlchemy, serializacao e `WorkflowRepository`.
- `config`: settings de ambiente, merge YAML e fabrica de sessoes.
- `cli`: composicao das dependencias e interface Typer.

## Sequencia

1. A CLI cria um registro minimo de execucao antes de acessar o Trello.
2. O card e carregado e preservado como snapshot JSON.
3. A referencia `Repository` do card resolve uma entrada do registro global.
4. Defaults, configuracao registrada e `.orchestrator.yml` do alvo sao mesclados.
5. Jules cria uma sessao remota read-only; seu ID e URL sao persistidos antes do polling.
6. Ambiguidade bloqueante encerra em `NEEDS_HUMAN`.
7. Git cria uma branch nova a partir da branch base, recusando qualquer branch protegida.
8. Codex recebe a task original e a spec, e trabalha apenas nessa branch.
9. Validacoes configuradas/detectadas rodam antes de commit e push.
10. Git cria commit e push; GitHub cria o PR; Jules revisa task, spec, diff, PR e checks.
11. O resultado final e `APPROVED`, `CHANGES_REQUESTED`, `NEEDS_HUMAN` ou `FAILED`.

Cada operacao logica cria uma linha em `workflow_steps`. `current_stage` e atualizado antes de
cada etapa, permitindo que uma V2 selecione um ponto de retomada sem mudar o schema conceitual.

## Persistencia

`workflow_executions` guarda os identificadores operacionais, IDs/URLs das sessoes Jules e
snapshots JSON de task, spec, implementacao, validacao e review. `workflow_steps` guarda inicio, fim, estado e erro de cada
etapa. As foreign keys usam `ON DELETE CASCADE`; indices atendem busca por card, status e etapa.

## Limites intencionais

Nao ha auto-merge, retomada automatica, processamento em lote, polling de quota, notificacoes
ou ciclo de correcao apos review. Falhas externas sao terminais e ficam persistidas para
diagnostico e futura retomada manual.
