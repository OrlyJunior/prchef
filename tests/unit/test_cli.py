from __future__ import annotations

import json
import logging
import re
import tomllib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect
from typer.testing import CliRunner

import prchef
from prchef.cli import main
from prchef.clients.command_client import CommandResult
from prchef.config.database import build_engine
from prchef.config.project_config import ProjectConfigLoader
from prchef.config.settings import Settings
from prchef.models import WorkflowExecution, WorkflowStage, WorkflowStatus
from prchef.services.workspace_cleaner import (
    CleanupWorkspace,
    WorkspaceCleanupReport,
    WorkspaceRetention,
)

runner = CliRunner()


def test_show_keeps_full_execution_json(monkeypatch: pytest.MonkeyPatch) -> None:
    execution = WorkflowExecution(
        id="execution-1",
        workspace_path=r"C:\worktree",
        working_branch="workflow/12",
        commit_sha="abc123",
        pr_url="https://example/pull/34",
        spec_snapshot={"proposal": "Implement the change"},
        validation_snapshot={"passed": True},
        review_snapshot={"decision": "APPROVED"},
        spec_jules_session_url="https://example/sessions/planner",
    )
    repository = SimpleNamespace(get_execution=lambda _id: execution)
    monkeypatch.setattr(main, "_migrated_repository", lambda _settings: repository)

    result = runner.invoke(main.app, ["show", execution.id])

    assert result.exit_code == 0
    rendered = json.loads(result.stdout)
    for field in execution.__dataclass_fields__:
        assert field in rendered
    for field in (
        "workspace_path", "working_branch", "commit_sha", "pr_url", "spec_snapshot",
        "validation_snapshot", "review_snapshot", "spec_jules_session_url",
    ):
        assert rendered[field] == getattr(execution, field)


def test_cli_and_tui_select_only_the_matching_planner_app(
    monkeypatch: Any, tmp_path: Path
) -> None:
    for name in ("first", "second"):
        (tmp_path / name / ".git").mkdir(parents=True)
    config_file = tmp_path / "prchef.yml"
    config_file.write_text(
        """defaults:
  roles: {spec: codex, review: codex}
repositories:
  first:
    path: first
    integrations:
      github:
        planner_app:
          app_id: 1
          installation_id: 11
          private_key_env: FIRST_KEY
  second:
    path: second
    integrations:
      github:
        planner_app:
          app_id: 2
          installation_id: 22
          private_key_env: SECOND_KEY
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        main.GitClient, "github_repository",
        lambda self: f"owner/{self._path.name}",
    )
    loader = ProjectConfigLoader(config_file)
    settings = Settings()

    _, cli_provider = main._task_provider(
        settings=settings, config_loader=loader, trello_card=None,
        github_issue=42, task_id=None, repository="owner/second",
    )
    assert cli_provider._client._planner_app.app_id == 2
    _, unmatched_provider = main._task_provider(
        settings=settings, config_loader=loader, trello_card=None,
        github_issue=42, task_id=None, repository="owner/unregistered",
    )
    assert unmatched_provider._client._planner_app is None
    tui_provider = main._tui_task_provider(settings, loader, "github", "first")
    assert tui_provider._client._planner_app.app_id == 1
    debug_provider = main._debug_task_provider(
        settings, "github", "owner/second", loader
    )
    assert debug_provider._client._planner_app.app_id == 2


@pytest.mark.parametrize("second_identity", ["same", "different", "unconfigured"])
def test_cli_slug_with_duplicate_origins_uses_only_a_shared_identity(
    monkeypatch: Any, tmp_path: Path, second_identity: str
) -> None:
    for name in ("first", "second"):
        (tmp_path / name / ".git").mkdir(parents=True)
    config_file = tmp_path / "prchef.yml"
    second_app = {
        "same": "        planner_app:\n          app_id: 1\n"
        "          installation_id: 11\n          private_key_env: FIRST_KEY\n",
        "different": "        planner_app:\n          app_id: 2\n"
        "          installation_id: 22\n          private_key_env: SECOND_KEY\n",
        "unconfigured": "        planner_app: {}\n",
    }[second_identity]
    config_file.write_text(
        "defaults:\n  roles: {spec: codex, review: codex}\nrepositories:\n"
        "  first:\n    path: first\n    integrations:\n      github:\n"
        "        planner_app:\n          app_id: 1\n"
        "          installation_id: 11\n          private_key_env: FIRST_KEY\n"
        "  second:\n    path: second\n    integrations:\n      github:\n"
        + second_app,
        encoding="utf-8",
    )
    monkeypatch.setattr(main.GitClient, "github_repository", lambda self: "owner/shared")
    loader = ProjectConfigLoader(config_file)

    if second_identity != "same":
        with pytest.raises(main.ConfigurationError, match="different Planner App identities"):
            main._planner_app_for_slug(loader, "owner/shared")
        with pytest.raises(main.typer.BadParameter, match="first, second"):
            main._task_provider(
                settings=Settings(), config_loader=loader, trello_card=None,
                github_issue=42, task_id=None, repository="owner/shared",
            )
        with pytest.raises(main.ConfigurationError, match="first, second"):
            main._debug_task_provider(Settings(), "github", "owner/shared", loader)
    else:
        planner_app = main._planner_app_for_slug(loader, "owner/shared")
        assert planner_app is not None
        assert planner_app.app_id == 1


def test_cli_slug_with_duplicate_origins_and_no_planner_app_keeps_gh_fallback(
    monkeypatch: Any, tmp_path: Path
) -> None:
    for name in ("first", "second"):
        (tmp_path / name / ".git").mkdir(parents=True)
    config_file = tmp_path / "prchef.yml"
    config_file.write_text(
        "defaults:\n  roles: {spec: codex, review: codex}\nrepositories:\n"
        "  first:\n    path: first\n"
        "  second:\n    path: second\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        main.GitClient, "github_repository",
        lambda self: (_ for _ in ()).throw(AssertionError("must not inspect Git")),
    )
    assert main._planner_app_for_slug(ProjectConfigLoader(config_file), "owner/shared") is None


@pytest.mark.parametrize("explicit_config", [False, True])
def test_debug_task_loading_without_global_config_uses_gh(
    monkeypatch: Any, tmp_path: Path, explicit_config: bool
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("PRCHEF_CONFIG", raising=False)
    monkeypatch.delenv("PRCHEF_CONFIG_PATH", raising=False)
    calls: list[list[str]] = []

    def run(self: Any, command: list[str], **kwargs: Any) -> CommandResult:
        calls.append(command)
        if command[1] == "api":
            stdout = "[[]]"
        else:
            stdout = json.dumps({
                "number": 42, "title": "Issue title", "body": "Issue body",
                "url": "https://github.com/owner/repo/issues/42", "labels": [],
                "state": "OPEN", "author": {"login": "octocat"},
                "milestone": None,
            })
        return CommandResult(
            command=command,
            exit_code=0,
            stdout=stdout,
            stderr="",
            duration_seconds=0.0,
        )

    monkeypatch.setattr(main.CommandClient, "run", run)
    arguments = [
        "debug", "--stage", "task-loading", "--github-issue", "42",
        "--repository", "owner/repo",
    ]
    if explicit_config:
        arguments.extend(["--config", str(tmp_path / "missing.yml")])
    result = runner.invoke(main.app, arguments)

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["result"]["task"]["title"] == "Issue title"
    assert calls == [[
        "gh", "issue", "view", "42", "--repo", "owner/repo", "--json",
        "number,title,body,url,labels,state,author,milestone",
    ], [
        "gh", "api", "--paginate", "--slurp",
        "repos/owner/repo/issues/42/comments?per_page=100",
    ]]


def test_invalid_existing_config_still_reports_error_for_planner_lookup(tmp_path: Path) -> None:
    config_file = tmp_path / "prchef.yml"
    config_file.write_text("defaults: [", encoding="utf-8")
    loader = ProjectConfigLoader(config_file)
    with pytest.raises(main.ConfigurationError):
        main._planner_app_for_slug(loader, "owner/repo")


def test_cli_version_option() -> None:
    result = runner.invoke(main.app, ["--version"])

    assert result.exit_code == 0, result.output
    assert result.stdout == f"{prchef.__version__}\n"


def test_cli_providers_lists_configured_provider_names_and_types(
    monkeypatch: Any, tmp_path: Path
) -> None:
    config = tmp_path / "prchef.yml"
    config.write_text(
        """defaults:
  agents:
    planner:
      provider: vertex-gemini
    implementer:
      provider: codex
    reviewer:
      provider: vertex-gemini
  providers:
    vertex-gemini:
      type: gemini-vertex
      project: example-project
      model: gemini-test-model
    codex:
      type: codex-cli
      command: [codex, exec, -]
""",
        encoding="utf-8",
    )

    monkeypatch.setattr(
        main,
        "_migrated_repository",
        lambda settings: (_ for _ in ()).throw(AssertionError("must not access persistence")),
    )
    monkeypatch.setattr(
        main,
        "ClientFactory",
        lambda: (_ for _ in ()).throw(AssertionError("must not create clients")),
    )

    result = runner.invoke(main.app, ["providers", "--config", str(config)])

    assert result.exit_code == 0, result.output
    assert result.stdout == "vertex-gemini  litellm\ncodex  codex-cli\n"


def test_cli_providers_succeeds_without_configured_providers(tmp_path: Path) -> None:
    config = tmp_path / "prchef.yml"
    config.write_text(
        """defaults:
  agents:
    planner:
      provider: planner
    implementer:
      provider: implementer
    reviewer:
      provider: reviewer
  providers: {}
""",
        encoding="utf-8",
    )

    result = runner.invoke(main.app, ["providers", "--config", str(config)])

    assert result.exit_code == 0, result.output
    assert result.stdout == ""


def test_cli_providers_uses_global_config_from_an_isolated_cwd(
    monkeypatch: Any, tmp_path: Path
) -> None:
    for variable in ("PRCHEF_CONFIG", "PRCHEF_CONFIG_FILE"):
        monkeypatch.delenv(variable, raising=False)
    config = tmp_path / "prchef.yml"
    config.write_text("defaults:\n  roles: {spec: codex, review: codex}\n", encoding="utf-8")
    isolated_cwd = tmp_path / "unrelated" / "directory"
    isolated_cwd.mkdir(parents=True)
    monkeypatch.chdir(isolated_cwd)
    monkeypatch.setenv("PRCHEF_CONFIG_PATH", str(config))

    result = runner.invoke(main.app, ["providers"])

    assert result.exit_code == 0, result.output
    assert result.stdout == "codex  codex-cli\n"


@pytest.mark.parametrize("command", ["providers", "tui"])
def test_cli_surfaces_rejected_jules_provider_before_startup(
    tmp_path: Path, command: str
) -> None:
    config = tmp_path / "prchef.yml"
    config.write_text(
        """defaults:
  agents:
    planner: {provider: codex}
    implementer: {provider: codex}
    reviewer: {provider: codex}
  providers:
    codex: {type: codex-cli, command: [codex, exec, -]}
    retired: {type: jules}
""",
        encoding="utf-8",
    )

    result = runner.invoke(main.app, [command, "--config", str(config)])

    assert result.exit_code == 2
    assert "retired" in result.output
    assert "codex-cli" in result.output


def test_cli_help_starts_from_an_arbitrary_directory_without_configuration(
    monkeypatch: Any, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(main.app, ["--help"])

    assert result.exit_code == 0, result.output
    assert "prchef workflow" in result.output


def test_cli_providers_reports_config_discovery_failure(
    monkeypatch: Any, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(main, "ProjectConfigLoader", lambda _: (_ for _ in ()).throw(
        main.ConfigurationError("Global configuration not found. Searched: test paths")
    ))

    result = runner.invoke(main.app, ["providers"])

    assert result.exit_code == 2
    assert "Global configuration not found. Searched: test paths" in result.output


def test_cli_run_reports_missing_global_configuration_before_accessing_persistence(
    monkeypatch: Any,
) -> None:
    persistence_accessed = False

    def missing_config(_: Path | None) -> object:
        raise main.ConfigurationError("Global configuration not found. Searched: test paths")

    def persistence_should_not_run(_: Settings) -> object:
        nonlocal persistence_accessed
        persistence_accessed = True
        return object()

    monkeypatch.setattr(main, "ProjectConfigLoader", missing_config)
    monkeypatch.setattr(main, "_migrated_repository", persistence_should_not_run)

    result = runner.invoke(main.app, ["run", "--trello-card", "card-1"])

    assert result.exit_code == 2
    assert "Global configuration not found. Searched: test paths" in result.output
    assert not persistence_accessed


class StubWorkflowService:
    calls: list[tuple[str, str, str | None]] = []

    def __init__(self, *, task_provider: Any, **kwargs: Any) -> None:
        self._source = str(task_provider.source)

    def run(self, task_id: str, repository: str | None) -> WorkflowExecution:
        self.calls.append((self._source, task_id, repository))
        return WorkflowExecution(
            id="execution-1",
            task_source=self._source,
            task_id=task_id,
            status=WorkflowStatus.APPROVED,
        )


class StubDebugService:
    calls: list[tuple[str, str, bool]] = []
    inputs: list[Any] = []

    def __init__(self, **kwargs: Any) -> None:
        pass

    def execute(
        self,
        execution: WorkflowExecution | None,
        stage: Any,
        *,
        inputs: Any,
        dry_run: bool,
    ) -> dict[str, Any]:
        execution_id = execution.id if execution is not None else ""
        self.calls.append((execution_id, stage.value, dry_run))
        self.inputs.append(inputs)
        return {
            "execution_id": execution.id if execution is not None else None,
            "stage": stage.value,
            "dry_run": dry_run,
            "execution_mutated": False,
        }


def _stub_runtime(monkeypatch: Any) -> None:
    StubWorkflowService.calls.clear()
    monkeypatch.setattr(main, "WorkflowService", StubWorkflowService)
    monkeypatch.setattr(main, "_repository", lambda settings: object())
    monkeypatch.setattr(main, "_apply_pending_migrations", lambda settings: None)
    monkeypatch.setattr(
        main,
        "_config_loader",
        lambda path: SimpleNamespace(
            # Startup reads the configured provider ceilings once to size the
            # process-scoped limiter and worker pool.
            load_defaults=lambda: SimpleNamespace(providers={}),
            default_task_provider=lambda: "github",
            list_repositories=lambda: [],
            has_planner_github_app=lambda: False,
        ),
    )
    monkeypatch.setattr(main, "ClientFactory", lambda: object())


def test_cli_debug_runs_only_selected_stage_and_supports_dry_run(
    monkeypatch: Any,
) -> None:
    _stub_runtime(monkeypatch)
    StubDebugService.calls.clear()
    StubDebugService.inputs.clear()

    class Repository:
        def get_execution(self, execution_id: str) -> WorkflowExecution:
            return WorkflowExecution(id=execution_id)

    monkeypatch.setattr(main, "_repository", lambda settings: Repository())
    monkeypatch.setattr(main, "DebugService", StubDebugService)

    result = runner.invoke(
        main.app,
        [
            "debug",
            "--execution-id",
            "execution-1",
            "--stage",
            "validation",
            "--dry-run",
            "--verbose",
        ],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "execution_id": "execution-1",
        "stage": "validation",
        "dry_run": True,
        "execution_mutated": False,
    }
    assert StubDebugService.calls == [("execution-1", "validation", True)]


def test_cli_debug_runs_without_loading_execution(monkeypatch: Any) -> None:
    _stub_runtime(monkeypatch)
    StubDebugService.calls.clear()
    StubDebugService.inputs.clear()
    repository_accessed = False

    def repository_should_not_run(settings: Any) -> object:
        nonlocal repository_accessed
        repository_accessed = True
        raise AssertionError("clean debug must not access workflow persistence")

    monkeypatch.setattr(main, "_repository", repository_should_not_run)
    monkeypatch.setattr(main, "DebugService", StubDebugService)

    result = runner.invoke(
        main.app,
        ["debug", "--stage", "validation", "--repository", "owner/repository"],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["execution_id"] is None
    assert not repository_accessed
    assert StubDebugService.calls == [("", "validation", False)]


def test_cli_debug_explicit_inputs_override_historical_source(monkeypatch: Any) -> None:
    _stub_runtime(monkeypatch)
    StubDebugService.calls.clear()
    StubDebugService.inputs.clear()

    class Repository:
        def get_execution(self, execution_id: str) -> WorkflowExecution:
            return WorkflowExecution(id=execution_id, repository="historical")

    monkeypatch.setattr(main, "_repository", lambda settings: Repository())
    monkeypatch.setattr(main, "DebugService", StubDebugService)

    result = runner.invoke(
        main.app,
        [
            "debug",
            "--from-execution",
            "historical-1",
            "--stage",
            "implementation",
            "--repository",
            "explicit",
            "--prompt",
            "Use explicit input",
        ],
    )

    assert result.exit_code == 0, result.output
    assert StubDebugService.inputs[0].repository == "explicit"
    assert StubDebugService.inputs[0].prompt == "Use explicit input"


def test_cli_runs_trello_provider(monkeypatch: Any) -> None:
    _stub_runtime(monkeypatch)

    result = runner.invoke(main.app, ["run", "--trello-card", "card-1"])

    assert result.exit_code == 0, result.output
    assert StubWorkflowService.calls == [("trello", "card-1", None)]


def test_cli_reports_rejected_fresh_run_without_traceback(monkeypatch: Any) -> None:
    _stub_runtime(monkeypatch)

    def reject(self: StubWorkflowService, task_id: str, repository: str | None):
        raise ValueError(f"Task {task_id} already has a PENDING execution")

    monkeypatch.setattr(StubWorkflowService, "run", reject)
    result = runner.invoke(main.app, ["run", "--trello-card", "card-1"])

    assert result.exit_code == 2
    assert "already has a PENDING execution" in result.output


def test_cli_respond_uses_same_execution_and_service_logic(monkeypatch: Any) -> None:
    execution = WorkflowExecution(
        id="execution-human",
        task_source="trello",
        task_id="card-1",
        status=WorkflowStatus.NEEDS_HUMAN,
        task_snapshot={
            "id": "card-1",
            "source": "trello",
            "title": "Task",
            "description": "",
            "url": "https://trello.example/card-1",
            "repository": "example",
            "metadata": {},
        },
    )

    class Repository:
        def get_execution(self, execution_id: str) -> WorkflowExecution:
            assert execution_id == execution.id
            return execution

    calls: list[tuple[str, str]] = []

    class RespondService:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def respond_and_resume(self, execution_id: str, message: str) -> WorkflowExecution:
            calls.append((execution_id, message))
            return WorkflowExecution(id=execution_id, status=WorkflowStatus.APPROVED)

    monkeypatch.setattr(main, "_migrated_repository", lambda settings: Repository())
    monkeypatch.setattr(
        main,
        "_config_loader",
        lambda path: SimpleNamespace(load_defaults=lambda: SimpleNamespace(providers={})),
    )
    monkeypatch.setattr(main, "WorkflowService", RespondService)
    monkeypatch.setattr(main, "ClientFactory", lambda: object())

    result = runner.invoke(
        main.app,
        ["respond", "execution-human", "--message", "Use the configured default."],
    )

    assert result.exit_code == 0, result.output
    assert calls == [("execution-human", "Use the configured default.")]


def test_cli_launches_textual_tui_after_migrations(monkeypatch: Any, tmp_path: Path) -> None:
    _stub_runtime(monkeypatch)
    launched = False
    config = tmp_path / "prchef.yml"
    config.write_text("defaults:\n  roles: {spec: codex, review: codex}\n", encoding="utf-8")
    monkeypatch.setattr(main, "_config_loader", lambda path: main.ProjectConfigLoader(path))

    class TuiApp:
        def __init__(self, controller: Any) -> None:
            assert controller is not None

        def run(self) -> None:
            nonlocal launched
            launched = True

    monkeypatch.setattr(main, "PrchefApp", TuiApp)

    result = runner.invoke(main.app, ["tui", "--config", str(config)])

    assert result.exit_code == 0, result.output
    assert launched


def test_cli_keeps_json_on_stdout_and_emits_final_summary_on_stderr(monkeypatch: Any) -> None:
    _stub_runtime(monkeypatch)

    result = runner.invoke(main.app, ["run", "--trello-card", "card-1"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["status"] == "APPROVED"
    assert "[workflow] Finished" in result.stderr
    assert "Status: APPROVED" in result.stderr
    assert re.search(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z \[workflow\] Finished",
        result.stderr,
    )


@pytest.mark.parametrize(
    "status", [WorkflowStatus.APPROVED, WorkflowStatus.NEEDS_HUMAN, WorkflowStatus.FAILED]
)
def test_final_summary_is_emitted_for_all_terminal_statuses(
    status: WorkflowStatus, caplog: Any
) -> None:
    target_logger = logging.getLogger("prchef.cli.main")
    target_logger.addHandler(caplog.handler)
    previous_propagation = target_logger.propagate
    target_logger.propagate = False
    try:
        with caplog.at_level(logging.INFO, logger=target_logger.name):
            main._print_final_summary(
                WorkflowExecution(
                    id="execution-1",
                    status=status,
                    current_stage=WorkflowStage.GENERATE_SPEC,
                    error_message=(
                        "JULES_API_KEY=should-not-appear"
                        if status is WorkflowStatus.FAILED
                        else None
                    ),
                )
            )
    finally:
        target_logger.removeHandler(caplog.handler)
        target_logger.propagate = previous_propagation

    assert "[workflow] Finished" in caplog.text
    assert f"Status: {status}" in caplog.text
    assert "should-not-appear" not in caplog.text


def test_cli_runs_github_issue_provider(monkeypatch: Any) -> None:
    _stub_runtime(monkeypatch)

    result = runner.invoke(
        main.app,
        ["run", "--github-issue", "42", "--repository", "owner/api"],
    )

    assert result.exit_code == 0, result.output
    assert StubWorkflowService.calls == [("github", "42", "owner/api")]


def test_cli_aborts_before_repository_when_migration_fails(
    monkeypatch: Any, tmp_path: Path
) -> None:
    repository_accessed = False
    config = tmp_path / "prchef.yml"
    config.write_text("defaults:\n  roles: {spec: codex, review: codex}\n", encoding="utf-8")

    def fail_migration(settings: Any) -> None:
        raise main.MigrationError("Could not apply pending database migrations: database locked")

    def repository_should_not_run(settings: Any) -> object:
        nonlocal repository_accessed
        repository_accessed = True
        return object()

    monkeypatch.setattr(main, "_apply_pending_migrations", fail_migration)
    monkeypatch.setattr(main, "_repository", repository_should_not_run)

    result = runner.invoke(
        main.app, ["run", "--trello-card", "card-1", "--config", str(config)]
    )

    assert result.exit_code == 2
    assert "Could not apply pending database migrations" in result.output
    assert not repository_accessed


def test_pending_migrations_are_applied_to_configured_database(tmp_path: Path) -> None:
    database_url = f"sqlite:///{(tmp_path / 'workflow.db').as_posix()}"

    main._apply_pending_migrations(Settings(database_url=database_url))
    main._apply_pending_migrations(Settings(database_url=database_url))

    engine = build_engine(database_url)
    assert "workflow_executions" in inspect(engine).get_table_names()
    engine.dispose()


def test_pending_migrations_accept_percent_in_database_url(tmp_path: Path) -> None:
    database_url = f"sqlite:///{(tmp_path / 'workflow%25.db').as_posix()}"

    main._apply_pending_migrations(Settings(database_url=database_url))

    engine = build_engine(database_url)
    assert "workflow_executions" in inspect(engine).get_table_names()
    engine.dispose()


def test_manual_alembic_upgrade_uses_global_persistence_configuration_outside_cwd(
    monkeypatch: Any, tmp_path: Path
) -> None:
    config = tmp_path / "config" / "prchef.yml"
    config.parent.mkdir()
    database = tmp_path / "state" / "workflow.db"
    config.write_text(
        "defaults:\n  persistence:\n    database_path: ../state/workflow.db\n",
        encoding="utf-8",
    )
    arbitrary_cwd = tmp_path / "arbitrary-working-directory"
    arbitrary_cwd.mkdir()
    monkeypatch.chdir(arbitrary_cwd)
    monkeypatch.setenv("PRCHEF_CONFIG", str(config))
    monkeypatch.delenv("PRCHEF_DATABASE_URL", raising=False)
    monkeypatch.delenv("PRCHEF_DATABASE_PATH", raising=False)

    project_root = Path(__file__).resolve().parents[2]
    alembic_config = Config(str(project_root / "alembic.ini"))
    command.upgrade(alembic_config, "head")

    engine = build_engine(f"sqlite:///{database.as_posix()}")
    assert "workflow_executions" in inspect(engine).get_table_names()
    engine.dispose()


def test_pytest_uses_a_project_local_temporary_directory() -> None:
    project_root = Path(__file__).resolve().parents[2]
    pyproject = tomllib.loads((project_root / "pyproject.toml").read_text(encoding="utf-8"))

    assert "--basetemp=.pytest-tmp/run" in pyproject["tool"]["pytest"]["ini_options"]["addopts"]


def test_cli_rejects_multiple_task_origins(monkeypatch: Any) -> None:
    _stub_runtime(monkeypatch)

    result = runner.invoke(
        main.app,
        [
            "run",
            "--trello-card",
            "card-1",
            "--github-issue",
            "42",
            "--repository",
            "owner/api",
        ],
    )

    assert result.exit_code == 2
    assert "exactly one" in result.output


def test_cli_uses_default_provider_from_configuration(
    monkeypatch: Any, tmp_path: Path
) -> None:
    _stub_runtime(monkeypatch)
    config = tmp_path / "prchef.yml"
    config.write_text("task:\n  provider: github\n", encoding="utf-8")

    result = runner.invoke(
        main.app,
        [
            "run",
            "--task-id",
            "42",
            "--repository",
            "owner/api",
            "--config",
            str(config),
        ],
    )

    assert result.exit_code == 0, result.output
    assert StubWorkflowService.calls == [("github", "42", "owner/api")]


@pytest.mark.parametrize("context_strategy", ["heuristic", "qdrant"])
def test_cli_cleanup_workspaces_reports_dry_run_candidates(
    monkeypatch: Any, tmp_path: Path, context_strategy: str
) -> None:
    control = tmp_path / "control"
    control.mkdir()

    class ConfigLoader:
        def __init__(self, _: Path) -> None:
            pass

        def load(self, task: Any, repository: str) -> Any:
            assert task.repository == repository == "example"
            return SimpleNamespace(
                path=control,
                workspace=SimpleNamespace(root=tmp_path / "workspaces"),
                repository_context=SimpleNamespace(strategy=context_strategy),
                configured_repository_key="example",
            )

    class IndexStrategy:
        created: list[tuple[Any, str, Path]] = []

        def __init__(self, settings: Any, key: str, root: Path) -> None:
            self.created.append((settings, key, root))

        def delete_worktree_points(self, path: Path) -> None:
            raise AssertionError(f"Dry run deleted points for {path}")

    class CleanupService:
        calls: list[tuple[bool, bool]] = []
        delete_callback: Any = None

        def __init__(self, *_: Any, **kwargs: Any) -> None:
            type(self).delete_callback = kwargs["delete_index_points"]

        def cleanup(self, *, dry_run: bool, include_failed: bool) -> WorkspaceCleanupReport:
            self.calls.append((dry_run, include_failed))
            return WorkspaceCleanupReport(
                workspaces=[
                    CleanupWorkspace(
                        path=tmp_path / "workspaces" / "control" / "execution-1",
                        execution_id="execution-1",
                        status=WorkflowStatus.APPROVED,
                        retention=WorkspaceRetention.CLEANABLE,
                        eligible=True,
                        exists=True,
                    )
                ],
                dry_run=dry_run,
            )

    monkeypatch.setattr(main, "ProjectConfigLoader", ConfigLoader)
    monkeypatch.setattr(main, "QdrantRepositoryContextStrategy", IndexStrategy)
    monkeypatch.setattr(main, "WorkspaceCleanupService", CleanupService)
    monkeypatch.setattr(main, "_migrated_repository", lambda settings: object())

    result = runner.invoke(
        main.app,
        ["cleanup-workspaces", "--repository", "example", "--dry-run"],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["workspaces"][0]["retention"] == "CLEANABLE"
    assert CleanupService.calls == [(True, False)]
    assert bool(IndexStrategy.created) == (context_strategy == "qdrant")
    assert (CleanupService.delete_callback is not None) == (context_strategy == "qdrant")
