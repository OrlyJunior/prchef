# Autonomous Development Orchestrator — V1

CLI em Python que executa e persiste o fluxo:

```text
Trello -> Jules (spec read-only) -> Codex -> validacoes locais
       -> Git branch/commit/push -> GitHub PR -> Jules review
```

A V1 nunca faz merge, nunca envia commits diretamente a branches protegidas e encerra em
`NEEDS_HUMAN` quando a spec informa que uma ambiguidade impede uma implementacao segura.

## Requisitos

- Python 3.11+
- Git
- Codex CLI autenticado
- Jules Tools CLI instalado e autenticado (`npm install -g @google/jules`, `jules login`) para
  inspecao operacional das sessoes
- `JULES_API_KEY` em variavel de ambiente para o lifecycle automatizado pela API oficial do Jules
- GitHub CLI (`gh`) autenticado
- credenciais Trello

O adaptador Codex usa o modo oficial nao interativo `codex exec`, com workspace limitado ao
repositorio. Jules cria uma sessao remota oficial, persiste ID e URL, acompanha a execucao por
polling e recupera a ultima mensagem do agente quando a sessao chega a `COMPLETED`.

O orquestrador nao usa um launcher, `stdin` ou `stdout` para o Jules. A CLI autenticada serve
para login e inspecao manual; o ciclo programatico usa a API oficial de sessoes, que oferece
estados e atividades estruturados. Por isso a chave deve estar no ambiente do processo
(`JULES_API_KEY`, tambem aceito como `ORCHESTRATOR_JULES_API_KEY`), e nao em arquivos versionados.
O modo de automacao enviado e o modo padrao sem automacao: Jules nao recebe autorizacao para criar
PR, publicar commits ou alterar o repositorio local durante spec/review.

## Instalacao

```bash
python -m venv .venv
source .venv/bin/activate  # Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
cp .env.example .env       # Windows: Copy-Item .env.example .env
cp orchestrator.example.yml orchestrator.yml
```

Edite `.env` e `orchestrator.yml`. O card deve possuir um custom field Trello chamado
`Repository` (ou `Repo`) cujo valor corresponda a uma chave em `repositories`. Como alternativa
operacional, passe `--repository` com uma chave configurada ou um caminho absoluto. Antes da
primeira execucao, conecte o repositorio ao Jules. Sem `integrations.jules.source`, o cliente usa
o remote `origin` para localizar a source correspondente na API.

## Banco e migrations

Crie/atualize o schema antes da primeira execucao. O Alembic le
`ORCHESTRATOR_DATABASE_URL` do ambiente ou do arquivo `.env`:

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
```

Overrides uteis:

```bash
orchestrator run --trello-card CARD_ID --repository example-api
orchestrator run --trello-card CARD_ID --config ./orchestrator.yml
orchestrator show EXECUTION_ID
```

Cada etapa e persistida em `workflow_executions` e `workflow_steps`. A saida final do comando e
JSON e inclui card, repositorio, branch, commit, PR, validacoes, review e status final.
As sessoes Jules de spec e review tambem ficam registradas com ID e URL para inspecao futura.
O orquestrador ainda nao retoma sessoes apos reinicio, mas esses identificadores permitem localizar
uma sessao interrompida sem criar outra.

## Lifecycle do Jules

Para cada spec ou review, o client cria uma unica sessao, persiste imediatamente `session_id` e
URL e consulta seu estado ate um terminal. Os estados oficiais nao terminais (`QUEUED`,
`PLANNING`, `AWAITING_PLAN_APPROVAL`, `AWAITING_USER_FEEDBACK`, `IN_PROGRESS` e `PAUSED`) continuam
em polling; `COMPLETED` recupera a ultima `agentMessage` das activities; `FAILED` e `CANCELLED`
interrompem com erro que inclui o ID da sessao. Tambem tratamos `CANCELLED` de forma compatível caso
seja devolvido pelo serviço.

O intervalo, timeout global e retentativas de consulta ficam em `integrations.jules`. O default é
15 s, 1800 s e 3 retentativas, respectivamente. Apenas consultas transitórias (rede, 408, 429 e
5xx) recebem retry, com backoff exponencial simples; criar sessão nunca é repetido automaticamente.

## Configuracao por repositorio

Os defaults e a entrada do registro global sao mesclados com `.orchestrator.yml` no repositorio
alvo; o arquivo local tem maior precedencia. Exemplo:

```yaml
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

## Arquitetura

Veja [docs/architecture.md](docs/architecture.md). Em resumo, models nao conhecem HTTP,
subprocessos ou SQLAlchemy; services coordenam regras; clients encapsulam integracoes externas;
e repositories sao a unica porta de persistencia.
