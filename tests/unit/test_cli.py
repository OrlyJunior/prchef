from __future__ import annotations

import json
import logging
import re
import tomllib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import inspect
from typer.testing import CliRunner

import orchestrator
from orchestrator.cli import main
from orchestrator.config.database import build_engine
from orchestrator.config.settings import Settings
from orchestrator.models import WorkflowExecution, WorkflowStage, WorkflowStatus
from orchestrator.services.workspace_cleaner import (
    CleanupWorkspace,
    WorkspaceCleanupReport,
    WorkspaceRetention,
)

runner = CliRunner()


def test_cli_version_option() -> None:
    result = runner.invoke(main.app, ["--version"])

    assert result.exit_code == 0, result.output
    assert result.stdout == f"{orchestrator.__version__}\n"


def test_cli_providers_lists_configured_provider_names_and_types(
    monkeypatch: Any, tmp_path: Path
) -> None:
    config = tmp_path / "orchestrator.yml"
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
        lambda api_key: (_ for _ in ()).throw(AssertionError("must not create clients")),
    )

    result = runner.invoke(main.app, ["providers", "--config", str(config)])

    assert result.exit_code == 0, result.output
    assert result.stdout == "vertex-gemini  gemini-vertex\ncodex  codex-cli\n"


def test_cli_providers_succeeds_without_configured_providers(tmp_path: Path) -> None:
    config = tmp_path / "orchestrator.yml"
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
    monkeypatch.setattr(main, "ClientFactory", lambda api_key: object())


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
    target_logger = logging.getLogger("orchestrator.cli.main")
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


def test_cli_aborts_before_repository_when_migration_fails(monkeypatch: Any) -> None:
    repository_accessed = False

    def fail_migration(settings: Any) -> None:
        raise main.MigrationError("Could not apply pending database migrations: database locked")

    def repository_should_not_run(settings: Any) -> object:
        nonlocal repository_accessed
        repository_accessed = True
        return object()

    monkeypatch.setattr(main, "_apply_pending_migrations", fail_migration)
    monkeypatch.setattr(main, "_repository", repository_should_not_run)

    result = runner.invoke(main.app, ["run", "--trello-card", "card-1"])

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
    config = tmp_path / "orchestrator.yml"
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


def test_cli_cleanup_workspaces_reports_dry_run_candidates(
    monkeypatch: Any, tmp_path: Path
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
            )

    class CleanupService:
        calls: list[tuple[bool, bool]] = []

        def __init__(self, *_: Any) -> None:
            pass

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
    monkeypatch.setattr(main, "WorkspaceCleanupService", CleanupService)
    monkeypatch.setattr(main, "_migrated_repository", lambda settings: object())

    result = runner.invoke(
        main.app,
        ["cleanup-workspaces", "--repository", "example", "--dry-run"],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["workspaces"][0]["retention"] == "CLEANABLE"
    assert CleanupService.calls == [(True, False)]
