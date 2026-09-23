# Durable agent context

## Purpose and boundaries

- Python 3.11+ CLI and Textual terminal UI that turn one Trello card or GitHub issue into a
  guarded change: plan → isolated implementation → validation → commit/push → GitHub PR →
  review publication.
- It coordinates providers, Git, GitHub, and persistence. It is **not** a merge bot or a generic
  repository browser/RAG service. Human-blocked executions are resumable using their persisted
  workflow state; this is not a provider-session resume system.
- `docs/PROJECT_CONTEXT.md` is shared supporting context for planning, implementation, and review prompts
  (capped at 12,000 characters). `PLANNER.md`, `IMPLEMENTER.md`, and `REVIEWER.md` are optional,
  capped, role-specific local guidance loaded from the configured repository root at execution
  time: planners receive only `PLANNER.md`, implementers receive only `IMPLEMENTER.md`, and
  reviewers receive only `REVIEWER.md`. All are supplementary; the current
  task, repository state, and core orchestrator invariants win. The role files are Git-ignored by
  default, can be versioned deliberately, and are editable from the TUI without staging or commit.
  Symlinked and non-regular role files are ignored when loading; the TUI refuses to save
  through a symlink. Role-file loading uses bounded memory before prompt rendering.

## Ownership and layering

- `models`: infrastructure-free domain data. `services`: workflow policy, prompts, parsing,
  validation, and debug orchestration. `providers`: role-neutral agent adapter boundary.
- `clients`: transport/subprocess/Git details. `repositories`: the only SQLAlchemy persistence
  boundary. `cli`: dependency composition and stdout/stderr contract. `tui`: a Textual
  presentation/controller layer over configuration, task providers, the workflow service, and
  persisted execution reads; it must not implement workflow policy or a second state machine.
- The TUI presents workflow observer events as a concise user-facing stage timeline by default.
  Sanitized persisted diagnostics are available through its detailed-log toggle and a full-log
  clipboard export action; they record effective agent requests/responses, validation command
  results, errors, and workflow events. Clipboard exports preserve readable section metadata and
  apply the same redaction boundary as persisted diagnostics.
  Expanded diagnostics grow in the screen's main vertical scroll area rather than using a nested
  vertical log viewport, so long entries remain readable in live runs and execution history.
  This does not alter the CLI's concise progress view or workflow observer semantics. `--verbose`
  emits that same structured diagnostic stream to stderr.
- TUI screens use `OrchestratorSelect` for Select controls in vertical scroll regions. It reserves
  visible overlay space after mouse or keyboard activation and restores focus without triggering
  parent-scroll jumps. Repository controls use its `RepositorySelect` specialization: configured
  repository keys are both their visible labels and Select values, with the selected key retained
  by `WorkflowController` across screen navigation.
- The dedicated TUI Repositories screen creates, edits, renames, and removes entries only under
  `repositories` in the resolved global `orchestrator.yml`. It retains unrelated YAML content and
  repository ordering, stores validation commands as argument lists, and reports missing paths
  without preventing deliberate configuration. Removing an entry never removes its directory,
  changes Git state, or creates repository-local configuration.
- That same dedicated Repositories screen is the index-management surface: it shows each selected
  repository's context strategy and independent semantic-index lifecycle (`not indexed`,
  `indexing`, `ready`, `stale`, or `failed`), last successful commit/timestamp and counts, and
  manual build/rebuild, changed-file refresh, and clear actions. Its operations run in TUI workers;
  the workflow launcher deliberately has no index controls.
- The TUI launcher may select configured provider instances and role-local runtime settings
  (model, reasoning, Claude permission mode, and exposed provider generation options) for one run. These overrides are
  schema-validated in memory, never alter YAML defaults, and may give roles sharing a configured
  provider independent runtime settings. Validated launch selections are recorded with the execution's
  diagnostics so a human-blocked execution reapplies them when it resumes. Its default launcher view presents compact per-role
  provider/model/runtime summaries; role configuration and generation controls are progressive,
  and generation fields identify Provider/YAML defaults versus temporary overrides. GitHub App
  identities remain separately persisted, inspectable configuration.
- With `--verbose`, TUI startup logs each role's configured provider and backend type before the
  workflow begins; a synthetic `__tui_runtime_*` name indicates only an in-memory override and
  never changes the selected backend. Provider type is always taken from the configured provider,
  not inferred from its model name.
- The TUI launcher automatically browses tasks for its selected repository and task provider.
  Rows show titles, labels, and checkboxes; users may launch one task through the unchanged
  single-workflow path or select an ordered batch whose shared role presets/runtime overrides are
  applied to every item. Tasks with any existing execution in the same repository and task source
  show that status and remain unselectable, with inspection/resume delegated to execution history.
  Refresh, loading, empty, and inline-error states remain available, while explicit CLI task
  identifiers remain supported.
- Batch policy is owned by `WorkflowController` and `WorkflowRepository`, not TUI widgets. A batch
  is persisted before its tasks are submitted in list order through `submit_execution`; it adds no
  concurrency limit beyond the shared dispatcher and per-provider ceilings. The dispatcher may skip
  queued jobs behind a closed admission gate while leaving them PENDING, and reevaluates gates when
  notified. NEEDS_HUMAN, FAILED, and CHANGES_REQUESTED items do not delay other submissions. Answers
  and continuations for a live batch rejoin the dispatcher tail. The batch stays open while any item
  is queued, running, waiting for provider capacity, or awaiting human input; it closes when every
  item is APPROVED, CHANGES_REQUESTED, FAILED, CANCELLED, skipped, or never submitted. Stopping a
  batch cancels all active items and closes it without requiring answers to NEEDS_HUMAN items.
  Stored batches remain browsable after restart. `on_batch_created` fires synchronously after the
  durable batch row commits and before the first submission, exposing its stable identity and order.
- Batch progress is a read-side TUI projection (`orchestrator.tui.batch_progress`), not a second
  workflow state machine: it reshapes durable batch items, workflow observer events, and persisted
  execution/step rows into one ordered, per-position view shared by the active `WorkflowRunnerScreen`
  batch launch and the persisted `BatchDetailScreen`. Both render the same `BatchProgressView`/
  `BatchItemRow` widgets, and both reuse the single-task stage/status wording factored into
  `orchestrator.tui.workflow_presentation` so the two views cannot drift. A summary line always
  counts every semantic status (queued, waiting for provider, running, needs human, approved,
  changes requested, failed, cancelled) and is never cleared when the batch finishes; an unlinked
  batch item (no execution yet) renders as queued rather than disappearing. Each row is independently
  expandable in the owning screen's existing main scroll region (no nested scroll viewport), showing
  a concise timeline plus its own show/copy detailed-log and open-execution controls.
- Only a normalized `quota` failure attributed to a named provider makes that provider unavailable
  for the current batch. Queued items whose selected role or fallback presets use it remain PENDING;
  other queued items proceed. The live batch view labels held rows, counts them, and lets the user
  mark the provider available again manually. Availability is batch-scoped and process-local; reset
  times do not reopen it automatically. Persisted or restarted batches show plain QUEUED/WAITING
  with no availability state and do not imply queued work resumes after restart. Submitting every
  item up front can leave multiple PENDING executions after a crash; queued recovery is separate.
  A quota failure during planning happens before a worktree/spec exists; continuing that execution
  repeats planning (with earlier answers and the continuation feedback) and then proceeds exactly
  like a fresh run. `WorkflowService.continuation_stage` is the single eligibility rule every view
  uses; an already-continued quota failure does not make a later ordinary failure continuable.
- The live batch view rebuilds its provider-availability controls only when the set of unavailable
  providers changes (counts update in place, so focus and clicks survive events) and notifies when
  a provider first starts holding tasks, since those controls usually sit below the launch form.
- The TUI's Agent Instructions view edits the selected repository's optional `PLANNER.md`,
  `IMPLEMENTER.md`, and `REVIEWER.md` independently. Missing files remain absent until their own
  save action; the TUI never stages or commits these files.
- Provider-specific commands, polling, credentials, or API shapes stay below
  `AgentProvider.execute(AgentRequest)`. Planner/implementer/reviewer services stay provider
  neutral.
- `AgentProvider` is the orchestrator's provider boundary. Model-routing libraries such as
  LiteLLM are implementation backends behind that boundary and must not own workflow semantics,
  persistence, scheduling, role behavior, or lifecycle state.
- `RepositoryContextStrategy` is the separate, orchestrator-owned retrieval-policy boundary used
  by planner and reviewer prompt composition. The configured strategy is resolved once per
  workflow/debug invocation and reused across retries and resume paths; it is not an agent
  provider. `heuristic` remains the compatibility default: planning keeps the existing bounded
  local tree and ranked excerpts, while review keeps its existing repository/branch metadata block.
  `qdrant` adds dense semantic retrieval below the same boundary through the official client and a
  provider-neutral, lazy local FastEmbed boundary; workflow services never import either backend.
- Qdrant uses one collection per registered repository, derived from both its immutable configured
  key and a canonical-path identifier. Every point operation is additionally scoped by repository,
  configured-path, and checkout/worktree `source_root_id`. The orchestrator owns Git-aware source
  eligibility, full-file hashes, language/symbol/location metadata, bounded overlapping chunks,
  deterministic point IDs, incremental upsert-before-delete, and complete-block prompt budgeting.
  Private role files and the separately supplied `docs/PROJECT_CONTEXT.md` are never indexed.
- Collection metadata retains the latest successful index timestamp and Git HEAD, plus sanitized
  lifecycle failures. Status inspection compares the stored commit with the current HEAD (an
  unavailable Git revision is inconclusive rather than an inspection error); a configured
  `repository_context.fallback_to_heuristic` allows a failed semantic lookup to use the bounded
  heuristic strategy without preventing the workflow.
- Qdrant collection metadata records the named `dense` vector, cosine distance, index schema, and
  embedding provider/model/version/dimension; incompatibility deliberately recreates and fully
  reindexes the collection. Sparse/hybrid retrieval is not implemented, but the named vector and
  payload schema leave room for it without changing workflows. Index state is outside SQLAlchemy
  and outside repositories. A process-owned client manager keyed by canonical connection settings
  owns one lazily created, long-lived client per configured store, shared by workflow, debug, and
  TUI index operations; local-mode operations are serialized through a per-store lock. An operation
  failure never closes that shared client, and each client is closed exactly once at process
  shutdown. Persistent local mode remains single-process; concurrent *processes* still require
  Qdrant server mode. A failed sync is not
  queried, is idempotently repairable, never mutates repository files, and yields a sanitized
  repository-context diagnostic without widening collection/filter scope.
- Database schema changes require versioned Alembic migrations; never add runtime
  `Base.metadata.create_all()`.

## Runtime and persistence contract

- `run` and `tui` apply pending Alembic migrations before opening the workflow repository. `show`,
  and `debug --from-execution`, do the same before database access; a migration failure aborts with
  a clear error. Keep `alembic upgrade head` usable for manual operations.
- Persistent state is global rather than repository- or cwd-scoped. Database resolution prefers
  `ORCHESTRATOR_DATABASE_URL`/`ORCHESTRATOR_DATABASE_PATH`, then global
  `defaults.persistence.database_url`/`database_path`, then an existing `orchestrator.db` beside
  the discovered global configuration, and finally `~/.orchestrator/orchestrator.db`. Relative
  explicit SQLite locations are anchored to the global configuration directory (or the user state
  directory if no configuration is discovered). Runtime and manual Alembic migrations share this
  resolver; repository-local configuration never selects persistence. The packaged `orchestrator`
  console script is intended for an isolated per-user `pipx` installation: discovery considers
  explicit CLI/environment selection and user-global configuration locations, never the cwd,
  source checkout, or its `.venv`; packaged migration scripts support both wheel and editable
  installs without a repository-root path. `pipx ensurepath` is the supported PATH remedy; the
  application never alters PATH or shell profiles.
- Runtime logs are UTC ISO-8601 timestamps on stderr. Final `run` execution JSON and all `debug`
  result JSON stay on stdout. Redact secrets before logging.
- Execution diagnostics are persisted as JSON-safe `execution_diagnostics` rows and must be
  redacted before persistence and before observer delivery. They preserve full non-sensitive
  prompt/output/context data; any future size limit must be explicit to the user. Agent-request
  diagnostics store `effective_instructions` as the one canonical provider prompt; a distinct
  `instructions` field is retained only when it adds prompt-construction context. TUI detailed
  logs and clipboard exports also collapse duplicate legacy prompt fields.
- Every execution enters through one process-scoped submission seam. `submit_execution`,
  `submit_response`, and `submit_continuation` synchronously persist the row, return an
  `ExecutionHandle` exposing the persisted ID plus non-blocking polling and blocking wait/result,
  and queue bounded in-process work. Submissions may supply a cheap admission gate; a closed gate
  leaves its job PENDING while workers admit later eligible jobs in FIFO order. `run`,
  `respond_and_resume`, and `continue_execution` are thin
  wrappers that submit and immediately wait, so CLI/TUI output and exit behavior are unchanged.
  Batch, scheduling, continuous-execution, and label-selection features (#44/#45/#46/#147) must
  submit here rather than adding their own concurrency handling.
- Ordered batch identity is stored separately in `workflow_batches` and
  `workflow_batch_items` (repository, task source, title snapshots, positions, and nullable
  execution links). Batch items never overload execution rows; deleting batch metadata cascades
  only to its items and leaves linked executions intact.
- An execution is created as `PENDING` and becomes `RUNNING` only when a dispatcher worker starts
  it; resumes and continuations return to `PENDING` and compete for the same capacity. Each entered
  stage is a separately persisted step. Repeated validation/correction attempts create repeated
  `VALIDATE`/`IMPLEMENT` steps.
- Concurrency is bounded per *named provider*, because the configured provider instance is the
  shared resource: one API quota pool, or one GPU's VRAM for a local model. Every provider
  definition accepts `defaults.providers.<name>.max_concurrency`, a positive integer defaulting to
  `1`. It is authoritative in exactly one place: a `repositories.<key>.providers.<name>` entry that
  sets `max_concurrency` is rejected with an actionable configuration error, even when it repeats
  the default. Other repository provider overrides are unaffected. Ceilings are fixed at startup;
  changing one requires a restart. There is no global ceiling, resource-pool mapping, backend
  detection, live resizing, or cross-process coordination.
- A permit is held only for `AgentProvider.execute`, never across Git, validation, or GitHub work,
  and is always released. A call that cannot acquire immediately queues FIFO and moves a started
  execution to `WAITING` — distinct from `PENDING`, which is work that has not begun — emitting the
  normal persisted status event naming the provider; admission restores `RUNNING`. Different
  provider names admit independently. TUI runtime overrides and role-local presets are charged to
  the configured provider they were derived from, so a temporary model change never creates a
  second quota pool. Fallback releases the primary permit before acquiring the fallback provider's
  own permit.
- Concurrent executions share a `WorkflowService` instance, so the event listener and active stage
  live in per-execution runtime state rather than on the service. Execution-derived worktree paths,
  branches, and worktree-bound Git/GitHub clients remain distinct per execution.
- Queueing is in-memory and single-process. Persisted `PENDING`/`WAITING` rows survive a crash but
  are deliberately **not** resumed automatically at startup; recovery is deferred to #46.
- Persist: normalized task, resolved repository/base branch and configured repository reference,
  worktree path, branch, latest
  implementation and validation snapshots, commit, PR, review, status/error, and timestamps.
  The implementation/validation snapshots are latest-attempt snapshots, not an attempt history.
- A `NEEDS_HUMAN` transition creates an append-only human-interaction record containing the
  requesting stage, exact blocking prompt, request/response timestamps, and the response. `respond`
  and the TUI resume the same execution ID; planner responses rerun planning before any workspace is
  created, while review responses reuse the persisted worktree, branch, PR, and snapshots.
- Any non-terminal execution — `PENDING`, `WAITING`, or `RUNNING` — can receive a durable
  cancellation request through the workflow service/controller boundary; terminal executions
  refuse it. It ends as `CANCELLED`, never `FAILED`; providers that cannot be
  interrupted immediately finish their current operation, but no later stage is scheduled.
  Queued dispatcher work and provider-queued work are woken or dropped, cancellation is checked
  immediately before and after permit acquisition, any acquired permit is released, and
  cancellation-versus-admission races resolve idempotently to `CANCELLED`.
  Cancellation status and diagnostics remain inspectable after restart.
- Terminal `CHANGES_REQUESTED` executions, and `FAILED` executions that retained both a
  workspace and working branch, can be manually continued from the TUI execution detail view.
  The operator edits the persisted terminal feedback, which is recorded as a diagnostic, then a
  fresh implementer invocation resumes in the retained worktree at `IMPLEMENT`. Continuations use
  normal validation/review correction limits but have no manual retry ceiling. A persisted PR must
  still be open before each resumed push; pushing its retained branch updates the PR, while a
  missing PR is created after push. A closed or unavailable persisted PR blocks continuation and is
  never recreated automatically.
- Provider startup references during spec/review are persisted immediately in legacy-named
  `spec_jules_session_*` / `review_jules_session_*` fields. Preserve those fields for later human
  inspection; no automatic session/workflow resume exists.
- Workflow observer events are emitted only after persisted status/step transitions. They are for
  live terminal presentation and listeners may never change workflow semantics or make a run fail.
- The TUI treats a `WORKFLOW_FINISHED` event as terminal for its active-run controls regardless of
  final status (including `CHANGES_REQUESTED`), reacting to the event itself rather than waiting
  for the blocking workflow call to return; this avoids the runner screen appearing stuck on a
  finished run.
- TUI history, detail, and live progress distinguish queued `PENDING` ("QUEUED"), provider-capacity
  `WAITING` ("WAITING FOR AGENT", naming the provider in live status where known), `RUNNING`, and a
  requested-but-unfinished cancellation. The stop control stays available for all three active
  states. Concurrency is configured in YAML only; no launcher control exposes it.

## Normal `run` behavior

1. CLI accepts exactly one task origin: `--trello-card`, `--github-issue`, or `--task-id`.
   `--task-id` uses configured `task.provider`; GitHub task loading requires `--repository`.
   Trello/GitHub clients normalize input into `DevelopmentTask` before configuration is resolved.
2. Resolve the project configuration and derive the GitHub slug from `origin`. Create configured
   planner/implementer/reviewer instances; a named provider shared by roles is instantiated once.
3. Generate one structured `RepositorySpec` from the base checkout/base branch. Planning receives
   bounded, read-only repository context from the configured strategy (`heuristic` tree/excerpts or
   Qdrant dense chunks), alongside `docs/PROJECT_CONTEXT.md` when present. A blocking spec ambiguity
   ends the execution as `NEEDS_HUMAN` before any branch or worktree is created.
4. Build a unique, non-protected working branch name from configured prefix, task identity/title,
   UTC timestamp, and execution ID. Create a linked worktree from freshly fetched
   `origin/<base_branch>`; do not switch, clean, stash, reset, or otherwise mutate the control
   checkout.
5. Bind mutable Git, GitHub, command, and implementation operations to that worktree. The initial
   implementation agent may edit it, but is instructed never to commit, push, create a PR, or
   merge. A reported successful implementation with no Git changes is a failure.
6. Run validation in the worktree. On success: commit, push, create PR, then run the reviewer
   against the PR data, validation result, and `base...HEAD` diff. Each review loop is published by
   the GitHub integration as `APPROVE`, `REQUEST_CHANGES`, or a neutral comment for `NEEDS_HUMAN`.
   The workflow never auto-merges.

## Worktree and Git invariants

- Worktree root is configurable. The default is a sibling `orchestrator-workspaces` directory;
  configured roots may not resolve inside the control checkout. Existing target paths are errors.
- Worktrees are retained automatically on success, failure, cancellation, and human intervention so they remain
  available for inspection and recovery. `cleanup-workspaces --repository <key-or-path> --dry-run`
  lists their retention decisions; without `--dry-run` it removes successful (`APPROVED` or
  `CHANGES_REQUESTED`) worktrees and prunes Git metadata. `FAILED`/`CANCELLED`/`NEEDS_HUMAN` worktrees require
  `--include-failed` (or `--all`); `PENDING`, `WAITING`, `RUNNING`, unmanaged, and out-of-root paths
  are never removed by this command.
- Commit/push refuse protected branches and refuse if the current checkout is not the workflow
  branch. Commit stages **all** worktree changes and fails if nothing is staged. Push is only
  `git push --set-upstream origin <working-branch>`.
- PR creation occurs only after a successful push. `gh pr create` must yield a URL, then `gh pr
  view` supplies the persisted PR fields. When an implementer App is configured, its resolved bot
  identity is assigned to the created PR; assignment failures are logged but do not fail the
  workflow. A later failure never rolls back a commit, push, PR, or worktree.

## Validation and review correction loops

- Validation starts with configured commands, then appends conservative auto-detected commands.
  Exact duplicates are skipped; any explicit mypy command suppresses auto-detected `mypy .`.
  All selected commands run, so the result contains every check rather than stopping at first
  failure.
- With `require_tests: true`, absence of a recognizable test command is itself a validation
  failure. Empty validation is allowed only when tests are not required.
- `max_validation_retries` defaults to `2` and must be non-negative. A failed validation records
  its result, then—while budget remains—asks the same implementer to correct the **same worktree**.
  The correction prompt includes failed commands, exit codes, and redacted/truncated output.
- A validation command that exceeds `validation.timeout_seconds` is a failed check, not a workflow
  error: `CommandClient` raises `CommandTimeoutError` (a `CommandExecutionError` subtype) carrying
  the configured duration and the partial stdout/stderr, decoded as UTF-8 with replacement.
  The check has no exit code and sets `timed_out`/`timeout_seconds`, later commands still run,
  and it uses the same retry budget. Its correction prompt names the command and timeout and
  includes the redacted tail of both captured streams. Detection relies only on the subprocess
  timeout and never parses tool output. Other command execution errors still fail the workflow.
- Planning, execution ID, branch, and worktree are never recreated during corrections. A passing
  retry continues to commit/push/PR/review. Exhaustion or correction failure ends as `FAILED` and
  preserves the worktree.
- A `CHANGES_REQUESTED` review is retried up to `review.max_review_retries` (default `1`,
  non-negative). While budget remains, the implementer receives the structured review feedback in
  the same worktree, then validation (with its own retry budget) runs again. A passing correction
  is committed and pushed to the existing working branch, updating the existing PR without a new
  PR creation attempt; the reviewer receives a fresh base...HEAD diff in a newly persisted
  `REVIEW` step. Repeated requested changes after the budget is exhausted finish as
  `CHANGES_REQUESTED`; approval finishes as `APPROVED`.
- An `APPROVED` review with non-blocking suggestions gets at most one optional `POLISH` stage per
  execution. The implementer may decline every suggestion; that preserves the approval and posts
  one concise implementer PR comment confirming the suggestions were evaluated and intentionally
  skipped. The persisted polish outcome and pre-publication comment-attempt marker prevent a
  resumed or retried execution from repeating either the evaluation or timeline comment. Comment
  publication failures are non-fatal. Applied scoped polish is validated,
  committed, and pushed to the existing branch before a fresh review; failed polish validation is
  discarded, receives the same skip explanation, and never turns the prior approval into a change
  request.
- A GitHub review-publication failure is logged and recorded without changing the structured
  reviewer decision or its terminal status. GitHub App credentials use separate optional reviewer
  (`integrations.github.app`) and implementer (`integrations.github.implementer_app`) identities.
  The implementer token is scoped to PR CLI operations and HTTPS pushes, while its bot metadata is
  set only for commit subprocesses; SSH pushes retain ambient transport authentication. See
  `docs/github_app.md`.

## Structured agents and integrations

- Planner and reviewer require structured objects. Provider-native structured output is preferred;
  textual output accepts plain JSON, one fenced JSON object, or exactly one safely extractable
  object with surrounding prose. Ambiguous/malformed data is rejected, never guessed.
- For user-facing planning, the planner treats the TUI as a first-class interface and grounds UX
  decisions in the task and current behavior. Specs consider interaction flow, discoverability,
  layout complexity, defaults, state handling, terminal constraints, input behavior, and existing
  patterns where relevant; they keep primary workflows simple, moving configuration to dedicated
  settings views when appropriate, and record applicable decisions in implementation proposals and
  acceptance criteria. Backend-only specs do not receive invented UX requirements.
- Review is adversarial and evidence-based: beyond task/spec compliance, it considers relevant
  operational, architectural, reliability, efficiency, and regression risks. When a task or diff
  affects a CLI, TUI, terminal interface, or user-facing messaging, it also considers grounded
  usability risks; backend-only changes do not receive invented UX requirements. Serious concrete
  usability defects may block approval, while subjective polish remains a non-blocking suggestion.
  Structured reviews separate concrete blocking findings from non-blocking suggestions; suggestions
  may accompany approval. Invalid output is converted conservatively to `NEEDS_HUMAN`;
  contradictory decisions (for example, approval with blocking findings or change requests with
  none) are also unsafe. Final review maps only to `APPROVED`, `CHANGES_REQUESTED`, or
  `NEEDS_HUMAN`.
- Jules uses official asynchronous REST sessions: create → persist identity/URL → poll session
  state → fetch paged activities. It uses configurable poll interval, overall timeout, and bounded
  exponential retries only for transient polling/result requests. It never creates a replacement
  session after creation. `FAILED`, `CANCELLED`, timeout, or human-intervention states surface
  with the original session ID.
- Claude Code is a first-class local-workspace CLI provider. Its safe configuration accepts one
  executable, timeout, optional model and native `effort` (`low`, `medium`, `high`, `xhigh`, or
  `max`), `dontAsk`/`acceptEdits`/`auto` permission mode, optional turn/budget limits, and bare
  mode, never arbitrary flags. When configured, effort becomes exactly `--effort <value>`; when
  omitted, Claude's CLI default applies. The provider constructs `claude -p`
  with JSON output itself, always adds Claude's `--restricted` mode and fixed `Read,Edit,Write,Glob,Grep`
  tool set, passes request instructions on stdin, and uses exactly the supplied workspace as cwd.
  Restricted mode ignores user/project settings, confines file tools to cwd, and prevents shell,
  MCP, and cloud execution; `bare_mode: true` additionally skips auto-discovery. It never creates
  Claude worktrees, adds directories, resumes/continues sessions, bypasses permissions, or owns Git
  branches/commits/pushes/PRs/reviews. Native `--json-schema` structured output is parsed and
  validated locally before `AgentResult`; a successful envelope requires `type=result`,
  `subtype=success`, and non-empty result text. Completion session IDs, whitelisted usage/cost, and
  selected terminal error fields are diagnostic-only and never resume. Permission mode and the
  provider-neutral `reasoning_effort` (translated to Claude's native `effort`) are exposed as
  schema-validated role-local preset/per-run overrides; one named Claude provider can therefore
  give planner/reviewer read-only or distinct thinking depths and implementer editing access without
  duplicate definitions. Provider YAML remains the default whenever that role-local value is omitted.
  Its unavailable CLI,
  authentication, timeout, malformed envelope/schema, invalid model, quota, rate-limit, and capacity
  failures remain provider-local; only normalized quota/rate-limit/capacity errors participate in
  existing one-hop fallback. Claude Code v2.1.259+ and externally supplied authentication are required.
- Jules plan/review requests explicitly disable publishing automation and prompt for read-only
  behavior. Codex uses configured argument lists via the generic command client and forces
  `codex exec --json`; when an `AgentRequest` carries a structured-output expectation, it also
  materializes that existing JSON Schema in a system temporary file and supplies it through
  `--output-schema`, while orchestrator-side validation remains authoritative. Only structured
  `error`, `turn.failed`, or failed-item events can classify
  a sandbox/execution failure. Never infer a failure from free-form command output, diffs, or test
  fixtures. A structured failure after an exit-0 run still inspects Git status so persisted results
  retain any files changed before the failure. Codex may edit only its assigned local workspace.
  Gemini Vertex is an optional specialized backend that uses ADC and has no local shell/filesystem
  access. LiteLLM is a first-class core dependency and generic cloud/local API-model backend; its
  configured API key is an environment-variable reference only, and it also has no implicit local
  shell/filesystem access. Codex CLI is the first-class specialized local-worktree backend. Jules
  is deprecated/legacy: its remote session API does not provide current interactive worktree
  execution, so existing Jules configurations warn on load and instantiation and must not be
  presented as recommended in the TUI or examples. Unknown provider types are unsupported.
- Jules credentials come from `JULES_API_KEY` or `ORCHESTRATOR_JULES_API_KEY`; GitHub PR creation
  uses ambient `gh` authentication unless a configured implementer App supplies a short-lived,
  process-local installation token. Formal review publication independently uses its configured
  reviewer App token or ambient `gh` authentication. The reviewer App is never automatically
  assigned; only the configured implementer App bot can be assigned to created PRs. Do not put
  credentials in YAML or tracked files.

## Configuration precedence

- `repository_context.strategy` is project-scoped, participates in the normal defaults then
  repository override deep merge, and defaults to `heuristic`. `qdrant` requires a registered
  repository key; path-only overrides are rejected. Its local path defaults to
  `~/.orchestrator/qdrant`, resolves explicit relative paths beside the global config, and must be
  outside repository/worktree roots. Server mode accepts exactly one of URL or host (plus optional
  port). Chunk size/overlap, result count, context budget, and explicit embedding identity are
  validated configuration. Unknown strategy names are rejected with the supported names.
- Global `orchestrator.yml` contains top-level task selection and `defaults`; project settings at
  global top level are rejected. For a repository, merge in order: `defaults` → matching
  `repositories.<key>` entry (later wins). Repository-local `.orchestrator.yml` files are ignored
  with a warning and are never created or updated.
- Global configuration discovery is ordered: explicit `--config`; `ORCHESTRATOR_CONFIG`,
  `ORCHESTRATOR_CONFIG_PATH`, or legacy `ORCHESTRATOR_CONFIG_FILE`; then
  `~/.config/orchestrator/orchestrator.yml` and `~/.orchestrator/orchestrator.yml`. It never
  consults the current directory, a repository checkout, or the installed source tree. An
  explicitly selected or
  environment-selected missing file is an error; a failed implicit search reports every path tried.
- Repository selection comes from explicit CLI override when supplied, otherwise normalized task
  repository metadata. A registered entry supplies its local path; an unregistered override is
  interpreted as a repository path.
- Modern configuration is an all-or-nothing pair: both `agents` and `providers` must be present,
  and all three roles must be assigned. Its roles are authoritative; legacy `roles` plus
  `integrations.jules/codex` are normalized only when modern configuration is absent. Never add a
  silent provider fallback. A role may explicitly name one role-local `fallback_preset`; the
  primary remains the first attempt and the fallback is attempted exactly once only for normalized
  quota, rate-limit, or temporary-capacity provider failures. Authentication, configuration,
  validation/schema, policy, and other request failures never trigger failover.
- Safety defaults: direct merge is rejected, protected branches include main/master/develop unless
  changed, and tests are required unless deliberately disabled. Repository-specific settings,
  including TUI edits, are stored in the corresponding `repositories.<key>` entry of the resolved
  global configuration; keep examples generic.
- `agent_presets` contains named, role-scoped (`planner`, `implementer`, `reviewer`) templates of
  a configured provider plus the launcher-exposed model, reasoning, Claude permission mode, and generation options. The
  dedicated TUI Agent Presets screen creates, updates, renames, and deletes them in the resolved
  global configuration; the compact launcher exposes an independent selector per role and only
  applies its preset to that one run. Editing a selected role's launcher settings returns that
  role to Manual, while choosing Manual restores its configured provider/YAML defaults. Switching
  a preset's provider in the editor hides, but does not clear, runtime inputs the newly-selected
  provider does not support (preserving their value if the user switches back); saving only
  serializes fields whose input is currently displayed, so a persisted preset can never contain a
  runtime option foreign to its assigned provider. Presets
  never alter or remove provider definitions. A preset with a missing provider remains persisted
  and is shown as an actionable launcher warning unless it is selected as a fallback, in which
  case configuration validation rejects it before execution. The Agent Presets screen also views,
  enables, changes, and disables each role's fallback. Fallback activation is persisted as a
  sanitized workflow diagnostic/event containing the role and primary/fallback provider and preset
  identities, and is shown in the live TUI timeline.
- Notifications are opt-in per resolved project configuration through `notifications.enabled`
  (default `false`) and `notifications.channel` (currently `desktop`). Workflow policy emits one
  best-effort alert only for terminal `APPROVED`, `FAILED`, `CANCELLED`, `NEEDS_HUMAN`, or exhausted-review
  `CHANGES_REQUESTED` outcomes. Desktop delivery selects native Windows toasts; unsupported,
  headless, or failed delivery only writes a warning and can never alter workflow state.

## Failure semantics

- Configuration errors and explicit `AgentNeedsHumanError` end as `NEEDS_HUMAN`; requested user
  cancellation ends as `CANCELLED`; all other stage,
  provider, Git, command, or persistence errors end as `FAILED`. The triggering step is persisted
  as failed with its error and the execution retains its last recorded snapshots.
- No compensation is attempted after external side effects. Inspect persisted execution ID,
  workspace, branch, session reference, and PR/commit fields rather than assuming a clean retry.
- A successful review sets `current_stage=COMPLETED`; terminal failed/cancelled/human executions retain the
  stage that failed or stopped them.

## `debug` is diagnostics, not resume

- `debug` runs exactly one selected boundary (`task-loading`, `spec-generation`, `implementation`,
  `validation`, or `review`) and never mutates the referenced execution, creates workflow steps,
  branches, commits, pushes, or PRs.
- `--from-execution` is read-only input fallback (task/spec/validation/PR/branch metadata), not a
  request to restore its workspace or Git state. Explicit files/options override historical data.
- Non-dry implementation debug uses the currently configured repository checkout, not the original
  working branch/worktree; it can therefore edit that selected checkout. Use `--dry-run` for
  input/configuration validation without creating clients, fetching tasks, or invoking providers.
- Inputs: task loading needs a task source/ID; spec needs task + repository; implementation needs
  prompt or task+spec; validation needs repository configuration; review needs task, spec,
  validation, PR, feature branch, and diff (explicit or current workspace).

## Agent maintenance rules

- Keep this file compact, behavioral, and durable. Update it only when architecture, lifecycle,
  invariants, persistence, configuration precedence, safety policy, or supported operational flow
  changes.
- Do not turn this into a source-file inventory or duplicate code. Keep provider details below the
  provider boundary and preserve the existing safety/ownership split.
