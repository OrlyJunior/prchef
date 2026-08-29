from __future__ import annotations

from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from orchestrator.cli import main
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
