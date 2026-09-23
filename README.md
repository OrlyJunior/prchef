# Autonomous Development Orchestrator — V1

CLI em Python que executa e persiste o fluxo:

```text
Trello Card   ─┐
               ├─> planner -> implementer -> validacoes locais
GitHub Issue  ─┘   -> Git branch/commit/push -> GitHub PR -> reviewer
```

A V1 nunca faz merge, nunca envia commits diretamente a branches protegidas e encerra em
`NEEDS_HUMAN` quando a spec informa que uma ambiguidade impede uma implementacao segura. O fluxo é sempre: task -> spec -> branch -> Codex -> validação
local -> commit -> push -> PR -> review. O merge continua inteiramente manual.

## Requisitos

- Python 3.11+
- Git
- Codex CLI autenticado
- Opcionalmente, Claude Code CLI autenticado para o provider `claude-code`
- GitHub CLI (`gh`) autenticado, usado para GitHub Issues e Pull Requests
- Uma credencial para o provider API escolhido, por exemplo `OPENAI_API_KEY` para o exemplo
  LiteLLM incluído
- credenciais Trello quando esse provider for utilizado
- Para o provider `gemini-vertex`: projeto Google Cloud com Vertex AI habilitado e credenciais
  Application Default Credentials (ADC), por exemplo `gcloud auth application-default login`.
  Ele usa `google-genai` em modo Vertex AI; `GEMINI_API_KEY` nao e usado nem deve ser configurado.

O adaptador Codex usa o modo oficial nao interativo `codex exec`, com workspace limitado ao
repositorio. LiteLLM é o backend API-modelo de primeira classe para provedores cloud e endpoints
locais compatíveis; ele não recebe implicitamente shell, filesystem ou estado de workflow.
O adaptador `gemini-vertex` faz uma unica chamada request/response ao Vertex AI usando ADC e nao
recebe acesso ao filesystem, shell ou ferramentas locais.

O provider opcional `claude-code` usa somente `claude -p --output-format json` no workspace que o
orquestrador forneceu. Cada execucao tambem usa `--restricted` e um conjunto fixo de ferramentas
de arquivo (`Read,Edit,Write,Glob,Grep`): isso ignora configuracoes de usuario/projeto, confina as
ferramentas de arquivo ao workspace e nao disponibiliza shell/Bash, MCP ou execucao cloud. Para
respostas estruturadas, ele encaminha o schema existente por
`--json-schema`, consome o envelope JSON (incluindo `result`, `structured_output` e metadados de
uso) e valida o objeto antes de ele chegar ao planner ou reviewer. Instale uma versao recente da
CLI v2.1.259 ou posterior, que suporte `--json-schema` e `--permission-prompts`, e autentique fora
do YAML. O modo
`bare_mode: true` e o default recomendado para chamadas reproduziveis; ele reforca o isolamento
desabilitando auto-descoberta de hooks, plugins, skills e MCP. Mesmo se configurado como `false`,
`--restricted` continua a proteger a fronteira de workspace. No modo bare, Claude Code requer
`ANTHROPIC_API_KEY` (ou credenciais suportadas de cloud) e nao usa login de assinatura ou
keychain. `permission_mode: dontAsk` e apropriado para papeis de leitura; apenas configure
`acceptEdits` para um papel que possa editar o worktree criado pelo orquestrador. Um unico
provider Claude pode servir todos os papeis: use presets ou a configuracao temporaria da TUI para
atribuir `dontAsk`, `acceptEdits` ou `auto` por papel. O valor no YAML do provider continua sendo
o default quando o papel nao define uma substituicao.

Para controlar o pensamento estendido de Claude, configure opcionalmente `effort` no provider com
um de `low`, `medium`, `high`, `xhigh` ou `max`. O orquestrador envia `--effort <valor>` apenas
quando o campo esta presente; se omitido, o default da CLI Claude permanece autoritativo. A TUI e
os presets usam a chave neutra `reasoning_effort` para essa mesma opcao por papel, sem alterar o
YAML ou outros papeis que compartilhem o provider.

A configuracao Claude aceita apenas `executable`, modelo, esforco, limites de turno/orcamento, modo de
permissao e bare mode. Nao ha campo para argumentos arbitrarios, `--worktree`, `--add-dir`,
`--resume`, `--continue`, execucao remota ou bypass de permissao. Claude nunca cria branch,
commit, push, PR, merge ou revisao: o provider nao expoe shell, e esses passos continuam pertencendo
ao orquestrador. O session
ID e guardado somente como metadado diagnostico apos a conclusao; nao existe resume automatico.

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

Jules permanece somente como integracao legada/depreciada para configuracoes existentes. Seu ciclo
de sessões remotas não oferece execução interativa no worktree, portanto não acompanha as
expectativas atuais do workflow. Ao carregar ou instanciar um provider Jules, o orquestrador emite
um aviso de deprecação; prefira LiteLLM para chamadas de modelo e Codex CLI para implementação local.

## Instalacao

Instale a CLI isoladamente com [pipx](https://pipx.pypa.io/). Isso cria um ambiente virtual
gerenciado pelo pipx, mas disponibiliza o comando `orchestrator` para o seu usuario, sem ativar
uma `.venv` do projeto:

```bash
pipx install autonomous-dev-orchestrator
pipx ensurepath
```

Quando a distribuicao for fornecida como wheel ou diretorio-fonte, substitua o nome do pacote pelo
caminho recebido, por exemplo `pipx install C:\Downloads\autonomous_dev_orchestrator-0.1.0-py3-none-any.whl`.

Feche e abra o terminal depois de `pipx ensurepath`, entao valide fora de qualquer checkout:

```bash
orchestrator --help
orchestrator tui
```

O arquivo de configuracao global e descoberto em `~/.config/orchestrator/orchestrator.yml` ou,
como alternativa compativel, em `~/.orchestrator/orchestrator.yml` (no Windows,
`$HOME\.orchestrator\orchestrator.yml`). Salve a configuracao e os caminhos dos repositorios em
uma dessas localizacoes; o `orchestrator.example.yml` e um ponto de partida. Caminhos relativos de
repositorios, banco e Qdrant no YAML sao ancorados ao diretorio desse arquivo global, nunca ao
diretorio em que o comando foi iniciado. Para selecionar outro arquivo global, defina
`ORCHESTRATOR_CONFIG_PATH` como um caminho absoluto ou use `--config` explicitamente.

Credenciais devem ser variaveis de ambiente. Como conveniencia, o aplicativo tambem le
`~/.orchestrator/.env`; ele nao le `.env` do checkout ou do diretorio atual. Voce pode copiar
`.env.example` para essa localizacao e preencher apenas as credenciais necessarias.

### Desenvolvimento com instalacao editavel

Contribuidores podem manter o mesmo comando global apontando para o clone atual:

```powershell
pipx install --editable C:\Git\agents-orchestrator
pipx ensurepath
```

Em macOS/Linux, use o caminho do clone, por exemplo
`pipx install --editable ~/src/agents-orchestrator`. Alteracoes em `src/` passam a ser usadas
imediatamente. Se mudar dependencias ou metadados de empacotamento, atualize o ambiente pipx:

```bash
pipx reinstall autonomous-dev-orchestrator
```

### Atualizacao, desinstalacao e PATH

Para uma instalacao publicada, execute `pipx upgrade autonomous-dev-orchestrator`. Para uma
instalacao a partir de clone ou wheel, repita `pipx install` com a origem desejada ou use
`pipx reinstall autonomous-dev-orchestrator`.

O pipx informa ao final da instalacao quando o diretorio de binarios dele nao esta no `PATH`.
Trate esse aviso como bloqueante: rode `pipx ensurepath`, abra um novo terminal e confira
`Get-Command orchestrator` no PowerShell ou `command -v orchestrator` em shells POSIX. No Windows,
o diretorio normalmente e `%USERPROFILE%\.local\bin`; nao adicione
`.venv\Scripts` do checkout ao `PATH`. O orquestrador nao altera PATH, perfis de shell ou
configuracoes de sistema.

Remova a CLI e seu ambiente isolado com:

```bash
pipx uninstall autonomous-dev-orchestrator
```

Para executar testes e ferramentas de desenvolvimento no clone, instale o extra de desenvolvimento com
`python -m pip install -e ".[dev]"`. LiteLLM já faz parte das dependências principais; não há
extra `litellm` para instalar.

Edite o `orchestrator.yml` global. Para Trello, o card deve possuir um custom field chamado
`Repository` (ou `Repo`) cujo valor corresponda a uma chave em `repositories`; `--repository`
continua podendo sobrescrevê-lo. Para GitHub Issues, informe `--repository owner/repo` e use essa
mesma chave no registro `repositories` para associá-la ao checkout local. Antes da primeira
execucao, conecte o repositorio ao Jules. Sem `providers.<nome-jules>.source`, o cliente usa o
remote `origin` para localizar a source correspondente na API.

## Contexto duravel do projeto

Cada repositorio pode versionar `docs/PROJECT_CONTEXT.md` como contexto duravel do projeto. O
orquestrador fornece esse arquivo aos agentes de planejamento, implementacao e revisao como
contexto complementar; a tarefa atual e o estado do repositorio continuam sendo autoritativos.
O conteudo enviado a cada prompt e limitado a 12.000 caracteres. `PLANNER.md`,
`IMPLEMENTER.md` e `REVIEWER.md` sao instrucoes locais e opcionais especificas de cada papel,
lidas da raiz do repositorio configurado em cada execucao. O planner usa `PLANNER.md` durante
planejamento normal, retomado e debug; sem ele, as regras padrao de seguranca e JSON continuam
valendo. A tela Agent Instructions permite editar e salvar cada arquivo separadamente, sem stage
ou commit; os arquivos sao ignorados pelo Git por padrao.

A recuperacao de contexto do checkout e uma politica propria do orquestrador, separada dos agent
providers. Configure-a em `defaults.repository_context.strategy` ou sobrescreva-a em
`repositories.<chave>.repository_context.strategy`. `heuristic` continua sendo o default: preserva
a arvore local limitada e os excertos ranqueados do planner; o reviewer continua recebendo apenas
metadados de repositorio e branches nesse bloco. Nomes desconhecidos falham com a lista de
estrategias suportadas.

`qdrant` habilita recuperacao semantica densa com o cliente Python oficial e embeddings locais
FastEmbed. Ela so pode ser usada por uma entrada registrada em `repositories`, cria uma collection
deterministica por chave/caminho configurado e separa o checkout de controle de cada worktree no
payload. O orquestrador descobre, filtra, calcula hashes e divide os arquivos; arquivos inalterados
nao sao re-embeddados, enquanto chunks alterados/removidos sao sincronizados sob demanda. Arquivos
ignorados pelo Git, binarios, symlinks, artefatos gerados conhecidos, `PLANNER.md`,
`IMPLEMENTER.md`, `REVIEWER.md` e `docs/PROJECT_CONTEXT.md` nao entram no indice.

Exemplo local persistente (o caminho deve ficar fora de todo checkout):

```yaml
repository_context:
  strategy: qdrant
  qdrant:
    mode: local
    path: ~/.orchestrator/qdrant
  embedding:
    provider: fastembed
    model: BAAI/bge-small-en-v1.5
    version: "1"
    dimension: 384
```

O primeiro uso pode baixar o modelo e ter custo de inicializacao. O modo local persistente e para
indices pequenos e um unico processo, pois o storage possui lock exclusivo. Para processos
concorrentes, execute um servidor Qdrant e configure `mode: server` com `url`, ou com `host` e
`port`. A mesma estrategia usa a mesma abstracao de cliente nos dois modos. Mudancas de provider,
modelo, versao, dimensao, distancia ou schema recriam a collection e reindexam. Esta iteracao usa
somente o vetor nomeado `dense`; vetores esparsos e fusao hibrida ficam para uma extensao futura.
Falhas de scanning, embedding ou Qdrant produzem um diagnostico limitado ao repositorio solicitado
e nunca alteram arquivos do checkout.

## Banco e migrations

No startup, os comandos da CLI aplicam automaticamente todas as migrations pendentes antes de
acessar o banco. Se uma migration falhar, o comando aborta com erro claro. O Alembic le
`ORCHESTRATOR_DATABASE_URL` do ambiente ou de `~/.orchestrator/.env`.

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
orchestrator providers
orchestrator tui
orchestrator run --trello-card CARD_ID
orchestrator run --github-issue 42 --repository owner/repository
```

Use `orchestrator --version` para exibir a versao atual do pacote.

### Interface interativa

`orchestrator tui` abre uma interface Textual para selecionar repositorios configurados, provider
de task, issues GitHub (ou um ID de card Trello), providers dos papeis planner/implementer/reviewer,
e iniciar o mesmo workflow usado por `run`. Ela mostra as transicoes persistidas ao vivo e permite
filtrar execucoes anteriores, inspecionar snapshots/steps e abrir links de task, PR e sessoes Jules.
As selecoes de providers valem apenas para a execucao iniciada e nunca alteram o YAML. Os comandos
CLI nao interativos continuam disponiveis para automacao.

Para listar as instancias de agent provider configuradas nos defaults globais, use:

```text
$ orchestrator providers
cloud-api  litellm
codex  codex-cli
```

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

## Lifecycle legado do Jules (depreciado)

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

Após cada resultado estruturado, a integração GitHub publica uma review formal no mesmo PR:
`APPROVED` envia `APPROVE`, `CHANGES_REQUESTED` envia `REQUEST_CHANGES` com os achados bloqueantes,
e `NEEDS_HUMAN` deixa um comentário neutro. Sugestões não bloqueantes acompanham uma aprovação sem
alterar a decisão. Falhas ao publicar são registradas, mas não mudam a decisão do reviewer nem o
status final do workflow. O orquestrador nunca faz auto-merge.

O PR continua sendo criado pela identidade autenticada no `gh`. Para reviews formais confiáveis,
configure um GitHub App instalado no repositório: [guia de configuração](docs/github_app.md).
O App gera tokens de instalação curtos somente para a chamada de publicação; sem essa configuração,
o `gh` usa sua autenticação ambiente. GitHub pode impedir uma identidade de aprovar ou solicitar
mudanças no próprio PR, então use uma identidade de App separada quando necessário.

## Configuracao por repositorio

O `orchestrator.yml` global e a unica fonte persistente de configuracao. Ele e descoberto em
`~/.config/orchestrator/orchestrator.yml` ou `~/.orchestrator/orchestrator.yml`, sem procurar no
checkout nem no diretorio atual. Os defaults sao mesclados com a
entrada correspondente em `repositories.<owner/repository>`; `.orchestrator.yml` no repositorio e
ignorado com um aviso. Exemplo:

```yaml
task:
  provider: github  # usado apenas com --task-id; opções explícitas têm precedência

defaults:
  agents:
    planner: {provider: cloud-api}
    implementer: {provider: codex}
    reviewer: {provider: cloud-api}
  providers:
    cloud-api:
      type: litellm
      model: openai/gpt-4.1-mini
      api_key_env: OPENAI_API_KEY
      max_concurrency: 3
    codex:
      type: codex-cli
      command: [codex, exec, --sandbox, workspace-write, --output-last-message, "{output_file}", -]
      max_concurrency: 2
  safety:
    allow_direct_merge: false
    require_tests: true
repositories:
  owner/repository:
    path: C:/Git/repository
    repository:
      base_branch: develop
      protected_branches: [main, master, develop, release]
    validation:
      auto_detect: true
      commands:
        - [python, -m, pytest]
```

Task providers suportados:

- `trello`: usa `ORCHESTRATOR_TRELLO_API_KEY` e `ORCHESTRATOR_TRELLO_TOKEN`.
- `github`: executa `gh issue view` com a autenticação já ativa no GitHub CLI.

Agent providers sao instancias nomeadas em `providers`, dos tipos `litellm`, `codex-cli`,
`gemini-vertex` e `jules`. `agents.planner`, `agents.implementer` e `agents.reviewer` associam responsabilidades
do workflow a essas instancias. Mais de um papel pode reutilizar a mesma instancia, ou cada papel
pode apontar para uma configuracao diferente do mesmo tipo. Opcoes de runtime/modelo permanecem
dentro da instancia do provider.

| Backend | Status | Uso |
| --- | --- | --- |
| `litellm` | primeira classe | APIs de modelos cloud, locais e self-hosted compatíveis; instalado normalmente. |
| `codex-cli` | primeira classe | agente especializado de implementação no worktree local. |
| `gemini-vertex` | opcional | integração especializada com Vertex AI/ADC. |
| `jules` | legado/depreciado | apenas compatibilidade; a API de sessões remotas não oferece o fluxo interativo de worktree atual. |
| outros tipos | não suportados | rejeitados com erro de configuração. |

No `codex-cli`, `model` e `reasoning_effort` sao opcionais somente para compatibilidade com
configuracoes legadas. Quando ausentes, a execucao usa o default externo da Codex CLI e o log
verbose mostra `<external-default>`. Prefira fixa-los para controle de reproducibilidade, quota e
custo. Em comandos customizados, o orquestrador injeta cada opcao apenas se a flag correspondente
nao existir; uma flag existente com valor diferente e rejeitada antes do subprocesso.

No `claude-code`, `model` e `effort` tambem sao opcionais. `effort` aceita apenas `low`, `medium`,
`high`, `xhigh` ou `max` e e enviado como `--effort` somente quando configurado. A TUI e os presets
o apresentam como `reasoning_effort`, uma substituicao em memoria por papel que e validada pela
mesma schema Claude e nunca persiste sobre o default YAML.

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

`litellm` é o backend API genérico de primeira classe, incluído em uma instalação normal de
`autonomous-dev-orchestrator`, para identificadores de modelo livres, incluindo
provedores cloud e endpoints locais/self-hosted compativeis, como Ollama, vLLM e servidores
OpenAI-compativeis. Configure `api_key_env` somente com o nome da variavel de ambiente; omita-o
para endpoints sem autenticacao. `temperature`, `max_output_tokens` e `generation` ficam isolados
na configuracao do provider. Como uma API de modelo simples nao fornece automaticamente shell ou
edicao de arquivos, um provider LiteLLM pode planejar ou revisar, mas nao substitui as ferramentas
de coding agent do `codex-cli` para implementacao local.

Configuracoes legadas com `roles.spec|implementation|review` e
`integrations.jules|codex` continuam aceitas sem reescrita automatica e sao normalizadas para os
providers `jules` e `codex`. Como Jules está depreciado, esse caminho emite um aviso claro na carga
da configuracao e na instanciacao. Nomes de provider ou tipos desconhecidos produzem um erro de
configuracao com os nomes disponiveis/suportados.

`allow_direct_merge: true` e rejeitado nesta V1. Credenciais ficam exclusivamente em variaveis
de ambiente e `.env` nao e versionado.

### Execucoes concorrentes e limites por provider

Varias execucoes podem correr ao mesmo tempo no mesmo processo (`run` ou `tui`), mas a
concorrencia e sempre limitada. O limite e declarado por instancia nomeada de provider, porque e
o provider que representa o recurso realmente compartilhado: uma unica cota de API, ou a VRAM de
uma unica GPU no caso de modelos locais. Um teto global sozinho nao protegeria nenhum dos dois.

```yaml
defaults:
  providers:
    codex:
      type: codex-cli
      command: [codex, exec, --sandbox, workspace-write, --output-last-message, "{output_file}", -]
      max_concurrency: 2   # duas chamadas Codex simultaneas
    local-ollama:
      type: litellm
      model: ollama/llama3.2
      api_base: http://localhost:11434
      max_concurrency: 1   # uma GPU, uma chamada por vez
```

- `max_concurrency` e um inteiro positivo e vale **1** quando omitido: o default e
  deliberadamente conservador, ou seja, execucao sequencial por provider.
- O teto e por provider, nao global. Providers com nomes diferentes sao admitidos de forma
  independente; `codex` ocupado nao bloqueia `local-ollama`.
- Declare `max_concurrency` apenas em `defaults.providers.<nome>`. Uma entrada em
  `repositories.<chave>.providers.<nome>` que defina `max_concurrency` e rejeitada com um erro de
  configuracao acionavel, mesmo quando repete o default. Um repositorio que precise de capacidade
  propria esta usando outra conexao e merece um nome de provider proprio. Os demais overrides de
  provider por repositorio continuam funcionando normalmente.
- Os tetos sao lidos uma vez na inicializacao e valem para todo o processo. Alterar
  `max_concurrency` exige reiniciar `orchestrator run`/`tui`.
- Overrides de runtime da TUI e presets por papel criam variantes em memoria, mas continuam
  contabilizados no provider configurado de origem: trocar o modelo nao cria uma segunda cota.
  Um fallback libera o slot do provider primario antes de disputar o slot do seu proprio provider.

Uma chamada de agente que nao consegue um slot imediatamente entra numa fila FIFO. A execucao
passa a `WAITING` (distinto de `PENDING`, que e trabalho ainda nao iniciado) e volta a `RUNNING`
quando e admitida. O slot e mantido apenas durante a chamada ao agente, nunca durante Git,
validacao ou operacoes GitHub. Execucoes em `PENDING`, `WAITING` e `RUNNING` continuam
cancelaveis pela TUI e terminam como `CANCELLED` sem iniciar outra etapa.

Trabalho enfileirado vive apenas na memoria do processo. As linhas persistidas permanecem no banco
apos um encerramento, mas **nao** sao retomadas automaticamente no proximo start; a recuperacao
automatica e trabalho futuro (#46).

## Validacoes

Com `validation.commands` vazio e `auto_detect: true`, a V1 reconhece configuracoes comuns de
Python (`pytest`, Ruff, mypy), Node (`npm test`, lint, typecheck), Maven e Gradle. Se
`require_tests` estiver ativo e nenhum comando for encontrado, o fluxo encerra em falha antes
de qualquer commit.

Quando uma validacao falha, `max_validation_retries` (default `2`) controla quantas vezes o
implementador pode corrigir a mesma implementacao antes da falha final. Cada tentativa reutiliza
a mesma execucao, branch e worktree; o planejamento nao e executado novamente. Ao esgotar o
limite, o worktree e preservado para inspecao.

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
