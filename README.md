# Autonomous Development Orchestrator — V1

CLI em Python que executa e persiste o fluxo:

```text
Trello Card   ─┐
               ├─> planner -> implementer -> validacoes locais
GitHub Issue  ─┘   -> Git branch/commit/push -> GitHub PR -> reviewer
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
- Para o provider `gemini-vertex`: projeto Google Cloud com Vertex AI habilitado e credenciais
  Application Default Credentials (ADC), por exemplo `gcloud auth application-default login`.
  Ele usa `google-genai` em modo Vertex AI; `GEMINI_API_KEY` nao e usado nem deve ser configurado.

O adaptador Codex usa o modo oficial nao interativo `codex exec`, com workspace limitado ao
repositorio. Jules cria uma sessao remota oficial, persiste ID e URL, acompanha a execucao por
polling e recupera a ultima mensagem do agente quando a sessao chega a `COMPLETED`.
O adaptador `gemini-vertex` faz uma unica chamada request/response ao Vertex AI usando ADC e nao
recebe acesso ao filesystem, shell ou ferramentas locais.

Por padrao, o Codex roda com `--sandbox workspace-write`. Para optar explicitamente pela revisao
automatica de pedidos de aprovacao da CLI, configure `approval_policy: approve-for-me` em
`providers.<nome-codex>.approval_policy`. Nesse modo o orquestrador invoca
`codex exec --approve-for-me` sem
`--sandbox`: a CLI associa esse modo a `workspace-write` e rejeita a combinacao dos dois flags.
Isso aumenta a autonomia para escalacoes suportadas, mas nao equivale a `danger-full-access`.
O orquestrador nunca troca de politica automaticamente apos uma falha.
Consulte a [documentacao oficial de aprovacoes do Codex](https://learn.chatgpt.com/docs/agent-approvals-security)
para os limites e criterios da revisao automatica.

Para execucoes reproduziveis, configure `model` e, quando desejado, `reasoning_effort` dentro da
instancia `codex-cli`. O orquestrador envia o modelo por `--model` e o esforco por
`--config model_reasoning_effort="..."`; ambos substituem somente aquela execucao, sem depender
dos defaults da CLI ou do app. Os valores precisam ser suportados pela CLI e pela conta usada; nao
ha fallback automatico se um deles for rejeitado.

### Diagnostico do sandbox Codex no Windows

O orquestrador preserva `--sandbox workspace-write`; ele nunca seleciona automaticamente acesso
total nem altera ACLs do repositorio. Se a CLI do Codex informar uma negacao de escrita do sandbox,
o workflow falha com o caminho do repositorio e o modo de sandbox em vez de informar apenas que o
Codex nao alterou arquivos.

Para isolar o problema, execute no diretorio do repositorio e solicite a criacao e remocao de um
arquivo descartavel no diretorio afetado:

```powershell
@'
Crie e remova apenas o arquivo descartavel codex-sandbox-probe.txt neste diretorio.
Nao altere nenhum outro arquivo.
'@ | codex exec --sandbox workspace-write -
```

No Windows, a configuracao oficial tambem oferece os modos sandboxed `elevated` e `unelevated`.
Qualquer mudanca entre eles deve ser uma decisao explicita do usuario ou administrador; o
orquestrador nao faz esse fallback automaticamente. Consulte a documentacao do sandbox Windows do
[Codex](https://learn.chatgpt.com/docs/windows/windows-sandbox).

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
execucao, conecte o repositorio ao Jules. Sem `providers.<nome-jules>.source`, o cliente usa o
remote `origin` para localizar a source correspondente na API.

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
orchestrator --version
orchestrator run --trello-card CARD_ID
orchestrator run --github-issue 42 --repository owner/repository
```

Use `orchestrator --version` para exibir a versao atual do pacote.

Overrides uteis:

```bash
orchestrator run --trello-card CARD_ID --repository example-api
orchestrator run --trello-card CARD_ID --config ./orchestrator.yml
orchestrator run --task-id 42 --repository owner/repository  # usa task.provider
orchestrator show EXECUTION_ID
```

### Debug isolado por etapa

O comando `debug` executa somente uma etapa em isolamento. Ele nao chama `run`, nao avanca o
workflow e nao cria nem altera uma execucao persistida. Use `--from-execution` (ou o alias legado
`--execution-id`) somente quando quiser reutilizar dados historicos como defaults:

```bash
orchestrator debug --stage task-loading --github-issue 2 --repository owner/repository
orchestrator debug --stage validation --repository owner/repository --dry-run
orchestrator debug --stage implementation --repository owner/repository --prompt "Add --version"
orchestrator debug --stage review --from-execution EXECUTION_ID --verbose
```

As etapas aceitas e seus inputs sao (a entrada explicita sempre tem precedencia sobre historico e
defaults de configuracao):

- `task-loading`: `--trello-card` ou `--github-issue`/`--task-id`, mais `--repository` quando
  necessario pelo provider. Sem esses argumentos, use `--from-execution`.
- `spec-generation`: os mesmos inputs de task-loading para obter a task normalizada, mais
  `--repository`/configuracao e autenticacao Jules. Cria uma sessao remota read-only sem persistir
  o resultado na execucao original.
- `implementation`: `--repository` e `--prompt`/`--prompt-file`, ou task via provider e spec via
  `--spec-file`/`--from-execution`. Usa o workspace configurado/atual;
  uma execucao real pode modificar arquivos locais. Nao depende da branch historica, nao troca/cria
  branches e nunca faz commit, push ou PR. Use `--dry-run` para nao chamar o Codex.
- `validation`: `--repository` e a configuracao normal de validacao. Os comandos locais rodam apenas
  sem `--dry-run` e nenhuma persistencia de workflow e alterada.
- `review`: task via provider ou historico, `--spec-file`, `--validation-file`,
  `--pull-request-file`, `--feature-branch` e opcionalmente `--diff-file`; `--from-execution` pode
  preencher esses dados. A operacao Jules permanece read-only e nao publica alteracoes.

`--dry-run` valida os inputs sem chamar providers, Jules, Codex ou comandos locais. O resultado do
debug e JSON em `stdout`, e o progresso continua em `stderr`. Nenhuma etapa seguinte e iniciada
automaticamente. `debug` e diagnostico isolado; uma futura funcionalidade `resume` sera responsavel
por continuar o workflow original usando seu estado Git persistido e nao faz parte deste comando.

No comando `run`, exatamente uma origem deve ser informada por execução. GitHub Issues é a opção recomendada para
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
de polling e o timeout do Jules continuam configuraveis no provider Jules (15 s e 1800 s
por padrao); `--verbose` nao altera o comportamento do workflow.

## Lifecycle do Jules

Para cada spec ou review, o client cria uma unica sessao, persiste imediatamente `session_id` e
URL e consulta seu estado ate uma decisão. Os estados de processamento (`QUEUED`, `PLANNING` e
`IN_PROGRESS`) continuam em polling; `COMPLETED` recupera a ultima `agentMessage` das activities.
Estados que aguardam interação humana e os estados de falha encerram a operação sem criar outra
sessão.

O intervalo, timeout global e retentativas de consulta ficam no provider Jules. O default é
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
agents:
  planner:
    provider: jules
  implementer:
    provider: codex
  reviewer:
    provider: jules
providers:
  jules:
    type: jules
    poll_interval_seconds: 15
    timeout_seconds: 1800
  codex:
    type: codex-cli
    # Exemplos: substitua por opcoes suportadas pela sua CLI/conta.
    model: <your-supported-codex-model>
    reasoning_effort: <your-supported-reasoning-effort>
    command: [codex, exec, --sandbox, workspace-write, --output-last-message, "{output_file}", -]
    timeout_seconds: 3600
  vertex-gemini:
    type: gemini-vertex
    project: my-gcp-project
    location: global
    model: gemini-2.5-flash
    timeout_seconds: 900
    generation:
      temperature: 0.2
      # max_output_tokens: 8192
safety:
  allow_direct_merge: false
  require_tests: true
validation:
  auto_detect: true
  commands:
    - [python, -m, pytest]
```

Task providers suportados:

- `trello`: usa `ORCHESTRATOR_TRELLO_API_KEY` e `ORCHESTRATOR_TRELLO_TOKEN`.
- `github`: executa `gh issue view` com a autenticação já ativa no GitHub CLI.

Agent providers sao instancias nomeadas em `providers`, dos tipos `jules`, `codex-cli` e
`gemini-vertex`. `agents.planner`, `agents.implementer` e `agents.reviewer` associam responsabilidades
do workflow a essas instancias. Mais de um papel pode reutilizar a mesma instancia, ou cada papel
pode apontar para uma configuracao diferente do mesmo tipo. Opcoes de runtime/modelo permanecem
dentro da instancia do provider.

No `codex-cli`, `model` e `reasoning_effort` sao opcionais somente para compatibilidade com
configuracoes legadas. Quando ausentes, a execucao usa o default externo da Codex CLI e o log
verbose mostra `<external-default>`. Prefira fixa-los para controle de reproducibilidade, quota e
custo. Em comandos customizados, o orquestrador injeta cada opcao apenas se a flag correspondente
nao existir; uma flag existente com valor diferente e rejeitada antes do subprocesso.

`gemini-vertex` exige `project` e `model`; `location` tem default `global`. A autenticacao e sempre
via ADC, nunca por API key. Quando uma etapa solicita JSON estruturado, o adapter usa
`response_mime_type: application/json` e o JSON Schema neutro de `StructuredOutputExpectation` na
configuracao nativa do SDK. Para, por exemplo, usar Gemini tanto no planejamento quanto no review:

```yaml
agents:
  planner:
    provider: vertex-gemini
  implementer:
    provider: codex
  reviewer:
    provider: vertex-gemini
```

Configuracoes legadas com `roles.spec|implementation|review` e
`integrations.jules|codex` continuam aceitas e sao normalizadas para os providers `jules` e
`codex`. Nomes de provider ou tipos desconhecidos produzem um erro de configuracao com os nomes
disponiveis/suportados.

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
e repositories sao a unica porta de persistencia. Os services de planner, implementacao e review
dependem apenas de `AgentProvider.execute(AgentRequest) -> AgentResult`; adaptadores acima de
`JulesClient` e `CodexClient` encapsulam REST/polling e subprocessos, respectivamente.
