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
from orchestrator.tui.screens.workflow_runner import WorkflowRunnerScreen


class _Repository:
    def __init__(self) -> None:
        self.execution = WorkflowExecution(id="execution-1", status=WorkflowStatus.APPROVED)

    def list_executions(self) -> list[WorkflowExecution]:
        return [self.execution]

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


def _controller(tmp_path: Path) -> tuple[WorkflowController, _WorkflowService]:
    service = _WorkflowService()
    loader = _ConfigLoader(tmp_path)
    return (
        WorkflowController(
            workflow_repository=_Repository(),  # type: ignore[arg-type]
            config_loader=loader,  # type: ignore[arg-type]
            client_factory=object(),  # type: ignore[arg-type]
            task_provider_factory=lambda source, repository: _TaskProvider(),
            workflow_service_factory=lambda provider: service,  # type: ignore[arg-type]
        ),
        service,
    )


def test_controller_browses_selections_and_delegates_workflow_start(tmp_path: Path) -> None:
    controller, service = _controller(tmp_path)

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
    controller, _ = _controller(tmp_path)
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test() as pilot:
            await pilot.press("n")
            assert isinstance(application.screen, WorkflowRunnerScreen)

    asyncio.run(drive())
