# Autonomous Dev Orchestrator

Autonomous Dev Orchestrator turns a GitHub issue or Trello card into a reviewed pull request. It plans the change, implements it in an isolated Git worktree, runs your validation commands, opens a pull request, and performs an automated review. It never merges: the final decision is always yours.

<!-- screenshot: TUI launcher -->

## Status

This is an early v0.1.0 release, designed for one user running one process.

Known limitations:

- Work runs inside the app's process. Closing the app interrupts active executions; on reopening, you can choose which interrupted executions to resume.
- On macOS, recovery checks retained worktrees for surviving processes and leaves an execution interrupted until its worktree is idle.
- SQLite is the default database, local Qdrant is single-process, and provider concurrency limits don't coordinate across processes.
- Nothing is rolled back after a failure: a pushed branch or open pull request stays in place. Inspect the saved execution and its worktree before continuing.

## Requirements

- Python 3.11 or newer, and Git.
- The GitHub CLI (`gh`), installed and authenticated for the repository you want to use.
- At least one agent backend:
  - [Codex CLI](https://github.com/openai/codex) v0.156.0 or newer (the minimum version verified with this project's required flags)
  - [Claude Code](https://docs.claude.com/en/docs/claude-code) v2.1.259 or newer
  - An API model through [LiteLLM](https://docs.litellm.ai/)
  - Optionally, Gemini on Vertex AI (`gemini-vertex`)

## Installation

Install the command in an isolated environment with [pipx](https://pipx.pypa.io/):

```sh
pipx install git+https://github.com/OrlyJunior/autonomous-development-orchestrator
```

Or, from a local clone:

```sh
pipx install .
```

For development, install the package and contributor tools in a virtual environment:

```sh
python -m pip install -e ".[dev]"
```

## Configuration

The app reads one global `orchestrator.yml`. An explicit `--config` path takes precedence, followed by the `ORCHESTRATOR_CONFIG`, `ORCHESTRATOR_CONFIG_PATH`, and `ORCHESTRATOR_CONFIG_FILE` environment variables. Otherwise it looks in `~/.config/orchestrator/orchestrator.yml`, then `~/.orchestrator/orchestrator.yml`. Repository-local `.orchestrator.yml` files are ignored.

This example uses one Codex provider for all three roles. Replace the repository path, base branch, and validation commands with values for your checkout:

```yaml
defaults:
  repository:
    base_branch: main
  agents:
    planner:
      provider: codex
    implementer:
      provider: codex
    reviewer:
      provider: codex
  providers:
    codex:
      type: codex-cli
      command: [codex, exec, --sandbox, workspace-write, --output-last-message, "{output_file}", -]
      approval_policy: approve-for-me
  integrations:
    github:
      command: [gh]
  validation:
    commands:
      - [python, -m, pytest]

repositories:
  owner/repository:
    path: /absolute/path/to/repository
```

For unattended Codex runs, we recommend `approval_policy: approve-for-me` as shown. The adapter passes `--approve-for-me` and removes the explicit `--sandbox` argument; Codex uses the `workspace-write` sandbox for this mode. [Codex Auto-review](https://learn.chatgpt.com/docs/sandboxing/auto-review) handles actions that need approval, while ordinary edits inside the worktree proceed without review. If Auto-review is unavailable for your account, omit `approval_policy` and keep the `--sandbox workspace-write` command.

Each provider runs one agent call at a time by default. To allow more, set `max_concurrency` on the provider under `defaults.providers`.

For the `claude-code` provider, `bare_mode` defaults to `false`. Claude subscription users should leave it off (omit it or set `bare_mode: false`) and authenticate with Claude Code outside the orchestrator.

Optionally set `bare_mode: true` for reproducible runs that skip user hooks, plugins, skills, and MCP auto-discovery. [Bare mode requires an API key (`ANTHROPIC_API_KEY`) or supported cloud credentials](https://code.claude.com/docs/en/headless#start-faster-with-bare-mode); it does not use subscription login credentials. The provider always passes [`--restricted`](https://code.claude.com/docs/en/cli-reference#cli-flags), which confines Claude Code's file tools to the supplied worktree with or without bare mode. See [orchestrator.example.yml](orchestrator.example.yml) for provider options.

Credentials belong in environment variables or ambient `gh` authentication, never in YAML. For Trello, set `ORCHESTRATOR_TRELLO_API_KEY` and `ORCHESTRATOR_TRELLO_TOKEN`. Optional GitHub App identities are described in [GitHub App setup](docs/github_app.md).

## First run

Start the terminal UI:

```sh
orchestrator tui
```

On the home screen, choose **Repositories** → **Add repository** → **Save repository** to register a local checkout, if it isn't already in your configuration. Then choose **Start workflow**, select the repository and task provider, pick a task, and start it.

The equivalent command for a GitHub issue:

```sh
orchestrator run --github-issue ISSUE_NUMBER --repository owner/repository
```

For a Trello card, use `orchestrator run --trello-card CARD_ID --repository owner/repository` after configuring its credentials.

## CLI overview

| Command | What it does |
| --- | --- |
| `orchestrator run` | Run one GitHub issue (`--github-issue`), Trello card (`--trello-card`), or configured task provider ID (`--task-id`). |
| `orchestrator tui` | Open the interactive launcher, settings, and execution history. |
| `orchestrator respond EXECUTION_ID --message "..."` | Answer an agent's blocking question and resume that execution. |
| `orchestrator show EXECUTION_ID` | Print a saved execution as JSON. |
| `orchestrator cleanup-workspaces --repository owner/repository` | Remove eligible retained worktrees. Use `--dry-run` to preview. |
| `orchestrator debug --stage STAGE` | Run one diagnostic stage. `--dry-run` previews its inputs without calling integrations. |
| `orchestrator providers` | List configured agent providers. |
| `orchestrator --version` | Print the installed version. |

## Repository context

Agents get relevant code from your repository through Qdrant semantic retrieval, which is the default for registered repositories. The first time it's used, the orchestrator downloads a small local embedding model and builds an index of your repository. This takes some CPU time on large repositories, but only happens once; later runs only process files that changed.

If semantic retrieval is unavailable, a simpler heuristic retrieval is used instead. You can also select it permanently with `repository_context.strategy: heuristic`. Local Qdrant is the default; `repository_context.qdrant.mode: server` connects to a separately running Qdrant server configured with `url` or `host`.

## Role guidance files

Optional local `PLANNER.md`, `IMPLEMENTER.md`, and `REVIEWER.md` files in your repository give each role its own extra instructions. They're ignored by Git by default, and each role sees only its own file. You can edit them from the TUI.

We recommend adding a versioned [docs/PROJECT_CONTEXT.md](docs/PROJECT_CONTEXT.md) to every repository the orchestrator works on. It is shared by the planner, implementer, and reviewer; the orchestrator reads it when present but does not scaffold it. Without it, each run has to rediscover project boundaries and design rules from the task and retrieved code. A brand-new repository can start without it and add it once there is real code to describe.

- Keep it short and durable: purpose and boundaries, layering rules, invariants, design decisions (including things deliberately not done), and where secrets and configuration come from.
- Leave out feature descriptions, UI details, and per-change notes. The code, tests, and repository retrieval already cover those.
- It is supplementary: the task, repository state, and the orchestrator's own invariants remain authoritative.
- The prompt includes at most 12,000 characters of stripped content. Longer content is truncated with a notice and a workflow warning; the file stays intact.
- The implementer is instructed to update it only when a change alters an architecture-level decision, keeping it within the cap by condensing or replacing existing content.

## Safety model

- Implementation happens in an isolated Git worktree. Your main checkout is never switched, reset, or cleaned.
- Agents are instructed never to commit, push, create pull requests, or merge. The orchestrator performs those steps itself, and never merges.
- Commits and pushes to protected branches (`main`, `master`, and `develop` by default) are refused.
- Worktrees are kept for inspection until you clean them up with `cleanup-workspaces`.
- New worktrees default to `~/.orchestrator/workspaces/<repo>/<execution-id>`, using the
  same per-user state directory as the database and the separate `qdrant` folder.
  Set `defaults.workspace.root` or `repositories.<key>.workspace.root` to override the root.
  Existing executions keep their recorded paths. `cleanup-workspaces` also lists and
  manages the previous sibling `orchestrator-workspaces/<repo>/<execution-id>` location;
  its `--dry-run` previews retention decisions. Existing worktrees are never migrated automatically.

## Troubleshooting

- **`GitHub CLI failed`:** check that `gh` is authenticated and has access to the target repository.
- **`Executable not found: codex`, `claude`, or `gh`:** install the CLI and make sure it's on your `PATH`, or select a provider you have installed.
- **Claude Code authentication fails with `bare_mode: true`:** bare mode requires an API key or supported cloud credentials. For a Claude subscription, omit `bare_mode` or set it to `false` and authenticate with Claude Code outside the orchestrator.
- **`Global configuration not found. Searched:`:** put `orchestrator.yml` in one of the locations listed under Configuration, or pass `--config` with an existing file.
- **A local Qdrant index lock error:** another process is using the index. Close it, or configure Qdrant server mode.

## Contributing

Label pull requests Feature, Improvement or Bug (or skip-changelog for internal changes) so the generated release notes group them correctly.

Install the development dependencies above, then run:

```sh
python -m pytest
python -m ruff check .
python -m mypy src/orchestrator
```

Database schema changes use versioned Alembic migrations in `src/orchestrator/migrations/versions`. From the repository root, create one with `alembic revision --autogenerate -m "describe change"` and apply it with `alembic upgrade head`. Normal CLI startup applies pending migrations automatically.

### Releasing

To publish a release, under **Actions → Release**, run the workflow on the `develop` branch and choose the `patch`, `minor`, or `major` increment.

## Changelog

Release notes live on the [GitHub Releases page](https://github.com/OrlyJunior/autonomous-development-orchestrator/releases).

## License

Released under the MIT License. See [LICENSE](LICENSE).
