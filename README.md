# Autonomous Development Orchestrator — V1

CLI em Python que executa e persiste o fluxo:

```text
Trello Card   ─┐
               ├─> Jules (spec read-only) -> Codex -> validacoes locais
GitHub Issue  ─┘   -> Git branch/commit/push -> GitHub PR -> Jules review
```

A V1 nunca faz merge, nunca envia commits diretamente a branches protegidas e encerra em
`NEEDS_HUMAN` quando a spec informa que uma ambiguidade impede uma implementacao segura ou quando
o Jules exige uma interação humana. O fluxo é sempre: task -> spec -> branch -> Codex -> validação
local -> commit -> push -> PR -> review. O merge continua inteiramente manual.

## Requisitos

- Python 3.11+
- Git
- Codex CLI autenticado
- Jules Tools CLI instalado e autenticado (`npm install -g @google/jules`, `jules login`) para
  inspecao operacional das sessoes
- `JULES_API_KEY` em variavel de ambiente para o lifecycle automatizado pela API oficial do Jules
- GitHub CLI (`gh`) autenticado, usado para GitHub Issues e Pull Requests
- credenciais Trello quando esse provider for utilizado

O adaptador Codex usa o modo oficial nao interativo `codex exec`, com workspace limitado ao
repositorio. Jules cria uma sessao remota oficial, persiste ID e URL, acompanha a execucao por
polling e recupera a ultima mensagem do agente quando a sessao chega a `COMPLETED`.

O orquestrador nao usa um launcher, `stdin` ou `stdout` para o Jules. A CLI autenticada serve
para login e inspecao manual; o ciclo programatico usa a API oficial de sessoes, que oferece
estados e atividades estruturados. Por isso a chave deve estar no ambiente do processo
(`JULES_API_KEY`, tambem aceito como `ORCHESTRATOR_JULES_API_KEY`), e nao em arquivos versionados.
O modo de automacao enviado e o modo padrao sem automacao (`AUTOMATION_MODE_UNSPECIFIED`): Jules
nao recebe autorizacao para criar PR, publicar commits ou alterar o repositorio local durante
spec/review. Ambas as sessoes tambem definem `requirePlanApproval: false`, para que a API
auto-aprove o plano em vez de parar por uma aprovação manual.

## Instalacao

```bash
python -m venv .venv
source .venv/bin/activate  # Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
cp .env.example .env       # Windows: Copy-Item .env.example .env
cp orchestrator.example.yml orchestrator.yml
```

Edite `.env` e `orchestrator.yml`. Para Trello, o card deve possuir um custom field chamado
`Repository` (ou `Repo`) cujo valor corresponda a uma chave em `repositories`; `--repository`
continua podendo sobrescrevê-lo. Para GitHub Issues, informe `--repository owner/repo` e use essa
mesma chave no registro `repositories` para associá-la ao checkout local. Antes da primeira
execucao, conecte o repositorio ao Jules. Sem `integrations.jules.source`, o cliente usa o remote
`origin` para localizar a source correspondente na API.

## Banco e migrations

No startup, os comandos da CLI aplicam automaticamente todas as migrations pendentes antes de
acessar o banco. Se uma migration falhar, o comando aborta com erro claro. O Alembic le
`ORCHESTRATOR_DATABASE_URL` do ambiente ou do arquivo `.env`.

O comando manual continua disponível para provisionamento e diagnóstico:

```bash
alembic upgrade head
```

O runtime nao chama `Base.metadata.create_all()`. Toda mudanca de schema deve ser versionada:

```bash
alembic revision --autogenerate -m "describe schema change"
alembic upgrade head
```

## Uso

```bash
orchestrator run --trello-card CARD_ID
orchestrator run --github-issue 42 --repository owner/repository
```

Overrides uteis:

```bash
orchestrator run --trello-card CARD_ID --repository example-api
orchestrator run --trello-card CARD_ID --config ./orchestrator.yml
orchestrator run --task-id 42 --repository owner/repository  # usa task.provider
orchestrator show EXECUTION_ID
```

Exatamente uma origem deve ser informada por execução. GitHub Issues é a opção recomendada para
projetos hospedados no GitHub porque reutiliza a autenticação existente do `gh` e permite que o PR
inclua `Closes #<numero>` quando issue e PR pertencem ao mesmo repositório. Para issues em outro
repositório, o PR registra apenas `owner/repo#numero`, sem solicitar fechamento automático.

Cada etapa e persistida em `workflow_executions` e `workflow_steps`. A saida final do comando e
JSON e inclui `task_source`, `task_id`, `task_url`, repositorio, branch, commit, PR, validacoes,
review e status final. As colunas Trello antigas são mantidas para leitura de execuções existentes.
As sessoes Jules de spec e review tambem ficam registradas com ID e URL para inspecao futura.
O orquestrador ainda nao retoma sessoes apos reinicio, mas esses identificadores permitem localizar
uma sessao interrompida sem criar outra.

## Observabilidade da CLI

O progresso do workflow e escrito em `stderr`, mantendo o JSON da execucao exclusivamente em
`stdout` para que possa ser consumido por scripts. Os logs mostram entrada e conclusao de cada
etapa, transicoes de estado e heartbeats do Jules, execucao das validacoes, branch/commit/push,
criação do PR e um resumo final mesmo quando o resultado e `FAILED` ou `NEEDS_HUMAN`.

Use `--verbose` para incluir detalhes adicionais de subprocessos e retentativas de polling:

```bash
orchestrator run --github-issue 42 --repository owner/repository --verbose
```

Prompts, respostas completas do Codex/Jules e credenciais nao sao incluidos nos logs. O intervalo
de polling e o timeout do Jules continuam configuraveis em `integrations.jules` (15 s e 1800 s
por padrao); `--verbose` nao altera o comportamento do workflow.

## Lifecycle do Jules

Para cada spec ou review, o client cria uma unica sessao, persiste imediatamente `session_id` e
URL e consulta seu estado ate uma decisão. Os estados de processamento (`QUEUED`, `PLANNING` e
`IN_PROGRESS`) continuam em polling; `COMPLETED` recupera a ultima `agentMessage` das activities.
Estados que aguardam interação humana e os estados de falha encerram a operação sem criar outra
sessão.

O intervalo, timeout global e retentativas de consulta ficam em `integrations.jules`. O default é
15 s, 1800 s e 3 retentativas, respectivamente. Apenas consultas transitórias (rede, 408, 429 e
5xx) recebem retry, com backoff exponencial simples; criar sessão nunca é repetido automaticamente.
`QUEUED`, `PLANNING` e `IN_PROGRESS` continuam em polling. `AWAITING_USER_FEEDBACK`, `PAUSED` e
`AWAITING_PLAN_APPROVAL` encerram imediatamente em `NEEDS_HUMAN`, preservando o ID/URL da sessão
e uma mensagem que explica a intervenção necessária. `FAILED` e `CANCELLED` encerram em `FAILED`.

## Review e Pull Request

Depois do push e da criação do PR pelo `gh` autenticado, o Jules recebe uma nova sessão de review
com a feature branch como `startingBranch`, a task, a spec, o repositório, as branches base/feature,
o PR, o diff e os resultados das validações. O resultado deve ser um JSON com `decision`
(`APPROVED`, `CHANGES_REQUESTED` ou `NEEDS_HUMAN`), `summary`, `findings`, `blocking_findings` e
`suggestions`. Resposta malformada ou contraditória vira `NEEDS_HUMAN`; o orquestrador nunca infere
aprovação de texto livre. `CHANGES_REQUESTED` não inicia uma nova rodada do Codex nesta V1.

`APPROVED` e `CHANGES_REQUESTED` são estados internos do orquestrador, não GitHub Pull Request
Reviews formais. A V1 não chama APIs de approve/request-changes, não usa bot/GitHub App e não faz
auto-merge. O PR é criado pela identidade autenticada no `gh`. Para um ruleset inicial, recomenda-se:
PR obrigatório para merge, 0 approvals obrigatórios, bloquear deleções e force pushes, exigir
resolução de conversas, sem bypass de IA/bot e sem status checks obrigatórios por enquanto. Uma
integração com GitHub App para reviews formais é evolução posterior.

## Configuracao por repositorio

Os defaults e a entrada do registro global sao mesclados com `.orchestrator.yml` no repositorio
alvo; o arquivo local tem maior precedencia. Exemplo:

```yaml
task:
  provider: github  # usado apenas com --task-id; opções explícitas têm precedência

repository:
  base_branch: develop
  protected_branches: [main, master, develop, release]
roles:
  spec: jules
  implementation: codex
  review: jules
safety:
  allow_direct_merge: false
  require_tests: true
validation:
  auto_detect: true
  commands:
    - [python, -m, pytest]
```

Providers suportados:

- `trello`: usa `ORCHESTRATOR_TRELLO_API_KEY` e `ORCHESTRATOR_TRELLO_TOKEN`.
- `github`: executa `gh issue view` com a autenticação já ativa no GitHub CLI.

`allow_direct_merge: true` e rejeitado nesta V1. Credenciais ficam exclusivamente em variaveis
de ambiente e `.env` nao e versionado.

## Validacoes

Com `validation.commands` vazio e `auto_detect: true`, a V1 reconhece configuracoes comuns de
Python (`pytest`, Ruff, mypy), Node (`npm test`, lint, typecheck), Maven e Gradle. Se
`require_tests` estiver ativo e nenhum comando for encontrado, o fluxo encerra em falha antes
de qualquer commit.

```bash
ruff check .
mypy src/orchestrator
pytest
```

O `pytest` já configura `--basetemp=.pytest-tmp/run`, um diretório relativo ao projeto ignorado
pelo Git. Isso evita a dependência de `%LOCALAPPDATA%\\Temp\\pytest-of-...` no Windows; basta executar
`python -m pytest`, sem workaround adicional.

## Arquitetura

Veja [docs/architecture.md](docs/architecture.md). Em resumo, models nao conhecem HTTP,
subprocessos ou SQLAlchemy; services coordenam regras; clients encapsulam integracoes externas;
e repositories sao a unica porta de persistencia.
