from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from sqlalchemy import inspect
from typer.testing import CliRunner

from orchestrator.cli import main
from orchestrator.config.database import build_engine
from orchestrator.config.settings import Settings
from orchestrator.models import WorkflowExecution, WorkflowStatus

runner = CliRunner()


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


def _stub_runtime(monkeypatch: Any) -> None:
    StubWorkflowService.calls.clear()
    monkeypatch.setattr(main, "WorkflowService", StubWorkflowService)
    monkeypatch.setattr(main, "_repository", lambda settings: object())
    monkeypatch.setattr(main, "_apply_pending_migrations", lambda settings: None)
    monkeypatch.setattr(main, "ClientFactory", lambda api_key: object())


def test_cli_runs_trello_provider(monkeypatch: Any) -> None:
    _stub_runtime(monkeypatch)

    result = runner.invoke(main.app, ["run", "--trello-card", "card-1"])

    assert result.exit_code == 0, result.output
    assert StubWorkflowService.calls == [("trello", "card-1", None)]


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
