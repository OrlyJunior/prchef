# Durable project context

Durable purpose, boundaries, architecture and invariants. Feature and UI details belong in code,
tests and repository retrieval.

## Purpose and boundaries

- Python 3.11+ CLI and Textual TUI that turn one Trello card or GitHub issue into a guarded change:
  plan → isolated implementation → validation → commit/push → GitHub PR → review publication.
- It coordinates providers, Git, GitHub, and persistence; it never merges and is not a generic
  repository browser or RAG service.
- It must work for other repositories and languages. Core behavior stays language- and
  tool-agnostic; nothing in core may assume this repository's structure, tools, or issue numbers.
- Human-blocked executions resume from persisted workflow state. This is not a provider-session
  resume system.
- Planning uses up to 12,000 characters of the task's newest comments. Resumed planning uses the
  persisted snapshot; other roles receive no separate comment section.
- Optional local, gitignored `PLANNER.md`, `IMPLEMENTER.md`, and `REVIEWER.md` guide their
  respective roles; this file is shared. Task, repository state, and core invariants take precedence.
  Role files load from the configured checkout, falling back to the control checkout for worktrees;
  symlinked and non-regular files are ignored. Prompt context has explicit size bounds.

## Operating model

- In-memory dispatch queues are process-scoped; SQLite is the default. Local Qdrant is single-process.
  Retrieval, indexing, and cleanup share admission per canonical local store; server operations
  stay concurrent. Workflows wait; screen actions try admission without overtaking them.
  Shutdown waits for admitted work. Provider ceilings have no cross-process coordination.
- Startup marks orphaned active executions interrupted; resumption requires an explicit user choice.
- Configuration comes only from a global `prchef.yml` (`defaults` deep-merged with
  `repositories.<key>`). Repository-local configuration files are ignored. Discovery and
  persistence never depend on the current directory or a source checkout. Explicit CLI and
  environment paths precede user-global defaults; a missing explicitly selected file is an error.
  Modern `agents`/`providers` configuration must assign all three roles and never silently falls
  back to legacy roles.
- Persistent state is global per user. CLI commands that read workflow state apply pending Alembic
  migrations. The package supports isolated per-user `pipx` installation and never alters PATH or
  shell profiles.
- Logs go to stderr with UTC timestamps; final `run` and `debug` JSON go to stdout.

## Layering

- `models`: infrastructure-free domain data. `services`: workflow policy, prompts, parsing,
  validation. `providers`: role-neutral agent adapters. `clients`: transport, subprocess, Git.
  `repositories`: the only SQLAlchemy boundary. `cli`: composition and stdout/stderr contract.
  `tui`: presentation over the controller.
- The TUI projects durable workflow state without its own policy or state machine. Its timeline
  summarizes events; detailed logs and clipboard exports retain redacted diagnostics. Live timelines
  surface durable-context size warnings with actual and capped character counts.
- Provider commands, credentials, polling, and API shapes stay below
  `AgentProvider.execute(AgentRequest)`. Role services are provider-neutral. Model-routing
  libraries (LiteLLM) are backends behind that boundary and never own workflow semantics.
- Repository context retrieval is a separate prchef-owned strategy boundary (local Qdrant by
  default for registered repositories, heuristic on semantic unavailability or explicit selection).
  Workflow services never import retrieval backends. Role files and this file are never indexed.
  Qdrant collections are repository-scoped; first retrieval builds the index. Linked roots never
  update provenance. Exact text and embedding identity key reusable vectors across roots; root
  inventories own source points, while vectors survive temporary cleanup. Schema changes rebuild.
- Every execution, response, and continuation enters through one submission seam. Scheduling
  features must use it rather than adding their own concurrency.
- The TUI may launch one task or an ordered persisted batch. Batch policy, cancellation, and
  admission live in `WorkflowController`/`WorkflowRepository`; batch progress is a projection.
  Batch items link to, but do not own, execution rows. Cancelling one item never cancels its peers.
- Settings update global entries; provider writes preserve unrelated YAML bytes with validated
  atomic replacement. Role-local runtime selections persist for resumption without rewriting YAML
  defaults. Value/source displays reuse the configuration submission seam.
- Schema changes require versioned Alembic migrations; never `create_all` at runtime.

## Workflow invariants

- Status is the outcome; stage is progress. `PENDING` = not started; `WAITING` = started but
  waiting for provider capacity. A successful review ends at stage `COMPLETED`; other terminal
  executions keep the stage that stopped them. Observer events follow persisted transitions;
  listeners can never change workflow outcomes. TUI observer delivery queues presentation without
  waiting for the UI. A `WORKFLOW_FINISHED` event ends live TUI controls
  for every terminal status, including `CHANGES_REQUESTED`.
- A blocking planning ambiguity ends as `NEEDS_HUMAN` before any branch or worktree exists.
- A fresh execution records one fetched base commit before planning. Planning reads a temporary
  snapshot of that commit; its linked worktree starts there. The control checkout is never
  switched, cleaned, stashed, or reset.
  Worktrees are retained on every outcome; removal happens only through the explicit cleanup
  command.
- Cleanup removes only managed, terminal worktrees in the configured root. Failed, cancelled,
  and human-blocked worktrees need explicit inclusion; active and unmanaged paths are excluded.
  For Qdrant retrieval, cleanup removes a worktree's index points before its Git worktree;
  index deletion failure leaves the worktree available for retry.
- Agents never commit, push, create PRs, or merge; prchef owns Git and GitHub. A
  reported successful implementation with no changes is a failure.
- Commit and push refuse protected branches; push is only `--set-upstream origin <branch>`. The
  worktree must share a merge base with the recorded base commit before every commit and review;
  review diffs compare that commit to HEAD. Legacy rows without a recorded commit retain the
  local base-branch comparison without fetching or recreating their worktree.
- All selected validation commands run. A timeout is a failed check only once the whole process
  tree is confirmed stopped; otherwise the workflow fails without correction.
- Validation starts with configured commands and conservatively detected commands. Required tests
  need a recognizable test command. A timed-out command retains redacted partial output, has no
  exit code, and shares the validation retry budget after its process tree is stopped. Corrections
  receive failed commands and summaries of every failed pytest test within prompt bounds.
- Validation failures and requested changes are corrected in the same worktree, within configured
  retry budgets. Planning, branch, and worktree are never recreated during corrections.
- An approved review may get at most one optional polish pass, which can never turn the approval
  into a change request.
- No compensation after external side effects: a later failure never rolls back a commit, push,
  PR, or worktree.
- Cancellation is a durable request that ends as `CANCELLED`, never `FAILED`, observed at safe
  boundaries. Pending and provider-waiting work is woken or dropped, and a request accepted before
  the final transition cannot leave the execution active or awaiting human input. Cancellation
  must not revive a batch item through a late response or completion event.
- `NEEDS_HUMAN` records the blocking prompt and response. An answer resumes the same execution:
  planner answers rerun planning before workspace creation; review answers reuse the worktree,
  branch, PR, and snapshots. Eligible `CHANGES_REQUESTED` and failed executions with retained
  workspace/branch can be continued from `IMPLEMENT`; an existing PR must still be open.
- Configuration errors and explicit human-needed errors end as `NEEDS_HUMAN`; everything else
  ends as `FAILED`.
- `debug` stages leave executions, branches, commits, and PRs unchanged. Saved-state startup
  can migrate data and mark orphaned active runs `INTERRUPTED`; `execution_mutated: false`
  excludes startup.

## Concurrency and providers

- Concurrency is bounded per named provider instance (`max_concurrency`, default 1, set only in
  `defaults.providers`), because that instance is the real shared resource: one quota pool or one
  GPU. There is no global ceiling, resource-pool mapping, or live resizing.
- A permit is held only during the provider call, never across Git, validation, or GitHub work.
  Runtime overrides and presets are charged to the provider they derive from.
- Fallback is explicit and one hop, only for normalized quota, rate-limit, or capacity failures.
  The primary permit is released before acquiring fallback capacity. Authentication, policy,
  configuration, and schema failures do not trigger fallback. There is never a silent fallback.
- Planner and reviewer require explicit provider selection. Legacy roles normalize only when they
  resolve to a supported configured provider; unsupported definitions fail during config load.
- Planner and reviewer are read-only and return structured output validated locally. Ambiguous or
  malformed output is rejected, never guessed. Review separates blocking findings from optional
  suggestions; approval may trigger one bounded polish pass but cannot be revoked by it.
- Codex CLI and Claude Code are first-class worktree backends; Claude uses restricted, fixed tools.
  LiteLLM handles API models, including Vertex Gemini (`vertex_ai/`), without filesystem access;
  implementation needs a CLI. Legacy `gemini-vertex` loads convert in memory, preserving names
  and YAML; unmappable fields fail. Saved executions need no migration.
- Behaviors that trade quota against throughput are configurable, not hardcoded to one user's
  limits.
- Legacy-named planner/reviewer session-reference fields remain in persisted executions and
  history reads. Provider removal does not rename them or require a data migration.

## Safety and diagnostics

- Credentials never go in YAML or tracked files; API keys are environment-variable references.
- Diagnostics are redacted before persistence and before observer delivery. Observer events are
  emitted only after persisted transitions and can never change workflow semantics.
- Text redaction infers unknown secret values from explicit assignments, not whitespace alone;
  recognized credential formats and sensitive structured fields remain independently protected.
- Rendering durable context above its prompt cap records a warning diagnostic with the stripped
  character count, path, and cap. The prompt still uses the capped content. Diagnostics flow to
  `--verbose`, the live TUI timeline, and persisted detailed logs without including file contents.
- GitHub App identities (planner, implementer, reviewer) are optional and separate; without them,
  ambient `gh` authentication is used. Publication failures never change a review decision.
- Notifications are opt-in and best-effort; delivery failure never affects workflow state.

## Maintaining this file

- Update only changed purpose, operating model, layering, invariants or design decisions;
  no feature/UI details or per-change notes. Stay under 12,000 characters by condensing or replacing.
