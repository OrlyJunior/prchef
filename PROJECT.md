# Durable agent context

## Purpose and boundaries

- Python 3.11+ CLI and Textual terminal UI that turn one Trello card or GitHub issue into a
  guarded change: plan → isolated implementation → validation → commit/push → GitHub PR →
  review publication.
- It coordinates providers, Git, GitHub, and persistence. It is **not** a merge bot or a generic
  repository browser/RAG service. Human-blocked executions are resumable using their persisted
  workflow state; this is not a provider-session resume system.
- `PROJECT.md` is shared supporting context for planning, implementation, and review prompts
  (capped at 12,000 characters). `IMPLEMENTER.md` and `REVIEWER.md` are optional, capped,
  role-specific local
  guidance loaded from the configured repository root at execution time: implementers receive
  only `IMPLEMENTER.md`, reviewers receive only `REVIEWER.md`. All are supplementary; the current
  task, repository state, and core orchestrator invariants win. The role files are Git-ignored by
  default, can be versioned deliberately, and are editable from the TUI without staging or commit.

## Ownership and layering

- `models`: infrastructure-free domain data. `services`: workflow policy, prompts, parsing,
  validation, and debug orchestration. `providers`: role-neutral agent adapter boundary.
- `clients`: transport/subprocess/Git details. `repositories`: the only SQLAlchemy persistence
  boundary. `cli`: dependency composition and stdout/stderr contract. `tui`: a Textual
  presentation/controller layer over configuration, task providers, the workflow service, and
  persisted execution reads; it must not implement workflow policy or a second state machine.
- The TUI presents workflow observer events as a concise user-facing stage timeline by default.
  Raw observer-event diagnostics are available only through its detailed-log toggle; this does
  not alter the CLI's verbose logging contract or workflow observer semantics.
- TUI screens use `OrchestratorSelect` for Select controls in vertical scroll regions. It reserves
  visible overlay space after mouse or keyboard activation and restores focus without triggering
  parent-scroll jumps. Repository controls use its `RepositorySelect` specialization: configured
  repository keys are both their visible labels and Select values, with the selected key retained
  by `WorkflowController` across screen navigation.
- The TUI launcher may select configured provider instances and role-local runtime settings
  (model, reasoning, and exposed provider generation options) for one run. These overrides are
  schema-validated in memory, never alter YAML defaults, and may give roles sharing a configured
  provider independent runtime settings. Its default launcher view presents compact per-role
  provider/model/runtime summaries; role configuration and generation controls are progressive,
  and generation fields identify Provider/YAML defaults versus temporary overrides. GitHub App
  identities remain separately persisted, inspectable configuration.
- The TUI launcher automatically browses tasks for its selected repository and task provider.
  Users choose a displayed task; its provider-specific identifier remains internal to the launch
  data. The interactive selector exposes refresh, loading, and inline-error states, while explicit
  CLI task identifiers remain supported.
- The TUI's Agent Instructions view edits the selected repository's optional `IMPLEMENTER.md` and
  `REVIEWER.md` independently. Missing files remain absent until their own save action; the TUI
  never stages or commits either file.
- Provider-specific commands, polling, credentials, or API shapes stay below
  `AgentProvider.execute(AgentRequest)`. Planner/implementer/reviewer services stay provider
  neutral.
- `AgentProvider` is the orchestrator's provider boundary. Model-routing libraries such as
  LiteLLM are implementation backends behind that boundary and must not own workflow semantics,
  persistence, scheduling, role behavior, or lifecycle state.
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
  resolver; repository-local configuration never selects persistence.
- Runtime logs are UTC ISO-8601 timestamps on stderr. Final `run` execution JSON and all `debug`
  result JSON stay on stdout. Redact secrets before logging.
- An execution is created as `PENDING`, immediately marked `RUNNING`, and records every entered
  stage as a separately persisted step. Repeated validation/correction attempts create repeated
  `VALIDATE`/`IMPLEMENT` steps.
- Persist: normalized task, resolved repository/base branch and configured repository reference,
  worktree path, branch, latest
  implementation and validation snapshots, commit, PR, review, status/error, and timestamps.
  The implementation/validation snapshots are latest-attempt snapshots, not an attempt history.
- A `NEEDS_HUMAN` transition creates an append-only human-interaction record containing the
  requesting stage, exact blocking prompt, request/response timestamps, and the response. `respond`
  and the TUI resume the same execution ID; planner responses rerun planning before any workspace is
  created, while review responses reuse the persisted worktree, branch, PR, and snapshots.
- Provider startup references during spec/review are persisted immediately in legacy-named
  `spec_jules_session_*` / `review_jules_session_*` fields. Preserve those fields for later human
  inspection; no automatic session/workflow resume exists.
- Workflow observer events are emitted only after persisted status/step transitions. They are for
  live terminal presentation and listeners may never change workflow semantics or make a run fail.

## Normal `run` behavior

1. CLI accepts exactly one task origin: `--trello-card`, `--github-issue`, or `--task-id`.
   `--task-id` uses configured `task.provider`; GitHub task loading requires `--repository`.
   Trello/GitHub clients normalize input into `DevelopmentTask` before configuration is resolved.
2. Resolve the project configuration and derive the GitHub slug from `origin`. Create configured
   planner/implementer/reviewer instances; a named provider shared by roles is instantiated once.
3. Generate one structured `RepositorySpec` from the base checkout/base branch. Planning receives
   a bounded, read-only local repository overview and task-matched code excerpts, alongside
   `PROJECT.md` when present. A blocking spec ambiguity ends the execution as `NEEDS_HUMAN` before
   any branch or worktree is created.
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
- Worktrees are retained automatically on success, failure, and human intervention so they remain
  available for inspection and recovery. `cleanup-workspaces --repository <key-or-path> --dry-run`
  lists their retention decisions; without `--dry-run` it removes successful (`APPROVED` or
  `CHANGES_REQUESTED`) worktrees and prunes Git metadata. `FAILED`/`NEEDS_HUMAN` worktrees require
  `--include-failed` (or `--all`); `PENDING`, `RUNNING`, unmanaged, and out-of-root paths are never
  removed by this command.
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
  skipped. Comment publication failures are non-fatal. Applied scoped polish is validated,
  committed, and pushed to the existing branch before a fresh review; failed polish validation is
  discarded and never turns the prior approval into a change request.
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
- Jules plan/review requests explicitly disable publishing automation and prompt for read-only
  behavior. Codex uses configured argument lists via the generic command client and forces
  `codex exec --json`: only structured `error`, `turn.failed`, or failed-item events can classify
  a sandbox/execution failure. Never infer a failure from free-form command output, diffs, or test
  fixtures. A structured failure after an exit-0 run still inspects Git status so persisted results
  retain any files changed before the failure. Codex may edit only its assigned local workspace.
  Gemini Vertex uses ADC and has no local shell/filesystem access. LiteLLM is an optional generic
  cloud/local API-model backend; its configured API key is an environment-variable reference only,
  and it also has no implicit local shell/filesystem access.
- Jules credentials come from `JULES_API_KEY` or `ORCHESTRATOR_JULES_API_KEY`; GitHub PR creation
  uses ambient `gh` authentication unless a configured implementer App supplies a short-lived,
  process-local installation token. Formal review publication independently uses its configured
  reviewer App token or ambient `gh` authentication. The reviewer App is never automatically
  assigned; only the configured implementer App bot can be assigned to created PRs. Do not put
  credentials in YAML or tracked files.

## Configuration precedence

- Global `orchestrator.yml` contains top-level task selection and `defaults`; project settings at
  global top level are rejected. For a repository, merge in order: `defaults` → matching
  `repositories.<key>` entry (later wins). Repository-local `.orchestrator.yml` files are ignored
  with a warning and are never created or updated.
- Global configuration discovery is ordered: explicit `--config`; `ORCHESTRATOR_CONFIG`,
  `ORCHESTRATOR_CONFIG_PATH`, or legacy `ORCHESTRATOR_CONFIG_FILE`; `orchestrator.yml` in the
  current directory and its parents; `~/.config/orchestrator/orchestrator.yml`; then
  `~/.orchestrator/orchestrator.yml` and the installed source checkout. An explicitly selected or
  environment-selected missing file is an error; a failed implicit search reports every path tried.
- Repository selection comes from explicit CLI override when supplied, otherwise normalized task
  repository metadata. A registered entry supplies its local path; an unregistered override is
  interpreted as a repository path.
- Modern configuration is an all-or-nothing pair: both `agents` and `providers` must be present,
  and all three roles must be assigned. Its roles are authoritative; legacy `roles` plus
  `integrations.jules/codex` are normalized only when modern configuration is absent. Never add a
  silent provider fallback.
- Safety defaults: direct merge is rejected, protected branches include main/master/develop unless
  changed, and tests are required unless deliberately disabled. Repository-specific settings,
  including TUI edits, are stored in the corresponding `repositories.<key>` entry of the resolved
  global configuration; keep examples generic.
- `agent_presets` contains named, role-scoped (`planner`, `implementer`, `reviewer`) templates of
  a configured provider plus the launcher-exposed model, reasoning, and generation options. The
  launcher can select, create, update, rename, or delete them in the resolved global configuration;
  selecting a preset only seeds one run's controls, and later edits remain temporary until an
  explicit update. Presets never alter or remove provider definitions. A preset with a missing
  provider remains persisted and is shown as an actionable launcher warning.
- Notifications are opt-in per resolved project configuration through `notifications.enabled`
  (default `false`) and `notifications.channel` (currently `desktop`). Workflow policy emits one
  best-effort alert only for terminal `APPROVED`, `FAILED`, `NEEDS_HUMAN`, or exhausted-review
  `CHANGES_REQUESTED` outcomes. Desktop delivery selects native Windows toasts; unsupported,
  headless, or failed delivery only writes a warning and can never alter workflow state.

## Failure semantics

- Configuration errors and explicit `AgentNeedsHumanError` end as `NEEDS_HUMAN`; all other stage,
  provider, Git, command, or persistence errors end as `FAILED`. The triggering step is persisted
  as failed with its error and the execution retains its last recorded snapshots.
- No compensation is attempted after external side effects. Inspect persisted execution ID,
  workspace, branch, session reference, and PR/commit fields rather than assuming a clean retry.
- A successful review sets `current_stage=COMPLETED`; terminal failed/human executions retain the
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
