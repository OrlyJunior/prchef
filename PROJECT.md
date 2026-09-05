# Project context

## Purpose and boundary

`autonomous-dev-orchestrator` is a Python 3.11+ CLI that turns one Trello card or GitHub issue
into a guarded development workflow: plan, implement in an isolated Git worktree, validate,
commit/push, open a GitHub PR, and review. It coordinates agents and integrations; it is not a
general repository browser, merge bot, or source-control replacement.

## Architecture

- `models` contains infrastructure-free dataclasses/enums. `services` owns workflow rules,
  prompt assembly, result parsing, validation, and isolated debug stages.
- `providers` exposes the provider-neutral `AgentProvider.execute(AgentRequest)` boundary and
  adapters for Jules, Codex CLI, and Gemini Vertex. `clients` owns HTTP, subprocess, Git, and
  GitHub/Trello/Jules details. `repositories` is the SQLAlchemy persistence boundary.
- `config` loads settings and merges YAML; `cli` composes dependencies. Schema changes are Alembic
  migrations, never runtime `create_all`.

## Workflow and invariants

1. Load exactly one normalized `DevelopmentTask` from Trello or GitHub, resolve its repository,
   then merge configuration.
2. Planner produces a structured `RepositorySpec`; blocking ambiguity ends as `NEEDS_HUMAN`.
3. Create a new non-protected feature branch and disposable external worktree. Implementer edits
   only that workspace; local validation may request bounded corrections.
4. Commit all changes on the feature branch, push, create a PR, then reviewer returns structured
   `APPROVED`, `CHANGES_REQUESTED`, or `NEEDS_HUMAN`. There is never automatic merge or a formal
   GitHub review submission.

Workflow executions and steps, task/spec/implementation/validation/PR/review snapshots, and Jules
session references are persisted. Failed or human-blocked worktrees are preserved for inspection.
Malformed or contradictory structured agent output is handled conservatively.

## Configuration and integrations

- Global `orchestrator.yml` defaults merge with a repository registry entry, then target
  `.orchestrator.yml`; later values take precedence. Explicit CLI task/repository inputs override
  inferred values where supported. Credentials come from environment/`.env`, not versioned files.
- Agent roles (`planner`, `implementer`, `reviewer`) name configured provider instances. Providers
  are `jules`, `codex-cli`, or `gemini-vertex`; roles may share instances. Jules is remote
  read-only for planning/review, Codex runs locally with its configured sandbox, and Gemini Vertex
  is request/response via ADC without local filesystem or shell access.
- GitHub interaction uses authenticated `gh`; Trello is HTTP; Jules uses its official sessions API.
  Validation uses configured commands or conservative project-type auto-detection. With required
  tests, no detected commands is a workflow failure.

## Conventions and limitations

- Preserve provider neutrality above adapters; avoid embedding provider-specific behavior in role
  services. Treat task text as authoritative and repository behavior as authoritative over stale
  semantic context.
- Protect configured base branches; direct merge is rejected by V1. The orchestrator owns commits,
  push, PR creation, and final workflow persistence; implementation agents must not perform those
  operations.
- V1 does not resume interrupted workflows or sessions, auto-merge, post formal GitHub reviews, or
  rerun planning after validation failures. It does not provide repository search/RAG.

Keep this file compact and semantic. Update it only for durable changes to the architecture,
workflow, domain rules, configuration, persistence, integrations, decisions, or limitations.
