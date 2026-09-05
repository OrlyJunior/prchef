from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

from orchestrator.config.project_config import ConfiguredRepository, ProjectConfig
from orchestrator.models.agent import AgentRole
from orchestrator.models.development_task import DevelopmentTask
from orchestrator.models.workflow_execution import WorkflowExecution
from orchestrator.models.workflow_status import WorkflowStatus
from orchestrator.tui.app import OrchestratorApp
from orchestrator.tui.bridge import WorkflowController, WorkflowLaunch
from orchestrator.tui.screens.execution_detail import ExecutionDetailScreen
from orchestrator.tui.screens.execution_history import ExecutionHistoryScreen
from orchestrator.tui.screens.workflow_runner import WorkflowRunnerScreen


class _Repository:
    def __init__(self) -> None:
        self.execution = WorkflowExecution(id="execution-1", status=WorkflowStatus.APPROVED)
        self.executions = [self.execution]

    def list_executions(self) -> list[WorkflowExecution]:
        return self.executions

    def get_execution(self, execution_id: str) -> WorkflowExecution:
        assert execution_id == self.execution.id
        return self.execution

    def list_steps(self, execution_id: str) -> list[Any]:
        assert execution_id == self.execution.id
        return []


class _ConfigLoader:
    def __init__(self, path: Path) -> None:
        self._path = path
        self.config = ProjectConfig.model_validate(
            {
                "path": path,
                "providers": {
                    "planner": {"type": "jules"},
                    "implementer": {
                        "type": "codex-cli",
                        "command": ["codex", "exec"],
                    },
                    "reviewer": {"type": "jules"},
                },
                "agents": {
                    "planner": {"provider": "planner"},
                    "implementer": {"provider": "implementer"},
                    "reviewer": {"provider": "reviewer"},
                },
                "task": {"provider": "github"},
            }
        )

    def list_repositories(self) -> list[ConfiguredRepository]:
        return [ConfiguredRepository("example", self._path)]

    def load(self, task: DevelopmentTask, repository: str | None = None) -> ProjectConfig:
        assert task.repository == repository == "example"
        return self.config


class _TaskProvider:
    source = "github"

    def get_task(self, task_id: str) -> DevelopmentTask:
        return DevelopmentTask(task_id, "github", "Task", "", "https://example/task")

    def list_tasks(self, *, limit: int = 50) -> list[DevelopmentTask]:
        assert limit == 50
        return [
            DevelopmentTask(
                id="12",
                source="github",
                title="Browseable issue",
                description="",
                url="https://example/issues/12",
                repository="example",
            )
        ]

    def pull_request_reference(self, task: DevelopmentTask, target_repository: str) -> str:
        return ""


class _WorkflowService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None, dict[AgentRole, str]]] = []

    def run(
        self,
        task_id: str,
        repository: str | None,
        *,
        agent_providers: dict[AgentRole, str],
        event_listener: Callable[..., None] | None,
    ) -> WorkflowExecution:
        self.calls.append((task_id, repository, agent_providers))
        del event_listener
        return WorkflowExecution(id="execution-1", status=WorkflowStatus.APPROVED)


def _controller(tmp_path: Path) -> tuple[WorkflowController, _WorkflowService, _Repository]:
    service = _WorkflowService()
    loader = _ConfigLoader(tmp_path)
    repository = _Repository()
    return (
        WorkflowController(
            workflow_repository=repository,  # type: ignore[arg-type]
            config_loader=loader,  # type: ignore[arg-type]
            client_factory=object(),  # type: ignore[arg-type]
            task_provider_factory=lambda source, repository: _TaskProvider(),
            workflow_service_factory=lambda provider: service,  # type: ignore[arg-type]
        ),
        service,
        repository,
    )


def test_controller_browses_selections_and_delegates_workflow_start(tmp_path: Path) -> None:
    controller, service, _ = _controller(tmp_path)

    assert [repository.key for repository in controller.repositories()] == ["example"]
    assert controller.task_providers("example") == ["github", "trello"]
    tasks = controller.list_tasks(repository="example", task_provider="github")
    assert [task.id for task in tasks] == ["12"]

    execution = controller.start_workflow(
        WorkflowLaunch(
            repository="example",
            task_provider="github",
            task_id="12",
            agent_providers={
                AgentRole.PLANNER: "reviewer",
                AgentRole.IMPLEMENTER: "implementer",
                AgentRole.REVIEWER: "planner",
            },
        )
    )

    assert execution.id == "execution-1"
    assert service.calls == [
        (
            "12",
            "example",
            {
                AgentRole.PLANNER: "reviewer",
                AgentRole.IMPLEMENTER: "implementer",
                AgentRole.REVIEWER: "planner",
            },
        )
    ]


def test_textual_app_navigates_to_workflow_runner(tmp_path: Path) -> None:
    controller, _, _ = _controller(tmp_path)
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test() as pilot:
            await pilot.press("n")
            assert isinstance(application.screen, WorkflowRunnerScreen)

    asyncio.run(drive())


def test_execution_history_filters_execution_without_repository(tmp_path: Path) -> None:
    controller, _, repository = _controller(tmp_path)
    repository.execution.repository = None
    repository.executions.append(
        WorkflowExecution(id="execution-2", repository="", status=WorkflowStatus.FAILED)
    )
    assert [execution.id for execution in controller.executions("execution")] == [
        "execution-2",
        "execution-1",
    ]
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test() as pilot:
            await pilot.press("h")
            assert isinstance(application.screen, ExecutionHistoryScreen)
            application.screen.refresh_history("execution")
            assert "2 persisted workflow execution(s)." in str(
                application.screen.query_one("#history-summary").renderable
            )

    asyncio.run(drive())


def test_execution_detail_renders_persisted_snapshots_and_links(tmp_path: Path) -> None:
    controller, _, repository = _controller(tmp_path)
    execution = repository.execution
    execution.repository = "example"
    execution.task_url = "https://example/tasks/12"
    execution.pr_url = "https://example/pull/34"
    execution.working_branch = "workflow/12"
    execution.validation_snapshot = {"passed": True}
    execution.review_snapshot = {"decision": "APPROVED", "findings": []}
    execution.spec_jules_session_url = "https://example/sessions/planner"
    execution.review_jules_session_url = "https://example/sessions/reviewer"
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test() as pilot:
            application.push_screen(ExecutionDetailScreen(execution.id))
            await pilot.pause()
            assert isinstance(application.screen, ExecutionDetailScreen)
            rendered = str(application.screen.query_one("#execution-content").renderable)
            assert '"validation_snapshot": {' in rendered
            assert '"review_snapshot": {' in rendered
            assert '"pr": "https://example/pull/34"' in rendered
            assert '"spec-session": "https://example/sessions/planner"' in rendered

    asyncio.run(drive())
