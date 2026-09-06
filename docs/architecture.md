# Arquitetura da V1

## Componentes

- `models`: dataclasses e enums do dominio, sem dependencias de infraestrutura.
- `services`: providers de task, prompts, parsing de resultados, validacao, debug isolado e maquina de workflow.
- `providers`: contrato comum de agentes, adaptadores e registry explicito por tipo.
- `clients`: Trello HTTP, GitHub Issues/PRs via `gh`, API de sessoes Jules, Codex e Git.
- `repositories`: entidades SQLAlchemy, serializacao e `WorkflowRepository`.
- `config`: settings de ambiente, merge YAML e fabrica de sessoes.
- `cli`: composicao das dependencias e interface Typer.

## Sequencia

1. A CLI seleciona exatamente um `TaskProvider`: Trello ou GitHub Issues.
2. O workflow cria um registro minimo e solicita ao provider um `DevelopmentTask` comum.
3. `task.repository` ou o custom field Trello `Repository` resolve uma entrada do registro global.
4. Defaults e a configuracao registrada em `repositories.<key>` do `orchestrator.yml` sao
   mesclados. Um `.orchestrator.yml` no alvo e ignorado com aviso.
5. O provider associado a `agents.planner` recebe um `AgentRequest`. Com Jules, o adaptador cria
   uma sessao remota read-only com `AUTOMATION_MODE_UNSPECIFIED`; seu ID e URL sao persistidos antes
   do polling. Com `gemini-vertex`, o adaptador faz uma chamada Vertex AI request/response usando
   ADC, sem acesso a filesystem, shell ou ferramentas locais. A spec parte da branch base e o review
   parte da feature branch.
6. Ambiguidade bloqueante encerra em `NEEDS_HUMAN`.
7. Git cria uma branch nova a partir da branch base, recusando qualquer branch protegida.
8. O provider associado a `agents.implementer` recebe a task original e a spec, e trabalha apenas
   nessa branch. O adaptador Codex preserva o sandbox e o subprocesso existentes.
9. Validacoes configuradas/detectadas rodam antes de commit e push.
10. Git cria commit e push; o `gh` autenticado cria o PR; o provider associado a
    `agents.reviewer` revisa task, spec, diff, PR e checks.
11. O review devolve decisão estruturada (`APPROVED`, `CHANGES_REQUESTED` ou `NEEDS_HUMAN`).
    JSON inválido/contraditório ou estados Jules aguardando intervenção humana viram
    `NEEDS_HUMAN`; falhas operacionais viram `FAILED`.

O comando `debug` monta inputs explícitos e chama apenas a etapa selecionada. Uma
`WorkflowExecution` pode ser informada por `--from-execution` como fonte somente leitura de defaults,
mas não é necessária para uma invocação limpa. O debug não atualiza `workflow_executions`/
`workflow_steps`, não inicia etapas seguintes e `--dry-run` não executa integrações. Na etapa de
implementação, o Codex usa o caminho do repositório configurado/selecionado e não exige, troca ou
cria a `working_branch` histórica; não há commit, push ou PR. Isso é deliberadamente separado de uma
futura operação `resume`, que poderá continuar o estado Git persistido.

Os tres services semanticos dependem do mesmo contrato pequeno:

```text
Spec/Implementation/Review service
              |
              v
AgentProvider.execute(AgentRequest) -> AgentResult
              |
              v
JulesClient (REST/polling), CodexClient (CLI/subprocesso) ou Gemini Vertex (SDK/Vertex AI)
```

O papel define as instrucoes e a expectativa de output estruturado. O provider resolve o mecanismo
de execucao. `AgentProviderRegistry` associa `providers.<nome>.type` a um adaptador explicito; task
providers de GitHub/Trello continuam sendo um dominio separado.

O provider `codex-cli` recebe `model` e `reasoning_effort` somente na sua propria configuracao e
os traduz para argumentos da CLI. Os services continuam conhecendo apenas `AgentProvider`; eles nao
selecionam modelo, esforco de raciocinio ou fallback de runtime.

O adapter `gemini-vertex` traduz `StructuredOutputExpectation` para `application/json` e
`response_json_schema` nativos do `google-genai`. Tipos e excecoes do SDK permanecem dentro do
adapter; o restante do workflow recebe apenas `AgentResult` e erros neutros de provider.

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
