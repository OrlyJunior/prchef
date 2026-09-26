# Durable project context

This file holds what the code alone doesn't make obvious: purpose and boundaries, the operating
model, layering rules, invariants, and design decisions (including things deliberately not done).
Feature behavior, UI layout, and implementation details are not described here; the code, tests,
and repository retrieval cover those.

## Purpose and boundaries

- Python 3.11+ CLI and Textual TUI that turn one Trello card or GitHub issue into a guarded change:
  plan → isolated implementation → validation → commit/push → GitHub PR → review publication.
- It coordinates providers, Git, GitHub, and persistence. It is not a merge bot and never merges.
  It is not a generic repository browser or RAG service.
- It must work for other repositories and languages. Core behavior stays language- and
  tool-agnostic; nothing in core may assume this repository's structure, tools, or issue numbers.
- Human-blocked executions resume from persisted workflow state. This is not a provider-session
  resume system.
- Planning uses the task's own comment thread as supporting context, bounded to 12,000 characters
  with the newest content retained. Resumed planning uses the persisted task snapshot; other roles
  do not receive a separate comment section.
- `PLANNER.md`, `IMPLEMENTER.md`, and `REVIEWER.md` are optional, local, Git-ignored-by-default
  role guidance; each role receives only its own file. This file is shared by all roles. All of
  them are supplementary: the task, repository state, and core invariants win.

## Operating model

- Single user, single process. Dispatch queues are in-memory; SQLite is the default database;
  Qdrant local mode is single-process. Concurrent processes, other database backends, and
  adversarial timing are outside the supported model and are not requirements.
- Persisted `PENDING`/`WAITING` executions are not recovered automatically at startup.
- Configuration comes only from a global `orchestrator.yml` (`defaults` deep-merged with
  `repositories.<key>`). Repository-local configuration files are ignored. Discovery and
  persistence never depend on the current directory or a source checkout.
- Persistent state is global per user. Installed as an isolated per-user `pipx` package; the
  application never alters PATH or shell profiles.
- Logs go to stderr with UTC timestamps; final `run` and `debug` JSON go to stdout.

## Layering

- `models`: infrastructure-free domain data. `services`: workflow policy, prompts, parsing,
  validation. `providers`: role-neutral agent adapters. `clients`: transport, subprocess, Git.
  `repositories`: the only SQLAlchemy boundary. `cli`: composition and stdout/stderr contract.
  `tui`: presentation over the controller.
- The TUI never implements workflow policy or a second state machine; its batch progress is a
  read-side projection of durable state.
- Provider commands, credentials, polling, and API shapes stay below
  `AgentProvider.execute(AgentRequest)`. Role services are provider-neutral. Model-routing
  libraries (LiteLLM) are backends behind that boundary and never own workflow semantics.
- Repository context retrieval is a separate orchestrator-owned strategy boundary (heuristic by
  default, Qdrant optional). Workflow services never import retrieval backends. Role files and
  this file are never indexed.
- Every execution, response, and continuation enters through one submission seam. Scheduling
  features must use it rather than adding their own concurrency.
- Schema changes require versioned Alembic migrations; never `create_all` at runtime.

## Workflow invariants

- Status is the outcome; stage is progress. `PENDING` = not started; `WAITING` = started but
  waiting for provider capacity. A successful review ends at stage `COMPLETED`; other terminal
  executions keep the stage that stopped them.
- A blocking planning ambiguity ends as `NEEDS_HUMAN` before any branch or worktree exists.
- Each execution works in its own linked worktree created from freshly fetched
  `origin/<base_branch>`. The control checkout is never switched, cleaned, stashed, or reset.
  Worktrees are retained on every outcome; removal happens only through the explicit cleanup
  command.
- Agents never commit, push, create PRs, or merge; the orchestrator owns Git and GitHub. A
  reported successful implementation with no changes is a failure.
- Commit and push refuse protected branches; push is only `--set-upstream origin <branch>`. The
  worktree must share a merge base with the local base branch before every commit and review.
- All selected validation commands run. A timeout is a failed check only once the whole process
  tree is confirmed stopped; otherwise the workflow fails without correction.
- Validation failures and requested changes are corrected in the same worktree, within configured
  retry budgets. Planning, branch, and worktree are never recreated during corrections.
- An approved review may get at most one optional polish pass, which can never turn the approval
  into a change request.
- No compensation after external side effects: a later failure never rolls back a commit, push,
  PR, or worktree.
- Cancellation is a durable request that ends as `CANCELLED`, never `FAILED`, observed at safe
  boundaries.
- Configuration errors and explicit human-needed errors end as `NEEDS_HUMAN`; everything else
  ends as `FAILED`.
- `debug` is diagnostics, not resume: it runs one boundary and never mutates executions, branches,
  commits, or PRs.

## Concurrency and providers

- Concurrency is bounded per named provider instance (`max_concurrency`, default 1, set only in
  `defaults.providers`), because that instance is the real shared resource: one quota pool or one
  GPU. There is no global ceiling, resource-pool mapping, or live resizing.
- A permit is held only during the provider call, never across Git, validation, or GitHub work.
  Runtime overrides and presets are charged to the provider they derive from.
- Fallback is explicit and one hop, only for normalized quota, rate-limit, or capacity failures.
  There is never a silent provider fallback.
- Planner and reviewer require explicit provider selection. Legacy roles normalize only when they
  resolve to a supported configured provider; unsupported definitions fail during config load.
- Planner and reviewer are read-only and return structured output validated locally. Ambiguous or
  malformed output is rejected, never guessed.
- Codex CLI and Claude Code are the first-class local-worktree backends; Claude Code runs in
  restricted mode with a fixed tool set. LiteLLM is the generic API backend with no implicit
  filesystem access.
- Behaviors that trade quota against throughput are configurable, not hardcoded to one user's
  limits.
- Legacy-named planner/reviewer session-reference fields remain in persisted executions and
  history reads. Provider removal does not rename them or require a data migration.

## Safety and diagnostics

- Credentials never go in YAML or tracked files; API keys are environment-variable references.
- Diagnostics are redacted before persistence and before observer delivery. Observer events are
  emitted only after persisted transitions and can never change workflow semantics.
- GitHub App identities (planner, implementer, reviewer) are optional and separate; without them,
  ambient `gh` authentication is used. Publication failures never change a review decision.
- Notifications are opt-in and best-effort; delivery failure never affects workflow state.

## Maintaining this file

- Update it only when purpose, operating model, layering, an invariant, or a design decision
  changes. Do not add feature behavior, UI details, or per-change notes.
- It must stay under the 12,000-character prompt cap. Condense or replace existing lines instead
  of appending.
