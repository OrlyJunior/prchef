from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import EllipsisType
from typing import Any

import pytest
from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import (
    Button,
    Checkbox,
    Input,
    Log,
    Select,
    SelectionList,
    Static,
    TextArea,
)

from orchestrator.config.project_config import (
    AgentPresetConfig,
    AgentProviderSettings,
    ConfiguredRepository,
    GitHubAppSettings,
    ProjectConfig,
    RepositoryConfiguration,
    RepositoryContextSettings,
)
from orchestrator.models.agent import AgentRole
from orchestrator.models.development_task import DevelopmentTask
from orchestrator.models.execution_diagnostic import DiagnosticCategory, ExecutionDiagnostic
from orchestrator.models.human_interaction import HumanInteraction
from orchestrator.models.repository_context import IndexLifecycleState, RepositoryIndexStatus
from orchestrator.models.workflow_batch import WorkflowBatch, WorkflowBatchItem
from orchestrator.models.workflow_event import WorkflowEvent, WorkflowEventKind
from orchestrator.models.workflow_execution import WorkflowExecution
from orchestrator.models.workflow_status import StepStatus, WorkflowStage, WorkflowStatus
from orchestrator.models.workflow_step import WorkflowStep
from orchestrator.tui.app import OrchestratorApp
from orchestrator.tui.batch_progress import BatchRowView, persisted_row
from orchestrator.tui.bridge import (
    WorkflowBatchLaunch,
    WorkflowController,
    WorkflowLaunch,
    _BatchRun,
)
from orchestrator.tui.diagnostics import format_diagnostics_for_export
from orchestrator.tui.screens.agent_instructions import AgentInstructionsScreen
from orchestrator.tui.screens.agent_presets import AgentPresetsScreen
from orchestrator.tui.screens.batch_detail import BatchDetailScreen
from orchestrator.tui.screens.execution_detail import ExecutionDetailScreen
from orchestrator.tui.screens.execution_history import (
    ExecutionHistoryScreen,
    execution_status_label,
)
from orchestrator.tui.screens.github_app_settings import GitHubAppSettingsScreen
from orchestrator.tui.screens.repository_settings import (
    DeleteRepositoryConfirmation,
    RepositorySettingsScreen,
)
from orchestrator.tui.screens.task_selector import (
    TaskRow,
    TaskSelector,
    TaskTableHeader,
)
from orchestrator.tui.screens.workflow_runner import (
    FreshRunConfirmation,
    StopWorkflowConfirmation,
    WorkflowRunnerScreen,
)
from orchestrator.tui.widgets import (
    BatchItemRow,
    BatchProgressView,
    OrchestratorSelect,
    RepositorySelect,
)
from tests.unit.tui_sync import settle_tui, wait_for_tui


def test_single_task_context_warning_stays_in_timeline_and_detailed_logs(tmp_path: Path) -> None:
    controller, _, _ = _controller(tmp_path)
    application = OrchestratorApp(controller)
    payload = {
        "kind": "PROJECT_CONTEXT_TRUNCATED",
        "severity": "warning",
        "path": str(tmp_path / "docs" / "PROJECT_CONTEXT.md"),
        "actual_chars": 12_001,
        "cap_chars": 12_000,
    }

    async def drive() -> None:
        async with application.run_test() as pilot:
            application.push_screen(WorkflowRunnerScreen())
            await settle_tui(pilot)
            screen = application.screen
            assert isinstance(screen, WorkflowRunnerScreen)
            screen._render_workflow_event(WorkflowEvent(
                execution_id="execution-1", kind=WorkflowEventKind.DIAGNOSTIC,
                category=DiagnosticCategory.WORKFLOW_EVENT.value,
                stage=WorkflowStage.GENERATE_SPEC, payload=payload,
            ))
            assert "12001 / 12000 characters" in screen._event_lines[-1]
            assert "12001 / 12000 characters" in str(
                screen.query_one("#workflow-events", Static).renderable
            )
            exported = format_diagnostics_for_export(screen._live_diagnostics)
            assert "PROJECT_CONTEXT_TRUNCATED" in exported
            assert json.dumps(payload["path"]) in exported

    asyncio.run(drive())


@pytest.mark.parametrize("batch_run", [False, True])
def test_embedding_progress_keeps_tui_controls_responsive(
    tmp_path: Path, batch_run: bool
) -> None:
    controller, _, _ = _controller(tmp_path)
    application = OrchestratorApp(controller)
    entered = threading.Event()
    release = threading.Event()

    async def drive() -> None:
        worker: threading.Thread | None = None
        try:
            async with application.run_test(size=(80, 20)) as pilot:
                application.push_screen(WorkflowRunnerScreen())
                await settle_tui(pilot)
                screen = application.screen
                assert isinstance(screen, WorkflowRunnerScreen)
                if batch_run:
                    batch = WorkflowBatch(
                        id="batch-progress", repository="example", task_source="github",
                        items=(WorkflowBatchItem(0, "12", "Embedding task"),),
                    )
                    screen._batch_running = True
                    screen._batch_created(batch, _batch_launch("12"))

                class BlockedEmbedding:
                    def embed_documents(self) -> None:
                        execution_id = "execution-progress"
                        screen._on_workflow_event(WorkflowEvent(
                            execution_id, WorkflowEventKind.EXECUTION_CREATED,
                            status=WorkflowStatus.PENDING,
                            payload={"batch_position": 0} if batch_run else None,
                        ))
                        screen._on_workflow_event(WorkflowEvent(
                            execution_id, WorkflowEventKind.STATUS_CHANGED,
                            status=WorkflowStatus.RUNNING,
                        ))
                        screen._on_workflow_event(WorkflowEvent(
                            execution_id, WorkflowEventKind.DIAGNOSTIC,
                            stage=WorkflowStage.GENERATE_SPEC,
                            category=DiagnosticCategory.WORKFLOW_EVENT.value,
                            payload={
                                "kind": "REPOSITORY_INDEX_PROGRESS",
                                "message": "Embedding chunks 0/64",
                            },
                        ))
                        entered.set()
                        assert release.wait(timeout=15)

                worker = threading.Thread(target=BlockedEmbedding().embed_documents, daemon=True)
                worker.start()
                assert await asyncio.to_thread(entered.wait, 2)
                await wait_for_tui(
                    pilot,
                    lambda: (
                        bool(screen._batch_progress.rows()[0].timeline)
                        and "Embedding chunks 0/64"
                        in screen._batch_progress.rows()[0].timeline[-1]
                        if batch_run and screen._batch_progress is not None
                        else bool(screen._event_lines)
                        and "Embedding chunks 0/64" in screen._event_lines[-1]
                    ),
                    description="semantic index progress",
                )
                content = screen.query_one("#workflow-runner", VerticalScroll)
                content.scroll_end(animate=False)
                await wait_for_tui(
                    pilot, lambda: content.scroll_y > 0,
                    description="workflow scrolling during embedding",
                )
                screen.query_one("#stop-workflow", Button).press()
                await wait_for_tui(
                    pilot, lambda: isinstance(application.screen, StopWorkflowConfirmation),
                    description="stop confirmation during embedding",
                )
                application.screen.query_one("#dismiss-stop-workflow", Button).press()
                await wait_for_tui(
                    pilot, lambda: application.screen is screen,
                    description="stop confirmation dismissal",
                )
                await pilot.press("r")
                await wait_for_tui(
                    pilot, lambda: isinstance(application.screen, RepositorySettingsScreen),
                    description="repository navigation during embedding",
                )
                assert worker.is_alive()
        finally:
            release.set()
            if worker is not None:
                worker.join(timeout=5)
                assert not worker.is_alive()

    asyncio.run(drive())


@pytest.mark.parametrize("stage", [WorkflowStage.GENERATE_SPEC, WorkflowStage.REVIEW])
def test_single_task_timeline_revises_human_blocked_attempt_and_keeps_retry(
    tmp_path: Path, stage: WorkflowStage
) -> None:
    controller, _, _ = _controller(tmp_path)
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test() as pilot:
            application.push_screen(WorkflowRunnerScreen())
            await settle_tui(pilot)
            screen = application.screen
            assert isinstance(screen, WorkflowRunnerScreen)
            execution_id = "execution-1"
            screen._render_workflow_event(WorkflowEvent(
                execution_id, WorkflowEventKind.STAGE_STARTED, stage=stage,
                step_status=StepStatus.RUNNING
            ))
            screen._render_workflow_event(WorkflowEvent(
                execution_id, WorkflowEventKind.STAGE_FINISHED, stage=stage,
                step_status=StepStatus.SUCCEEDED
            ))
            screen._render_workflow_event(WorkflowEvent(
                execution_id, WorkflowEventKind.STATUS_CHANGED, stage=stage,
                status=WorkflowStatus.NEEDS_HUMAN
            ))
            waiting = (
                "! Generating specification — waiting for human input"
                if stage is WorkflowStage.GENERATE_SPEC
                else "! Reviewing pull request — waiting for human input"
            )
            assert screen._event_lines == [waiting]
            assert waiting in str(screen.query_one("#workflow-events", Static).renderable)

            screen._render_workflow_event(WorkflowEvent(
                execution_id, WorkflowEventKind.STAGE_STARTED, stage=stage,
                step_status=StepStatus.RUNNING
            ))
            screen._render_workflow_event(WorkflowEvent(
                execution_id, WorkflowEventKind.STAGE_FINISHED, stage=stage,
                step_status=StepStatus.SUCCEEDED
            ))
            assert screen._event_lines == [
                waiting,
                "✓ Specification generated" if stage is WorkflowStage.GENERATE_SPEC
                else "✓ Review completed",
            ]

    asyncio.run(drive())


class _ScrollableSelectApp(App[None]):
    """Small viewport used to exercise the Select/VerticalScroll interaction."""

    CSS = """
    VerticalScroll {
        height: 8;
    }

    #select-after {
        height: 8;
    }

    #select-before {
        height: 5;
    }
    """

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="select-viewport"):
            yield Static("", id="select-before")
            yield OrchestratorSelect(
                [("One", "one"), ("Two", "two"), ("Three", "three")],
                prompt="Choose a value",
                id="viewport-select",
            )
            yield Static("", id="select-after")


class _RepositorySelectApp(App[None]):
    def __init__(self, selector: RepositorySelect) -> None:
        super().__init__()
        self.selector = selector

    def compose(self) -> ComposeResult:
        yield self.selector


class _Repository:
    def __init__(self) -> None:
        self.execution = WorkflowExecution(id="execution-1", status=WorkflowStatus.APPROVED)
        self.executions = [self.execution]
        self.diagnostics: list[ExecutionDiagnostic] = []
        self.cancellation_requests: list[str] = []
        self.batch_rows: list[WorkflowBatch] = []

    def list_executions(self) -> list[WorkflowExecution]:
        return self.executions

    def get_execution(self, execution_id: str) -> WorkflowExecution:
        for execution in self.executions:
            if execution.id == execution_id:
                return execution
        raise KeyError(execution_id)

    def execution_statuses(self, execution_ids: list[str]) -> dict[str, WorkflowStatus]:
        return {
            execution.id: execution.status
            for execution in self.executions
            if execution.id in execution_ids
        }

    def list_steps(self, execution_id: str) -> list[Any]:
        assert any(execution.id == execution_id for execution in self.executions)
        return []

    def list_diagnostics(self, execution_id: str) -> list[ExecutionDiagnostic]:
        assert execution_id == self.execution.id
        return [
            replace(diagnostic, id=diagnostic.id or index)
            for index, diagnostic in enumerate(self.diagnostics, 1)
        ]

    def list_diagnostics_after(
        self, execution_id: str, after_id: int
    ) -> list[ExecutionDiagnostic]:
        assert execution_id == self.execution.id
        return [
            replace(diagnostic, id=diagnostic.id or index)
            for index, diagnostic in enumerate(self.diagnostics, 1)
            if (diagnostic.id or index) > after_id
        ]

    def request_cancellation(self, execution_id: str) -> bool:
        assert execution_id == self.execution.id
        if self.execution.status is WorkflowStatus.NEEDS_HUMAN:
            self.cancellation_requests.append(execution_id)
            self.execution.status = WorkflowStatus.CANCELLED
            return True
        if self.execution.status not in {
            WorkflowStatus.PENDING,
            WorkflowStatus.WAITING,
            WorkflowStatus.RUNNING,
        }:
            return False
        if self.execution.cancellation_requested_at is not None:
            return True
        self.cancellation_requests.append(execution_id)
        self.execution.cancellation_requested_at = datetime.now(UTC)
        return True

    def record_submission_repository(self, execution_id: str, repository: str) -> None:
        del execution_id
        self.execution.repository_ref = repository

    def executions_for_tasks(
        self, task_source: str, task_ids: list[str], repository: str
    ) -> dict[str, WorkflowExecution]:
        return {
            execution.task_id: execution
            for execution in self.executions
            if execution.task_source == task_source
            and execution.task_id in task_ids
            and repository in {execution.repository, execution.repository_ref}
        }

    def create_batch(
        self, repository: str, task_source: str, tasks: list[DevelopmentTask]
    ) -> WorkflowBatch:
        batch = WorkflowBatch(
            id=f"batch-{len(self.batch_rows) + 1}",
            repository=repository,
            task_source=task_source,
            items=tuple(
                WorkflowBatchItem(position, task.id, task.title)
                for position, task in enumerate(tasks)
            ),
        )
        self.batch_rows.append(batch)
        return batch

    def record_batch_item_execution(
        self, batch_id: str, position: int, execution_id: str
    ) -> None:
        batch = self.get_batch(batch_id)
        items = tuple(
            WorkflowBatchItem(item.position, item.task_id, item.task_title, execution_id)
            if item.position == position
            else item
            for item in batch.items
        )
        self.batch_rows[self.batch_rows.index(batch)] = WorkflowBatch(
            batch.id, batch.repository, batch.task_source, batch.created_at, items
        )

    def get_batch(self, batch_id: str) -> WorkflowBatch:
        return next(batch for batch in self.batch_rows if batch.id == batch_id)

    def list_batches(self) -> list[WorkflowBatch]:
        return list(self.batch_rows)


class _ConfigLoader:
    def __init__(self, path: Path) -> None:
        self._path = path
        self.saved_github_apps: tuple[
            str, GitHubAppSettings, GitHubAppSettings, GitHubAppSettings
        ] | None = None
        self.repository_entries = {
            "example": RepositoryConfiguration(
                key="example",
                path=str(path),
                resolved_path=path,
                auto_detect=True,
                commands=[["python", "-m", "pytest"]],
            )
        }
        self.config = ProjectConfig.model_validate(
            {
                "path": path,
                "providers": {
                    "planner": {"type": "litellm", "model": "test/model"},
                    "implementer": {
                        "type": "codex-cli",
                        "command": ["codex", "exec"],
                    },
                    "reviewer": {"type": "litellm", "model": "test/model"},
                    "vertex": {
                        "type": "gemini-vertex",
                        "project": "example-project",
                        "model": "gemini-configured-model",
                        "generation": {"temperature": 0.2},
                    },
                },
                "agents": {
                    "planner": {"provider": "planner"},
                    "implementer": {"provider": "implementer"},
                    "reviewer": {"provider": "reviewer"},
                },
                "task": {"provider": "github"},
                "integrations": {
                    "github": {
                        "app": {
                            "app_id": 12,
                            "installation_id": 34,
                            "private_key_env": "REVIEWER_APP_KEY",
                        },
                        "implementer_app": {
                            "app_id": 56,
                            "installation_id": 78,
                            "private_key_env": "IMPLEMENTER_APP_KEY",
                        },
                        "planner_app": {
                            "app_id": 90,
                            "installation_id": 91,
                            "private_key_env": "PLANNER_APP_KEY",
                        },
                    }
                },
            }
        )

    def list_repositories(self) -> list[ConfiguredRepository]:
        return [
            ConfiguredRepository(key, settings.resolved_path)
            for key, settings in self.repository_entries.items()
        ]

    def repository_settings(self, repository: str) -> RepositoryConfiguration:
        return self.repository_entries[repository]

    def repository_retry_defaults(self) -> tuple[int, int]:
        return 1, 2

    def resolve_repository_path(self, path: str) -> Path:
        candidate = Path(path)
        return candidate if candidate.is_absolute() else self._path / candidate

    def save_repository(
        self,
        repository_key: str,
        path: str,
        auto_detect: bool,
        commands: list[list[str]],
        original_key: str | None = None,
        review_retries: int | None | EllipsisType = ...,
        validation_retries: int | None | EllipsisType = ...,
    ) -> Path:
        source_key = original_key or repository_key
        if source_key != repository_key and repository_key in self.repository_entries:
            raise ValueError("repository already exists")
        resolved = self.resolve_repository_path(path)
        old = self.repository_entries.get(source_key)

        def budget(
            update: int | None | EllipsisType, previous: int, overridden: bool, default: int
        ) -> tuple[int, bool]:
            if update is ...:
                return previous, overridden
            if update is None:
                return default, False
            assert isinstance(update, int)
            return update, True

        review_value, review_overridden = budget(
            review_retries, old.review_retries if old else 1,
            old.review_retries_overridden if old else False, 1,
        )
        validation_value, validation_overridden = budget(
            validation_retries, old.validation_retries if old else 2,
            old.validation_retries_overridden if old else False, 2,
        )
        replacement = RepositoryConfiguration(
            key=repository_key,
            path=path,
            resolved_path=resolved,
            auto_detect=auto_detect,
            commands=commands,
            review_retries=review_value,
            review_retries_overridden=review_overridden,
            validation_retries=validation_value,
            validation_retries_overridden=validation_overridden,
        )
        if source_key in self.repository_entries:
            updated = {
                (repository_key if key == source_key else key): (
                    replacement if key == source_key else value
                )
                for key, value in self.repository_entries.items()
            }
            self.repository_entries = updated
        else:
            self.repository_entries[repository_key] = replacement
        return self._path / "orchestrator.yml"

    def delete_repository(self, repository_key: str) -> Path:
        del self.repository_entries[repository_key]
        return self._path / "orchestrator.yml"

    def load(self, task: DevelopmentTask, repository: str | None = None) -> ProjectConfig:
        assert task.repository == repository == "example"
        return self.config

    def save_github_app_settings(
        self,
        repository_key: str,
        *,
        implementer_app: GitHubAppSettings,
        reviewer_app: GitHubAppSettings,
        planner_app: GitHubAppSettings,
    ) -> Path:
        assert repository_key == "example"
        self.saved_github_apps = (repository_key, planner_app, implementer_app, reviewer_app)
        return self._path / "orchestrator.yml"

    def save_agent_preset(
        self,
        repository_key: str,
        role: str,
        name: str,
        preset: AgentPresetConfig,
        *,
        overwrite: bool = False,
    ) -> Path:
        assert repository_key == "example"
        presets = self.config.agent_presets.setdefault(role, {})
        if name in presets and not overwrite:
            raise ValueError("preset already exists")
        presets[name] = preset
        return self._path / "orchestrator.yml"

    def rename_agent_preset(
        self, repository_key: str, role: str, old_name: str, new_name: str
    ) -> Path:
        assert repository_key == "example"
        presets = self.config.agent_presets[role]
        if old_name != new_name and new_name in presets:
            raise ValueError("preset already exists")
        presets[new_name] = presets.pop(old_name)
        settings = getattr(self.config.agents, role)
        if settings.fallback_preset == old_name:
            settings.fallback_preset = new_name
        return self._path / "orchestrator.yml"

    def delete_agent_preset(self, repository_key: str, role: str, name: str) -> Path:
        assert repository_key == "example"
        del self.config.agent_presets[role][name]
        if not self.config.agent_presets[role]:
            del self.config.agent_presets[role]
        return self._path / "orchestrator.yml"

    def save_agent_fallback_preset(
        self, repository_key: str, role: str, preset_name: str | None
    ) -> Path:
        assert repository_key == "example"
        getattr(self.config.agents, role).fallback_preset = preset_name
        return self._path / "orchestrator.yml"


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


class _Handle:
    """Stand in for ExecutionHandle: the controller only polls and waits."""

    def __init__(self, execution: WorkflowExecution) -> None:
        self._execution = execution

    @property
    def execution_id(self) -> str:
        return self._execution.id

    def done(self) -> bool:
        return True

    def wait(self, timeout: float | None = None) -> bool:
        del timeout
        return True

    def result(self, timeout: float | None = None) -> WorkflowExecution:
        del timeout
        return self._execution


class _FailingHandle(_Handle):
    def __init__(self, execution_id: str) -> None:
        super().__init__(WorkflowExecution(id=execution_id))

    def result(self, timeout: float | None = None) -> WorkflowExecution:
        del timeout
        raise RuntimeError("simulated task failure")


class _WorkflowService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None, dict[AgentRole, str], dict[AgentRole, Any]]] = []
        self.emit_terminal_event = False
        self.continuations: list[tuple[str, str]] = []
        self.continuation_error: Exception | None = None
        self.responses: list[tuple[str, str]] = []
        self.cancellations: list[str] = []
        self.before_submit: Callable[[], None] | None = None
        self.fail_tasks: set[str] = set()

    def submit_execution(
        self,
        task_id: str,
        repository: str | None = None,
        *,
        agent_providers: dict[AgentRole, str],
        agent_runtime_options: dict[AgentRole, Any],
        event_listener: Callable[..., None] | None,
        admission_gate: Callable[[], bool] | None = None,
    ) -> _Handle:
        del admission_gate
        if self.before_submit is not None:
            self.before_submit()
        self.calls.append((task_id, repository, agent_providers, agent_runtime_options))
        execution_id = f"execution-{len(self.calls)}"
        if self.emit_terminal_event and event_listener is not None:
            event_listener(
                WorkflowEvent(
                    execution_id=execution_id,
                    kind=WorkflowEventKind.EXECUTION_CREATED,
                    status=WorkflowStatus.PENDING,
                )
            )
            event_listener(
                WorkflowEvent(
                    execution_id=execution_id,
                    kind=WorkflowEventKind.WORKFLOW_FINISHED,
                    status=WorkflowStatus.APPROVED,
                )
            )
        if task_id in self.fail_tasks:
            return _FailingHandle(execution_id)
        return _Handle(
            WorkflowExecution(
                id=execution_id,
                task_id=task_id,
                task_source="github",
                repository_ref=repository or "",
                status=WorkflowStatus.APPROVED,
            )
        )

    def run(
        self,
        task_id: str,
        repository: str | None = None,
        *,
        agent_providers: dict[AgentRole, str],
        agent_runtime_options: dict[AgentRole, Any],
        event_listener: Callable[..., None] | None,
    ) -> WorkflowExecution:
        return self.submit_execution(
            task_id,
            repository,
            agent_providers=agent_providers,
            agent_runtime_options=agent_runtime_options,
            event_listener=event_listener,
        ).result()

    def continuation_feedback(self, execution_id: str) -> str:
        assert execution_id == "execution-1"
        return "Persisted terminal feedback"

    def submit_continuation(
        self,
        execution_id: str,
        feedback: str,
        *,
        event_listener: Callable[..., None] | None = None,
        admission_gate: Callable[[], bool] | None = None,
    ) -> _Handle:
        del event_listener, admission_gate
        self.continuations.append((execution_id, feedback))
        if self.continuation_error is not None:
            raise self.continuation_error
        return _Handle(WorkflowExecution(id=execution_id, status=WorkflowStatus.APPROVED))

    def continue_execution(
        self,
        execution_id: str,
        feedback: str,
        *,
        event_listener: Callable[..., None] | None = None,
    ) -> WorkflowExecution:
        return self.submit_continuation(
            execution_id, feedback, event_listener=event_listener
        ).result()

    def submit_response(
        self,
        execution_id: str,
        response: str,
        *,
        event_listener: Callable[..., None] | None = None,
        admission_gate: Callable[[], bool] | None = None,
    ) -> _Handle:
        del event_listener, admission_gate
        self.responses.append((execution_id, response))
        return _Handle(WorkflowExecution(id=execution_id, status=WorkflowStatus.APPROVED))

    def cancel_execution(self, execution_id: str) -> bool:
        self.cancellations.append(execution_id)
        return True


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
            {},
        )
    ]


def test_controller_persists_and_runs_batch_in_order_with_shared_role_options(
    tmp_path: Path,
) -> None:
    controller, service, repository = _controller(tmp_path)
    tasks = [
        DevelopmentTask("12", "github", "First", "", "https://example/12"),
        DevelopmentTask("7", "github", "Second", "", "https://example/7"),
        DevelopmentTask("3", "github", "Third", "", "https://example/3"),
    ]
    provider = _TaskProvider()
    provider.list_tasks = lambda limit=50: tasks  # type: ignore[method-assign]
    controller._task_provider_factory = lambda source, repository: provider
    service.fail_tasks.add("7")
    service.before_submit = lambda: repository.batch_rows[0]
    runtime = {AgentRole.IMPLEMENTER: {"model": "codex-batch"}}

    batch = controller.start_batch(
        WorkflowBatchLaunch(
            repository="example",
            task_provider="github",
            task_ids=("12", "7", "3"),
            agent_providers={
                AgentRole.PLANNER: "planner",
                AgentRole.IMPLEMENTER: "implementer",
                AgentRole.REVIEWER: "reviewer",
            },
            agent_runtime_options=runtime,
        )
    )

    assert batch is not None
    assert [call[0] for call in service.calls] == ["12", "7", "3"]
    assert all(call[3] == runtime for call in service.calls)
    assert [item.task_id for item in batch.items] == ["12", "7", "3"]
    assert all(item.execution_id is not None for item in batch.items)


def test_controller_batch_closes_when_persisted_status_lags_a_settled_handle(
    tmp_path: Path,
) -> None:
    controller, service, repository = _controller(tmp_path)
    tasks = [
        DevelopmentTask("12", "github", "First", "", "https://example/12"),
        DevelopmentTask("3", "github", "Second", "", "https://example/3"),
    ]
    provider = _TaskProvider()
    provider.list_tasks = lambda limit=50: tasks  # type: ignore[method-assign]
    controller._task_provider_factory = lambda source, repository: provider
    # The first item's handle settles APPROVED while its persisted row still
    # reads RUNNING; that stale read must not keep the batch open.
    repository.execution.status = WorkflowStatus.RUNNING
    outcome: list[WorkflowBatch | None] = []
    worker = threading.Thread(
        target=lambda: outcome.append(controller.start_batch(_batch_launch("12", "3"))),
        daemon=True,
    )

    worker.start()
    worker.join(timeout=5)

    assert not worker.is_alive(), "start_batch did not observe batch completion"
    assert outcome[0] is not None
    assert [call[0] for call in service.calls] == ["12", "3"]


def test_controller_exposes_batch_identity_before_first_item_submits(
    tmp_path: Path,
) -> None:
    controller, service, repository = _controller(tmp_path)
    tasks = [
        DevelopmentTask("12", "github", "First", "", "https://example/12"),
        DevelopmentTask("7", "github", "Second", "", "https://example/7"),
    ]
    provider = _TaskProvider()
    provider.list_tasks = lambda limit=50: tasks  # type: ignore[method-assign]
    controller._task_provider_factory = lambda source, repository: provider
    observed: list[WorkflowBatch] = []

    def observe(batch: WorkflowBatch) -> None:
        # The batch row is already durable and every item is still unlinked:
        # nothing has been submitted through the service yet.
        assert repository.get_batch(batch.id).id == batch.id
        assert [item.execution_id for item in batch.items] == [None, None]
        assert service.calls == []
        observed.append(batch)

    batch = controller.start_batch(
        WorkflowBatchLaunch(
            repository="example",
            task_provider="github",
            task_ids=("12", "7"),
            agent_providers={
                AgentRole.PLANNER: "planner",
                AgentRole.IMPLEMENTER: "implementer",
                AgentRole.REVIEWER: "reviewer",
            },
        ),
        on_batch_created=observe,
    )

    assert len(observed) == 1
    assert batch is not None
    assert observed[0].id == batch.id
    assert [call[0] for call in service.calls] == ["12", "7"]


def test_controller_batch_empty_selection_is_a_no_op(tmp_path: Path) -> None:
    controller, service, repository = _controller(tmp_path)

    result = controller.start_batch(
        WorkflowBatchLaunch(
            repository="example",
            task_provider="github",
            task_ids=(),
            agent_providers={},
        )
    )

    assert result is None
    assert service.calls == []
    assert repository.batch_rows == []


def test_controller_quota_event_closes_gate_until_provider_is_marked_available(
    tmp_path: Path,
) -> None:
    controller, _, _ = _controller(tmp_path)
    run = _BatchRun("batch-1", 1, frozenset({"planner"}), None)
    controller._active_batches[run.batch_id] = run
    awakened = False

    def reevaluate() -> None:
        nonlocal awakened
        awakened = True

    controller._dispatcher.reevaluate = reevaluate  # type: ignore[method-assign]
    controller._observe_batch_event(
        run,
        0,
        WorkflowEvent(
            execution_id="execution-1",
            kind=WorkflowEventKind.DIAGNOSTIC,
            payload={"kind": "PROVIDER_ERROR", "provider": "planner", "category": "rate_limit"},
        ),
    )
    assert run.ready()
    controller._observe_batch_event(
        run,
        0,
        WorkflowEvent(
            execution_id="execution-1",
            kind=WorkflowEventKind.DIAGNOSTIC,
            payload={"kind": "PROVIDER_QUOTA_EXHAUSTED", "provider": "planner"},
        ),
    )
    assert controller.batch_unavailable_providers(run.batch_id) == ("planner",)
    assert not run.ready()

    controller.mark_batch_provider_available(run.batch_id, "planner")
    assert run.ready()
    assert awakened


@pytest.mark.parametrize(
    "status",
    [
        WorkflowStatus.APPROVED,
        WorkflowStatus.CHANGES_REQUESTED,
        WorkflowStatus.FAILED,
        WorkflowStatus.CANCELLED,
    ],
)
def test_batch_completion_accepts_final_statuses(status: WorkflowStatus) -> None:
    run = _BatchRun("batch-1", 2, frozenset(), None)
    run.submissions_done = True
    run.skipped.add(1)
    run.statuses[0] = status
    assert run.complete()


@pytest.mark.parametrize(
    "status",
    [
        WorkflowStatus.PENDING,
        WorkflowStatus.WAITING,
        WorkflowStatus.RUNNING,
        WorkflowStatus.NEEDS_HUMAN,
    ],
)
def test_batch_completion_waits_for_open_statuses(status: WorkflowStatus) -> None:
    run = _BatchRun("batch-1", 1, frozenset(), None)
    run.submissions_done = True
    run.statuses[0] = status
    assert not run.complete()
    run.stopping = True
    assert run.complete() is (status is WorkflowStatus.NEEDS_HUMAN)


def test_controller_validates_shared_batch_launch_before_persisting(tmp_path: Path) -> None:
    controller, service, repository = _controller(tmp_path)

    with pytest.raises(RuntimeError, match="not configured"):
        controller.start_batch(
            WorkflowBatchLaunch(
                repository="example",
                task_provider="github",
                task_ids=("12",),
                agent_providers={AgentRole.PLANNER: "unknown-provider"},
            )
        )

    assert service.calls == []
    assert repository.batch_rows == []


def test_controller_batch_cancellation_is_accepted_between_items(tmp_path: Path) -> None:
    controller, _, repository = _controller(tmp_path)
    repository.execution.status = WorkflowStatus.RUNNING
    controller._execution_batches[repository.execution.id] = "batch-1"

    assert controller.cancel_workflow(repository.execution.id) is True
    assert repository.cancellation_requests == [repository.execution.id]


@pytest.mark.parametrize(
    "bookkeeping_status", [WorkflowStatus.NEEDS_HUMAN, WorkflowStatus.RUNNING]
)
def test_controller_cancelling_last_human_blocked_item_completes_batch(
    tmp_path: Path, bookkeeping_status: WorkflowStatus
) -> None:
    controller, _, repository = _controller(tmp_path)
    repository.execution.status = WorkflowStatus.NEEDS_HUMAN
    run = _BatchRun("batch-1", 2, frozenset(), None)
    run.submissions_done = True
    run.statuses = {0: WorkflowStatus.APPROVED, 1: bookkeeping_status}
    run.positions[repository.execution.id] = 1
    controller._active_batches[run.batch_id] = run
    controller._execution_batches[repository.execution.id] = run.batch_id

    assert not run.complete()
    assert controller.cancel_workflow(repository.execution.id)
    assert run.statuses == {0: WorkflowStatus.APPROVED, 1: WorkflowStatus.CANCELLED}
    assert run.complete()
    assert repository.cancellation_requests == [repository.execution.id]

    controller._observe_batch_event(run, 1, WorkflowEvent(
        repository.execution.id, WorkflowEventKind.WORKFLOW_FINISHED,
        status=WorkflowStatus.NEEDS_HUMAN,
    ))
    assert run.statuses[1] is WorkflowStatus.CANCELLED


def test_batch_reconciles_human_cancellation_from_another_controller(tmp_path: Path) -> None:
    controller, _, repository = _controller(tmp_path)
    repository.execution.status = WorkflowStatus.NEEDS_HUMAN
    provider = _TaskProvider()
    controller._task_provider_factory = lambda source, repository: provider

    class HumanBlockedService(_WorkflowService):
        def submit_execution(self, task_id: str, repository: str | None = None, **kwargs: Any):
            return _Handle(WorkflowExecution(
                id="execution-1", task_id=task_id, status=WorkflowStatus.NEEDS_HUMAN
            ))

    controller._workflow_service_factory = lambda provider: HumanBlockedService()
    other_controller = WorkflowController(
        workflow_repository=repository,  # type: ignore[arg-type]
        config_loader=controller._config_loader,
        client_factory=object(),  # type: ignore[arg-type]
        task_provider_factory=lambda source, repository: provider,
    )
    created = threading.Event()
    runs: list[_BatchRun] = []
    outcomes: list[WorkflowBatch] = []

    def on_created(batch: WorkflowBatch) -> None:
        runs.append(controller._active_batches[batch.id])
        created.set()

    worker = threading.Thread(
        target=lambda: outcomes.append(controller.start_batch(
            _batch_launch("12"), on_batch_created=on_created
        )),
        daemon=True,
    )
    worker.start()
    assert created.wait(timeout=5)
    run = runs[0]
    with run.condition:
        assert run.condition.wait_for(
            lambda: run.submissions_done
            and run.statuses.get(0) is WorkflowStatus.NEEDS_HUMAN,
            timeout=5,
        )
    assert other_controller.cancel_workflow("execution-1")
    worker.join(timeout=5)
    assert not worker.is_alive(), "batch did not reconcile cancellation from another controller"
    assert run.statuses == {0: WorkflowStatus.CANCELLED}
    assert outcomes and outcomes[0].id == run.batch_id


def test_controller_stopping_batch_cancels_current_and_skips_later_tasks(
    tmp_path: Path,
) -> None:
    controller, _, repository = _controller(tmp_path)
    tasks = [
        DevelopmentTask("12", "github", "First", "", "https://example/12"),
        DevelopmentTask("7", "github", "Second", "", "https://example/7"),
    ]
    provider = _TaskProvider()
    provider.list_tasks = lambda limit=50: tasks  # type: ignore[method-assign]
    controller._task_provider_factory = lambda source, selected_repository: provider
    entered = {task_id: threading.Event() for task_id in ("12", "7")}
    release = {task_id: threading.Event() for task_id in ("12", "7")}
    calls: list[str] = []

    class BlockingHandle:
        def __init__(self, task_id: str) -> None:
            self.task_id = task_id
            self.execution_id = f"execution-{task_id}"

        def result(self, timeout: float | None = None) -> WorkflowExecution:
            entered[self.task_id].set()
            assert release[self.task_id].wait(timeout=5), "batch cancellation did not release task"
            return WorkflowExecution(
                id=self.execution_id, status=WorkflowStatus.CANCELLED
            )

    class BlockingService(_WorkflowService):
        def submit_execution(self, task_id: str, repository: str | None = None, **kwargs: Any):
            calls.append(task_id)
            listener = kwargs.get("event_listener")
            assert listener is not None
            listener(
                WorkflowEvent(
                    execution_id=f"execution-{task_id}",
                    kind=WorkflowEventKind.EXECUTION_CREATED,
                    status=WorkflowStatus.PENDING,
                )
            )
            return BlockingHandle(task_id)

        def cancel_execution(self, execution_id: str) -> bool:
            release[execution_id.removeprefix("execution-")].set()
            return True

    blocking_service = BlockingService()
    controller._workflow_service_factory = lambda selected_provider: blocking_service
    errors: list[BaseException] = []

    def run_batch() -> None:
        try:
            controller.start_batch(
                WorkflowBatchLaunch(
                    repository="example",
                    task_provider="github",
                    task_ids=("12", "7"),
                    agent_providers={
                        AgentRole.PLANNER: "planner",
                        AgentRole.IMPLEMENTER: "implementer",
                        AgentRole.REVIEWER: "reviewer",
                    },
                )
            )
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=run_batch)
    worker.start()
    assert entered["12"].wait(timeout=5), "batch did not start its first task"
    assert entered["7"].wait(timeout=5), "batch did not submit its second task"

    assert controller.cancel_batch("batch-1") is True
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert errors == []
    assert calls == ["12", "7"]
    assert repository.get_batch("batch-1").items[1].execution_id == "execution-7"


def test_controller_rejects_batch_task_with_an_existing_execution(tmp_path: Path) -> None:
    controller, service, repository = _controller(tmp_path)
    repository.execution.task_source = "github"
    repository.execution.task_id = "12"
    repository.execution.repository_ref = "example"

    with pytest.raises(ValueError, match="already have executions"):
        controller.start_batch(
            WorkflowBatchLaunch(
                repository="example",
                task_provider="github",
                task_ids=("12",),
                agent_providers={},
            )
        )
    assert service.calls == []
    assert repository.batch_rows == []


def test_controller_batch_accepts_failed_prior_run_and_new_task_in_order(
    tmp_path: Path,
) -> None:
    controller, service, repository = _controller(tmp_path)
    repository.execution.task_source = "github"
    repository.execution.task_id = "7"
    repository.execution.repository_ref = "example"
    repository.execution.status = WorkflowStatus.FAILED
    provider = _TaskProvider()
    provider.list_tasks = lambda limit=50: [
        DevelopmentTask("12", "github", "New", "", "https://example/12"),
        DevelopmentTask("7", "github", "Retry", "", "https://example/7"),
    ]
    controller._task_provider_factory = lambda source, selected_repository: provider

    batch = controller.start_batch(_batch_launch("12", "7"))

    assert batch is not None
    assert [call[0] for call in service.calls] == ["12", "7"]
    assert repository.execution.status == WorkflowStatus.FAILED


def test_controller_batch_rechecks_prior_run_before_submission(tmp_path: Path) -> None:
    controller, service, repository = _controller(tmp_path)
    repository.execution.task_source = "github"
    repository.execution.task_id = "12"
    repository.execution.repository_ref = "example"
    repository.execution.status = WorkflowStatus.FAILED

    batch = controller.start_batch(
        _batch_launch("12"),
        on_batch_created=lambda _: setattr(
            repository.execution, "status", WorkflowStatus.APPROVED
        ),
    )

    assert batch is not None
    assert service.calls == []
    assert repository.get_batch(batch.id).items[0].execution_id is None


def test_controller_saves_renames_and_deletes_repository_configuration(
    tmp_path: Path,
) -> None:
    controller, _, _ = _controller(tmp_path)

    controller.save_repository(
        "renamed",
        "renamed-path",
        False,
        [["python", "-m", "ruff", "check", "."]],
        original_key="example",
    )

    assert controller.selected_repository == "renamed"
    assert [repository.key for repository in controller.repositories()] == ["renamed"]
    assert controller.repository_settings("renamed").commands == [
        ["python", "-m", "ruff", "check", "."]
    ]

    controller.delete_repository("renamed")
    assert controller.repositories() == []
    assert controller.selected_repository is None


def test_controller_reports_heuristic_context_ready_without_an_index(tmp_path: Path) -> None:
    controller, _, _ = _controller(tmp_path)
    controller.project_config("example").repository_context = RepositoryContextSettings(
        strategy="heuristic"
    )

    status = controller.repository_index_status("example")

    assert status.strategy == "heuristic"
    assert status.state is IndexLifecycleState.READY
    with pytest.raises(ValueError, match="require repository_context.strategy: qdrant"):
        controller.build_repository_index("example")


def test_controller_discards_finished_workflow_service(tmp_path: Path) -> None:
    controller, service, _ = _controller(tmp_path)
    service.emit_terminal_event = True

    controller.start_workflow(
        WorkflowLaunch(
            repository="example",
            task_provider="github",
            task_id="12",
            agent_providers={
                AgentRole.PLANNER: "planner",
                AgentRole.IMPLEMENTER: "implementer",
                AgentRole.REVIEWER: "reviewer",
            },
        )
    )

    assert controller._active_services == {}


def test_controller_passes_validated_role_runtime_overrides_without_mutating_defaults(
    tmp_path: Path,
) -> None:
    controller, service, _ = _controller(tmp_path)

    controller.start_workflow(
        WorkflowLaunch(
            repository="example",
            task_provider="github",
            task_id="12",
            agent_providers={
                AgentRole.PLANNER: "planner",
                AgentRole.IMPLEMENTER: "implementer",
                AgentRole.REVIEWER: "reviewer",
            },
            agent_runtime_options={
                AgentRole.IMPLEMENTER: {
                    "model": "any-codex-model",
                    "reasoning_effort": "high",
                }
            },
        )
    )

    assert service.calls[0][3] == {
        AgentRole.IMPLEMENTER: {
            "model": "any-codex-model",
            "reasoning_effort": "high",
        }
    }


def test_controller_logs_tui_role_resolution_with_selected_backend(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    controller, _, _ = _controller(tmp_path)
    controller._verbose = True
    config = controller.project_config("example")
    config.providers["vertex-gemini-litellm"] = AgentProviderSettings.model_validate(
        {
            "type": "litellm",
            "model": "vertex_ai/gemini-3.8-flash",
            "generation": {
                "vertex_project": "agents-orchestrator-507215",
                "vertex_location": "global",
            },
        }
    )
    config.agents.planner.provider = "vertex-gemini-litellm"
    config.agents.reviewer.provider = "vertex-gemini-litellm"

    with caplog.at_level(logging.DEBUG, logger="orchestrator"):
        controller.start_workflow(
            WorkflowLaunch(
                repository="example",
                task_provider="github",
                task_id="12",
                agent_providers={
                    AgentRole.PLANNER: "vertex-gemini-litellm",
                    AgentRole.IMPLEMENTER: "implementer",
                    AgentRole.REVIEWER: "vertex-gemini-litellm",
                },
            )
        )

    assert "planner -> vertex-gemini-litellm (litellm)" in caplog.text
    assert "reviewer -> vertex-gemini-litellm (litellm)" in caplog.text
    assert "implementer -> implementer (codex-cli)" in caplog.text


def test_controller_rejects_runtime_options_not_supported_by_selected_provider(
    tmp_path: Path,
) -> None:
    controller, _, _ = _controller(tmp_path)

    with pytest.raises(Exception, match="does not support runtime option"):
        controller.start_workflow(
            WorkflowLaunch(
                repository="example",
                task_provider="github",
                task_id="12",
                agent_providers={
                    AgentRole.PLANNER: "planner",
                    AgentRole.IMPLEMENTER: "implementer",
                    AgentRole.REVIEWER: "reviewer",
                },
                agent_runtime_options={AgentRole.PLANNER: {"permission_mode": "auto"}},
            )
        )


def test_textual_app_navigates_to_workflow_runner(tmp_path: Path) -> None:
    controller, _, _ = _controller(tmp_path)
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test() as pilot:
            await pilot.press("n")
            assert isinstance(application.screen, WorkflowRunnerScreen)

    asyncio.run(drive())


@pytest.mark.parametrize(
    "role, filename",
    [
        (AgentRole.PLANNER, "PLANNER.md"),
        (AgentRole.IMPLEMENTER, "IMPLEMENTER.md"),
        (AgentRole.REVIEWER, "REVIEWER.md"),
    ],
)
def test_agent_instructions_refuse_symlinked_files(
    tmp_path: Path, role: AgentRole, filename: str
) -> None:
    repository_path = tmp_path / "repository"
    repository_path.mkdir()
    outside = tmp_path / "external.md"
    outside.write_text("Keep this content", encoding="utf-8")
    instruction_path = repository_path / filename
    try:
        instruction_path.symlink_to(outside)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"Symlinks unavailable: {exc}")

    controller, _, _ = _controller(repository_path)
    assert controller.agent_instructions("example") == (None, None, None)
    with pytest.raises(ValueError, match="symlink"):
        controller.save_agent_instructions("example", role, "New guidance")
    assert outside.read_text(encoding="utf-8") == "Keep this content"
    assert instruction_path.is_symlink()


def test_agent_instructions_save_cannot_follow_a_link_added_during_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository_path = tmp_path / "repository"
    repository_path.mkdir()
    outside = tmp_path / "external.md"
    outside.write_text("Keep this content", encoding="utf-8")
    instruction_path = repository_path / "PLANNER.md"
    original_replace = os.replace

    def replace_after_link(source: Path, destination: Path) -> None:
        assert destination == instruction_path
        try:
            instruction_path.symlink_to(outside)
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f"Symlinks unavailable: {exc}")
        original_replace(source, destination)

    monkeypatch.setattr("orchestrator.tui.bridge.os.replace", replace_after_link)
    controller, _, _ = _controller(repository_path)
    controller.save_agent_instructions("example", AgentRole.PLANNER, "New guidance")

    assert outside.read_text(encoding="utf-8") == "Keep this content"
    assert instruction_path.read_text(encoding="utf-8") == "New guidance"
    assert not instruction_path.is_symlink()


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes required")
def test_agent_instructions_save_preserves_existing_file_mode(tmp_path: Path) -> None:
    instruction_path = tmp_path / "PLANNER.md"
    instruction_path.write_text("Old guidance", encoding="utf-8")
    instruction_path.chmod(0o640)
    controller, _, _ = _controller(tmp_path)

    controller.save_agent_instructions("example", AgentRole.PLANNER, "New guidance")

    assert instruction_path.read_text(encoding="utf-8") == "New guidance"
    assert instruction_path.stat().st_mode & 0o777 == 0o640


def test_agent_instructions_navigation_uses_a_compact_distinct_control(tmp_path: Path) -> None:
    controller, _, _ = _controller(tmp_path)
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test(size=(120, 50)) as pilot:
            navigation = application.query_one("#agent-instructions-button", Button)
            history = application.query_one("#history", Button)

            assert navigation.size.height == history.size.height
            assert navigation.size.height < application.size.height
            assert navigation.size.width < application.size.width

            await pilot.click("#agent-instructions-button")
            await settle_tui(pilot)
            assert isinstance(application.screen, AgentInstructionsScreen)
            assert isinstance(
                application.screen.query_one("#agent-instructions"), VerticalScroll
            )
            repository = application.screen.query_one(
                "#agent-instructions-repository", RepositorySelect
            )
            assert repository.value == "example"
            assert repository._options == [("example", "example")]

    asyncio.run(drive())


def test_repository_select_defers_options_until_overlay_mounts(tmp_path: Path) -> None:
    selector = RepositorySelect()
    repositories = [
        ConfiguredRepository("example", tmp_path),
        ConfiguredRepository("selected", tmp_path),
    ]

    assert (
        selector.set_repositories(repositories, selected_repository="selected")
        == "selected"
    )
    application = _RepositorySelectApp(selector)

    async def drive() -> None:
        async with application.run_test() as pilot:
            await wait_for_tui(
                pilot,
                lambda: selector.value == "selected" and bool(selector._options),
                description="RepositorySelect options and selected value",
            )
            overlay = selector.query_one("SelectOverlay")

            assert selector.value == "selected"
            assert selector._options == [
                ("example", "example"),
                ("selected", "selected"),
            ]
            assert [str(option.prompt) for option in overlay._options] == [
                "example",
                "selected",
            ]

    asyncio.run(drive())


def test_main_navigation_reflows_compact_controls_without_content_sizing(tmp_path: Path) -> None:
    controller, _, _ = _controller(tmp_path)
    application = OrchestratorApp(controller)
    navigation_ids = (
        "new-workflow",
        "history",
        "repositories",
        "github-apps",
        "agent-instructions-button",
        "agent-presets",
    )

    async def drive() -> None:
        async with application.run_test(size=(120, 50)) as pilot:
            navigation = application.query_one("#home-actions", Horizontal)
            controls = [
                application.query_one(f"#{control_id}", Button) for control_id in navigation_ids
            ]

            assert all(control.has_class("navigation-control") for control in controls)
            assert len({control.size.height for control in controls}) == 1
            assert len({control.region.y for control in controls}) == 1
            assert navigation.size.height == 3
            assert navigation.size.height < application.size.height
            assert all(control.size.width < application.size.width for control in controls)
            assert all(str(control.styles.width) != "1fr" for control in controls)

            await pilot.resize_terminal(70, 50)
            await settle_tui(pilot)

            assert len({control.region.y for control in controls}) > 1
            assert len({control.size.height for control in controls}) == 1
            assert all(control.size.width < application.size.width for control in controls)
            assert all(str(control.styles.width) != "1fr" for control in controls)

    asyncio.run(drive())


def test_workflow_runner_navigation_reflows_compact_controls(tmp_path: Path) -> None:
    controller, _, _ = _controller(tmp_path)
    application = OrchestratorApp(controller)
    navigation_ids = (
        "start-workflow",
        "workflow-history",
        "workflow-repositories",
        "workflow-github-apps",
        "workflow-agent-instructions",
        "workflow-agent-presets",
    )

    async def drive() -> None:
        async with application.run_test(size=(120, 50)) as pilot:
            application.push_screen(WorkflowRunnerScreen())
            await settle_tui(pilot)
            controls = [
                application.screen.query_one(f"#{control_id}", Button)
                for control_id in navigation_ids
            ]

            assert all(control.has_class("navigation-control") for control in controls)
            assert len({control.size.height for control in controls}) == 1
            assert len({control.region.y for control in controls}) == 1
            assert all(control.size.width < application.size.width for control in controls)
            assert all(str(control.styles.width) != "1fr" for control in controls)

            await pilot.resize_terminal(70, 50)
            await settle_tui(pilot)

            assert len({control.region.y for control in controls}) > 1
            assert len({control.size.height for control in controls}) == 1
            assert all(control.size.width < application.size.width for control in controls)
            assert all(str(control.styles.width) != "1fr" for control in controls)

    asyncio.run(drive())


def test_main_navigation_controls_keep_their_destinations(tmp_path: Path) -> None:
    controller, _, _ = _controller(tmp_path)
    application = OrchestratorApp(controller)
    destinations = (
        ("#new-workflow", WorkflowRunnerScreen),
        ("#history", ExecutionHistoryScreen),
        ("#repositories", RepositorySettingsScreen),
        ("#github-apps", GitHubAppSettingsScreen),
        ("#agent-instructions-button", AgentInstructionsScreen),
        ("#agent-presets", AgentPresetsScreen),
    )

    async def drive() -> None:
        async with application.run_test(size=(120, 50)) as pilot:
            for selector, expected_screen in destinations:
                await pilot.click(selector)
                await settle_tui(pilot)
                assert isinstance(application.screen, expected_screen)
                await pilot.press("escape")
                await settle_tui(pilot)

    asyncio.run(drive())


def test_repository_settings_creates_repository_and_reorders_commands(
    tmp_path: Path,
) -> None:
    controller, _, _ = _controller(tmp_path)
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test(size=(120, 60)) as pilot:
            await pilot.press("r")
            await settle_tui(pilot)
            assert isinstance(application.screen, RepositorySettingsScreen)
            screen = application.screen

            await pilot.click("#add-repository")
            screen.query_one("#repository-key", Input).value = "owner/new-repository"
            screen.query_one("#repository-path", Input).value = "missing/repository"
            await settle_tui(pilot)
            assert "does not currently exist" in str(
                screen.query_one("#repository-path-warning", Static).renderable
            )

            command = screen.query_one("#validation-command-input", Input)
            command.value = "python -m pytest"
            screen.query_one("#add-validation-command", Button).press()
            await settle_tui(pilot)
            assert screen._commands == [["python", "-m", "pytest"]]
            command.value = "python -m mypy 'src/orchestrator package'"
            screen.query_one("#add-validation-command", Button).press()
            await settle_tui(pilot)
            assert screen._commands == [
                ["python", "-m", "pytest"],
                ["python", "-m", "mypy", "src/orchestrator package"],
            ]
            screen.query_one("#validation-command-list", OrchestratorSelect).value = 1
            await settle_tui(pilot)
            screen.query_one("#move-validation-command-up", Button).press()
            await settle_tui(pilot)
            screen.query_one("#save-repository", Button).press()
            await settle_tui(pilot)

            settings = controller.repository_settings("owner/new-repository")
            assert settings.commands == [
                ["python", "-m", "mypy", "src/orchestrator package"],
                ["python", "-m", "pytest"],
            ]
            assert controller.selected_repository == "owner/new-repository"

            screen.query_one("#add-repository", Button).press()
            await settle_tui(pilot)
            screen.query_one("#repository-key", Input).value = "example"
            screen.query_one("#repository-path", Input).value = str(tmp_path)
            screen.query_one("#save-repository", Button).press()
            await settle_tui(pilot)
            assert "already configured" in str(
                screen.query_one("#repository-errors", Static).renderable
            )

    asyncio.run(drive())


def test_repository_deletion_requires_confirmation_and_leaves_path(
    tmp_path: Path,
) -> None:
    controller, _, _ = _controller(tmp_path)
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test(size=(100, 45)) as pilot:
            await pilot.press("r")
            await wait_for_tui(
                pilot,
                lambda: isinstance(application.screen, RepositorySettingsScreen),
                description="repository settings screen",
            )
            application.screen.query_one("#delete-repository", Button).press()
            await wait_for_tui(
                pilot,
                lambda: isinstance(application.screen, DeleteRepositoryConfirmation),
                description="repository deletion confirmation",
            )
            application.screen.query_one("#cancel-delete", Button).press()
            await wait_for_tui(
                pilot,
                lambda: isinstance(application.screen, RepositorySettingsScreen),
                description="repository settings after cancelling deletion",
            )
            assert [repository.key for repository in controller.repositories()] == ["example"]

            application.screen.query_one("#delete-repository", Button).press()
            await wait_for_tui(
                pilot,
                lambda: isinstance(application.screen, DeleteRepositoryConfirmation),
                description="repository deletion confirmation",
            )
            application.screen.query_one("#confirm-delete", Button).press()
            await wait_for_tui(
                pilot,
                lambda: controller.repositories() == [],
                description="repository configuration removal",
            )
            assert controller.repositories() == []
            assert tmp_path.is_dir()
            assert "files were not deleted" in str(
                application.screen.query_one("#repository-status", Static).renderable
            )

    asyncio.run(drive())


def test_repository_settings_actions_remain_scrollable_on_small_terminal(
    tmp_path: Path,
) -> None:
    controller, _, _ = _controller(tmp_path)
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test(size=(60, 15)) as pilot:
            await pilot.press("r")
            await settle_tui(pilot)
            content = application.screen.query_one("#repository-settings", VerticalScroll)
            assert application.screen.has_class("-narrow")
            assert content.max_scroll_y > 0

            application.screen.query_one("#save-repository", Button).focus()
            await wait_for_tui(
                pilot,
                lambda: content.scroll_y > 0,
                description="focused Save repository button in the scrollable viewport",
            )
            assert content.scroll_y > 0

    asyncio.run(drive())


def test_repository_retry_budgets_edit_clear_validate_and_switch(tmp_path: Path) -> None:
    controller, _, _ = _controller(tmp_path)
    controller.save_repository("second", "second", True, [])
    controller.select_repository("example")
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test(size=(60, 15)) as pilot:
            await pilot.press("r")
            await settle_tui(pilot)
            screen = application.screen
            assert isinstance(screen, RepositorySettingsScreen)
            assert screen.query_one("#review-budget", Input).value == "1"
            assert "inherited from defaults" in str(
                screen.query_one("#review-budget-source", Static).renderable
            )
            review = screen.query_one("#review-budget", Input)
            review.focus()
            content = screen.query_one("#repository-settings", VerticalScroll)
            await wait_for_tui(
                pilot, lambda: content.scroll_y > 0,
                description="keyboard focus on review budget in narrow viewport",
            )
            review.value = "0"
            screen.query_one("#validation-budget", Input).value = "4"
            screen.query_one("#save-repository", Button).press()
            await settle_tui(pilot)
            settings = controller.repository_settings("example")
            assert (settings.review_retries, settings.review_retries_overridden) == (0, True)
            assert (
                settings.validation_retries, settings.validation_retries_overridden
            ) == (4, True)
            assert "overridden for this repository" in str(
                screen.query_one("#review-budget-source", Static).renderable
            )

            for invalid in ("-1", "1.5", "", "abc"):
                screen.query_one("#review-budget", Input).value = invalid
                await settle_tui(pilot)
                assert "non-negative integer" in str(
                    screen.query_one("#review-budget-error", Static).renderable
                )
                screen.query_one("#save-repository", Button).press()
                await settle_tui(pilot)
                assert controller.repository_settings("example").review_retries == 0

            screen.query_one("#review-budget", Input).value = "0"
            screen.query_one("#validation-budget", Input).value = "-2"
            await settle_tui(pilot)
            assert "non-negative integer" in str(
                screen.query_one("#validation-budget-error", Static).renderable
            )
            screen.query_one("#save-repository", Button).press()
            await settle_tui(pilot)
            assert controller.repository_settings("example").validation_retries == 4
            screen.query_one("#validation-budget", Input).value = "4"

            screen.query_one("#clear-review-budget", Button).press()
            await settle_tui(pilot)
            assert screen.query_one("#review-budget", Input).value == "1"
            screen.query_one("#save-repository", Button).press()
            await settle_tui(pilot)
            settings = controller.repository_settings("example")
            assert settings.review_retries_overridden is False
            assert settings.validation_retries_overridden is True

            screen.query_one("#clear-validation-budget", Button).press()
            await settle_tui(pilot)
            assert screen.query_one("#validation-budget", Input).value == "2"
            screen.query_one("#save-repository", Button).press()
            await settle_tui(pilot)
            settings = controller.repository_settings("example")
            assert settings.validation_retries_overridden is False

            screen.query_one("#repository-picker", RepositorySelect).value = "second"
            await wait_for_tui(
                pilot, lambda: screen._editing_key == "second",
                description="second repository selection",
            )
            assert screen.query_one("#review-budget", Input).value == "1"
            assert "inherited from defaults" in str(
                screen.query_one("#validation-budget-source", Static).renderable
            )

    asyncio.run(drive())


def test_repository_load_failure_disables_stale_editor_and_prevents_save(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller, _, _ = _controller(tmp_path)
    controller.save_repository("invalid", "invalid", True, [])
    controller.select_repository("example")
    original_settings = controller.repository_settings

    def repository_settings(repository: str) -> RepositoryConfiguration:
        if repository == "invalid":
            raise ValueError("review.max_review_retries must be a non-negative integer")
        return original_settings(repository)

    monkeypatch.setattr(controller, "repository_settings", repository_settings)
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test(size=(100, 45)) as pilot:
            await pilot.press("r")
            await wait_for_tui(
                pilot,
                lambda: isinstance(application.screen, RepositorySettingsScreen)
                and application.screen._editing_key == "example",
                description="valid repository editor",
            )
            screen = application.screen
            assert isinstance(screen, RepositorySettingsScreen)
            screen.query_one("#repository-key", Input).value = "accidental-rename"
            screen.query_one("#repository-picker", RepositorySelect).value = "invalid"
            await wait_for_tui(
                pilot,
                lambda: "Cannot load repository configuration" in str(
                    screen.query_one("#repository-errors", Static).renderable
                ),
                description="invalid repository load error",
            )
            assert screen._editing_key is None
            assert screen.query_one("#repository-key", Input).value == ""
            assert screen.query_one("#save-repository", Button).is_disabled
            assert screen.query_one("#delete-repository", Button).is_disabled
            screen.query_one("#save-repository", Button).press()
            screen._save_repository()
            await settle_tui(pilot)
            assert [repository.key for repository in controller.repositories()] == [
                "example", "invalid"
            ]

            screen.query_one("#repository-picker", RepositorySelect).value = "example"
            await wait_for_tui(
                pilot,
                lambda: screen._editing_key == "example",
                description="valid repository restored",
            )
            assert not screen.query_one("#save-repository", Button).is_disabled
            assert screen.query_one("#repository-key", Input).value == "example"

    asyncio.run(drive())


def test_repository_index_management_renders_independent_status_and_dispatches_actions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller, _, _ = _controller(tmp_path)
    controller.save_repository("second", "second", True, [])
    statuses = {
        "example": RepositoryIndexStatus(
            strategy="qdrant",
            state=IndexLifecycleState.READY,
            last_successful_indexed_at="2026-09-13T12:00:00+00:00",
            document_count=2,
            point_count=4,
        ),
        "second": RepositoryIndexStatus(
            strategy="qdrant",
            state=IndexLifecycleState.STALE,
            last_indexed_commit="old-commit",
        ),
    }
    calls: list[tuple[str, str]] = []

    def index_status(repository: str) -> RepositoryIndexStatus:
        return statuses[repository]

    monkeypatch.setattr(controller, "repository_index_status", index_status)

    def refresh(repository: str) -> RepositoryIndexStatus:
        calls.append(("refresh", repository))
        return RepositoryIndexStatus(strategy="qdrant", state=IndexLifecycleState.READY)

    monkeypatch.setattr(controller, "refresh_repository_index", refresh)
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test(size=(120, 60)) as pilot:
            await pilot.press("r")
            await wait_for_tui(
                pilot,
                lambda: "READY"
                in str(application.screen.query_one("#index-status", Static).renderable),
                description="initial repository index status",
            )
            screen = application.screen
            assert "READY" in str(screen.query_one("#index-status", Static).renderable)
            assert "Documents: 2" in str(screen.query_one("#index-metadata", Static).renderable)

            screen.query_one("#repository-picker", RepositorySelect).value = "second"
            await wait_for_tui(
                pilot,
                lambda: "STALE" in str(screen.query_one("#index-status", Static).renderable),
                description="selected repository index status",
            )
            assert "STALE" in str(screen.query_one("#index-status", Static).renderable)
            screen.query_one("#refresh-index", Button).press()
            await wait_for_tui(
                pilot,
                lambda: calls == [("refresh", "second")]
                and "READY" in str(screen.query_one("#index-status", Static).renderable),
                description="repository index refresh result",
            )
            assert calls == [("refresh", "second")]
            assert "READY" in str(screen.query_one("#index-status", Static).renderable)

    asyncio.run(drive())


def test_workflow_repository_selector_refreshes_after_repository_settings_save(
    tmp_path: Path,
) -> None:
    controller, _, _ = _controller(tmp_path)
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test(size=(120, 60)) as pilot:
            await pilot.press("n")
            await settle_tui(pilot)
            workflow = application.screen
            workflow.query_one("#workflow-repositories", Button).press()
            await settle_tui(pilot)
            settings = application.screen
            assert isinstance(settings, RepositorySettingsScreen)

            settings.query_one("#add-repository", Button).press()
            await settle_tui(pilot)
            assert settings._editing_key is None
            settings.query_one("#repository-key", Input).value = "new"
            settings.query_one("#repository-path", Input).value = "new-path"
            settings.query_one("#save-repository", Button).press()
            await settle_tui(pilot)
            await pilot.press("escape")
            await settle_tui(pilot)

            assert application.screen is workflow
            repository = workflow.query_one("#repository", RepositorySelect)
            assert [(str(label), value) for label, value in repository._options] == [
                ("example", "example"),
                ("new", "new"),
            ]
            assert repository.value == "new"

    asyncio.run(drive())


def test_tui_buttons_activate_on_initial_click(tmp_path: Path) -> None:
    """A pointer click should activate an action without a focus-setting click first."""

    controller, _, _ = _controller(tmp_path)
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test(size=(120, 50)) as pilot:
            await pilot.click("#new-workflow")
            await wait_for_tui(
                pilot,
                lambda: isinstance(application.screen, WorkflowRunnerScreen),
                description="workflow runner after first click",
            )
            await pilot.click("#workflow-history")
            await wait_for_tui(
                pilot,
                lambda: isinstance(application.screen, ExecutionHistoryScreen),
                description="workflow history after first click",
            )

    asyncio.run(drive())


def test_select_opens_on_first_click_and_restores_focus_when_closed(tmp_path: Path) -> None:
    controller, _, _ = _controller(tmp_path)
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test(size=(120, 50)) as pilot:
            await pilot.press("n")
            await settle_tui(pilot)
            screen = application.screen
            select = screen.query_one("#repository", Select)
            await pilot.click("#repository")
            assert select.expanded is True
            await pilot.press("escape")
            assert select.expanded is False
            assert screen.focused is select

    asyncio.run(drive())


def test_orchestrator_select_keeps_overlay_visible_in_scrolled_viewport() -> None:
    """A click or key press must reserve room for an overlay near a viewport edge."""

    application = _ScrollableSelectApp()

    async def drive() -> None:
        async with application.run_test(size=(80, 10)) as pilot:
            content = application.query_one("#select-viewport", VerticalScroll)
            select = application.query_one("#viewport-select", OrchestratorSelect)
            await settle_tui(pilot)
            assert content.max_scroll_y > 0

            before_open = content.scroll_y
            await pilot.click("#viewport-select")
            await settle_tui(pilot)
            overlay = select.query_one("SelectOverlay")
            assert select.expanded is True
            assert content.scroll_y >= before_open
            assert overlay.region.y >= content.content_region.y
            assert overlay.region.bottom <= content.content_region.bottom

            opened_scroll = content.scroll_y
            await pilot.press("escape")
            await settle_tui(pilot)
            assert select.expanded is False
            assert application.screen.focused is select
            assert content.scroll_y == opened_scroll

            await pilot.press("enter", "down", "enter")
            await settle_tui(pilot)
            assert select.value == "one"
            assert application.screen.focused is select

    asyncio.run(drive())


def test_select_focus_scrolls_the_control_into_view_on_small_terminals(tmp_path: Path) -> None:
    controller, _, _ = _controller(tmp_path)
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test(size=(80, 20)) as pilot:
            await pilot.press("n")
            await settle_tui(pilot)
            screen = application.screen
            screen.query_one("#planner-configure", Button).press()
            await settle_tui(pilot)
            select = screen.query_one("#planner-provider", Select)
            select.focus()
            await settle_tui(pilot)
            content = screen.query_one("#workflow-runner", VerticalScroll)
            await pilot.press("enter")
            await settle_tui(pilot)
            overlay = select.query_one("SelectOverlay")
            assert content.scroll_y > 0
            assert overlay.region.y >= screen.region.y
            assert overlay.region.bottom <= screen.region.bottom

    asyncio.run(drive())


def test_github_app_compact_summary_hides_identifiers() -> None:
    configured = GitHubAppSettings(
        app_id=12,
        installation_id=34,
        private_key_env="APP_KEY",
    )
    unconfigured = GitHubAppSettings()

    assert WorkflowRunnerScreen.github_app_summary(configured, configured, configured) == (
        "GitHub Apps: Planner ✓ · Implementer ✓ · Reviewer ✓"
    )
    assert WorkflowRunnerScreen.github_app_summary(unconfigured, configured, unconfigured) == (
        "GitHub Apps: Planner — · Implementer ✓ · Reviewer —"
    )
    assert "12" not in WorkflowRunnerScreen.github_app_summary(
        configured, configured, unconfigured
    )


def test_format_diagnostics_for_export_keeps_sections_metadata_and_redaction() -> None:
    timestamp = datetime(2026, 9, 8, 20, 30, tzinfo=UTC)
    diagnostics = [
        ExecutionDiagnostic(
            id=index,
            execution_id="execution-1",
            category=category,
            stage=WorkflowStage.IMPLEMENT,
            role=AgentRole.IMPLEMENTER,
            occurred_at=timestamp,
            payload={"summary": f"{category.value.lower()} payload", "token": "not-safe"},
        )
        for index, category in enumerate(DiagnosticCategory, start=1)
    ]

    rendered = format_diagnostics_for_export(diagnostics)

    for header in ("REQUEST", "RESPONSE", "VALIDATION", "ERROR", "WORKFLOW EVENT"):
        assert f"=== {header} ===" in rendered
    assert "Timestamp: 2026-09-08T20:30:00+00:00" in rendered
    assert "Stage: IMPLEMENT" in rendered
    assert "Role: implementer" in rendered
    assert '"summary": "request payload"' in rendered
    assert '"token": "[REDACTED]"' in rendered
    assert "not-safe" not in rendered


def test_format_diagnostics_for_export_collapses_legacy_duplicate_agent_prompts() -> None:
    prompt = "Authorization: Bearer very-secret\nunique agent prompt"
    rendered = format_diagnostics_for_export(
        [
            ExecutionDiagnostic(
                execution_id="execution-1",
                category=DiagnosticCategory.REQUEST,
                payload={
                    "effective_instructions": prompt,
                    "instructions": prompt,
                },
            )
        ]
    )

    assert rendered.count("unique agent prompt") == 1
    assert '"effective_instructions"' in rendered
    assert '"instructions"' not in rendered
    assert "very-secret" not in rendered
    assert "[REDACTED]" in rendered


def test_format_diagnostics_for_export_keeps_distinct_agent_prompt_fields() -> None:
    rendered = format_diagnostics_for_export(
        [
            ExecutionDiagnostic(
                execution_id="execution-1",
                category=DiagnosticCategory.REQUEST,
                payload={
                    "effective_instructions": "final prompt sent to provider",
                    "instructions": "prompt before provider-specific assembly",
                },
            )
        ]
    )

    assert '"effective_instructions"' in rendered
    assert '"instructions"' in rendered
    assert "final prompt sent to provider" in rendered
    assert "prompt before provider-specific assembly" in rendered


def test_format_diagnostics_for_export_supports_single_agent_prompt_fields() -> None:
    for payload, prompt in (
        ({"effective_instructions": "canonical prompt"}, "canonical prompt"),
        ({"instructions": "legacy prompt"}, "legacy prompt"),
    ):
        rendered = format_diagnostics_for_export(
            [
                ExecutionDiagnostic(
                    execution_id="execution-1",
                    category=DiagnosticCategory.REQUEST,
                    payload=payload,
                )
            ]
        )
        assert rendered.count(prompt) == 1


def test_workflow_runner_copies_complete_live_diagnostics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller, _, _ = _controller(tmp_path)
    application = OrchestratorApp(controller)
    copied: list[str] = []
    monkeypatch.setattr(application, "copy_to_clipboard", copied.append)

    async def drive() -> None:
        async with application.run_test(size=(120, 50)) as pilot:
            application.push_screen(WorkflowRunnerScreen())
            await settle_tui(pilot)
            screen = application.screen
            payload = "\n".join(f"request line {number}" for number in range(400))
            screen._render_workflow_event(
                WorkflowEvent(
                    execution_id="execution-1",
                    kind=WorkflowEventKind.DIAGNOSTIC,
                    category=DiagnosticCategory.REQUEST.value,
                    stage=WorkflowStage.IMPLEMENT,
                    payload={"instructions": payload, "authorization": "Bearer private-token"},
                )
            )
            copy_button = screen.query_one("#copy-detailed-logs-button", Button)
            assert not copy_button.disabled
            copy_button.press()
            await settle_tui(pilot)

    asyncio.run(drive())

    assert len(copied) == 1
    assert "=== REQUEST ===" in copied[0]
    assert "request line 399" in copied[0]
    assert "private-token" not in copied[0]
    assert '"authorization": "[REDACTED]"' in copied[0]


def test_workflow_runner_detailed_logs_expand_the_main_scroll_region_and_collapse_cleanly(
    tmp_path: Path,
) -> None:
    controller, _, _ = _controller(tmp_path)
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test(size=(120, 200)) as pilot:
            application.push_screen(WorkflowRunnerScreen())
            await settle_tui(pilot)
            screen = application.screen
            content = screen.query_one("#workflow-runner", VerticalScroll)
            raw_events = screen.query_one("#workflow-raw-events", Static)
            raw_events_title = screen.query_one("#workflow-raw-events-title", Static)
            assert raw_events.display is False
            assert raw_events_title.display is False
            assert content.max_scroll_y == 0

            started = WorkflowEvent(
                execution_id="execution-1",
                kind=WorkflowEventKind.STAGE_STARTED,
                stage=WorkflowStage.IMPLEMENT,
                step_status=StepStatus.RUNNING,
            )
            finished = WorkflowEvent(
                execution_id="execution-1",
                kind=WorkflowEventKind.STAGE_FINISHED,
                stage=WorkflowStage.IMPLEMENT,
                step_status=StepStatus.SUCCEEDED,
            )
            screen._render_workflow_event(started)
            screen._render_workflow_event(
                WorkflowEvent(
                    execution_id="execution-1",
                    kind=WorkflowEventKind.AGENT_FALLBACK,
                    stage=WorkflowStage.IMPLEMENT,
                    message="Implementer switched from primary to backup/cloud",
                    payload={"role": "implementer", "fallback_preset": "cloud"},
                )
            )
            screen._render_workflow_event(finished)

            timeline = str(screen.query_one("#workflow-events", Static).renderable)
            assert "✓ Changes implemented" in timeline
            assert "primary to backup/cloud" in timeline
            assert "STAGE_STARTED" not in timeline
            assert "STAGE_FINISHED" not in timeline

            # Include a diagnostic large enough to exceed the otherwise tall
            # viewport. It must grow the outer page rather than a RichLog-sized
            # nested vertical viewport.
            screen._raw_event_lines.append(
                "REQUEST\n" + "\n".join(f"prompt line {number}" for number in range(300))
            )
            screen.query_one("#toggle-workflow-logs", Button).press()
            await settle_tui(pilot)
            assert raw_events.display is True
            assert raw_events_title.display is True
            assert (
                screen.query_one("#toggle-workflow-logs", Button).label.plain
                == "Hide detailed logs"
            )
            assert raw_events.size.height > 200
            assert content.max_scroll_y > 0
            content.scroll_end(animate=False)
            await settle_tui(pilot)
            assert content.scroll_y > 0
            raw_log = str(raw_events.renderable)
            assert "STAGE_STARTED" in raw_log
            assert "STAGE_FINISHED" in raw_log
            assert "AGENT_FALLBACK" in raw_log
            assert "REQUEST" in raw_log

            screen.query_one("#toggle-workflow-logs", Button).press()
            await settle_tui(pilot)
            assert raw_events.display is False
            assert raw_events_title.display is False
            assert (
                screen.query_one("#toggle-workflow-logs", Button).label.plain
                == "Show detailed logs"
            )
            assert content.max_scroll_y == 0

    asyncio.run(drive())


def test_workflow_runner_shows_only_supported_runtime_controls(tmp_path: Path) -> None:
    controller, _, _ = _controller(tmp_path)
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test() as pilot:
            await pilot.press("n")
            screen = application.screen
            repository = screen.query_one("#repository")
            repository.value = "example"
            await settle_tui(pilot)
            assert screen.query_one("#planner-config").display is False
            assert screen.query_one("#planner-advanced").display is False
            assert "planner (Provider/YAML default)" in str(
                screen.query_one("#planner-summary").renderable
            )
            assert screen.query_one("#planner-provider").value == "planner"
            assert screen.query_one("#planner-model").display is True
            assert screen.query_one("#planner-reasoning").display is False
            assert screen.query_one("#implementer-model").display is True
            assert screen.query_one("#implementer-reasoning").display is True
            assert screen.query_one("#implementer-model").value == ""

            screen.query_one("#planner-provider").value = "implementer"
            await settle_tui(pilot)
            assert screen.query_one("#planner-model").display is True
            assert screen.query_one("#planner-reasoning").display is True

    asyncio.run(drive())


def test_workflow_runner_keeps_claude_permission_modes_and_effort_role_local(
    tmp_path: Path,
) -> None:
    controller, _, _ = _controller(tmp_path)
    config = controller.project_config("example")
    config.providers["claude"] = AgentProviderSettings.model_validate(
        {"type": "claude-code", "effort": "medium", "permission_mode": "dontAsk"}
    )
    for role in ("planner", "implementer", "reviewer"):
        getattr(config.agents, role).provider = "claude"
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test() as pilot:
            await pilot.press("n")
            screen = application.screen
            screen.query_one("#repository").value = "example"
            await settle_tui(pilot)
            for prefix in ("planner", "implementer", "reviewer"):
                assert screen.query_one(f"#{prefix}-reasoning").display is True
                assert screen.query_one(f"#{prefix}-permission").display is True
                assert "Provider/YAML default (dontAsk)" in str(
                    screen.query_one(f"#{prefix}-summary", Static).renderable
                )

            screen.query_one("#planner-reasoning", Input).value = "low"
            screen.query_one("#implementer-reasoning", Input).value = "high"
            screen.query_one("#reviewer-reasoning", Input).value = "max"
            screen.query_one("#implementer-permission").value = "acceptEdits"
            screen.query_one("#reviewer-permission").value = "auto"
            await settle_tui(pilot)
            assert screen._runtime_options_for_role(AgentRole.PLANNER, "planner") == {
                "model": None,
                "reasoning_effort": "low",
            }
            assert screen._runtime_options_for_role(AgentRole.IMPLEMENTER, "implementer") == {
                "model": None,
                "reasoning_effort": "high",
                "permission_mode": "acceptEdits",
            }
            assert screen._runtime_options_for_role(AgentRole.REVIEWER, "reviewer") == {
                "model": None,
                "reasoning_effort": "max",
                "permission_mode": "auto",
            }

    asyncio.run(drive())


def test_workflow_runner_clarifies_configured_fallback_during_primary_override(
    tmp_path: Path,
) -> None:
    controller, _, _ = _controller(tmp_path)
    config = controller.project_config("example")
    config.agent_presets = {
        "implementer": {"backup": AgentPresetConfig(provider="vertex", model="fallback-model")}
    }
    config.agents.implementer.fallback_preset = "backup"
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test() as pilot:
            await pilot.press("n")
            await settle_tui(pilot)
            summary = application.screen.query_one("#implementer-summary", Static)
            assert "fallback: vertex/backup" in str(summary.renderable)
            assert "repository setting; unchanged" not in str(summary.renderable)

            application.screen.query_one("#implementer-model", Input).value = "temporary-model"
            await settle_tui(pilot)

            assert "fallback: vertex/backup" in str(summary.renderable)
            assert "repository setting; unchanged" in str(summary.renderable)

    asyncio.run(drive())


def test_workflow_runner_selects_role_local_presets_without_crud_controls(tmp_path: Path) -> None:
    controller, service, _ = _controller(tmp_path)
    config = controller.project_config("example")
    config.agent_presets = {
        "implementer": {
            "deep": AgentPresetConfig(
                provider="implementer", model="preset-model", reasoning_effort="xhigh"
            )
        },
        "reviewer": {"review": AgentPresetConfig(provider="reviewer")},
    }
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test() as pilot:
            await pilot.press("n")
            await settle_tui(pilot)
            screen = application.screen
            screen.query_one("#implementer-preset", Select).value = "deep"
            await settle_tui(pilot)
            assert list(screen.query("#implementer-preset-name")) == []
            assert list(screen.query("#implementer-preset-create")) == []
            assert list(screen.query("#implementer-preset-update")) == []
            assert list(screen.query("#implementer-preset-rename")) == []
            assert list(screen.query("#implementer-preset-delete")) == []
            screen.query_one("#implementer-configure", Button).press()
            assert screen.query_one("#implementer-model", Input).value == "preset-model"
            assert screen.query_one("#implementer-reasoning", Input).value == "xhigh"
            assert "Selected preset: deep" in str(
                screen.query_one("#implementer-preset-source", Static).renderable
            )
            screen.query_one("#implementer-model", Input).value = "temporary-model"
            await settle_tui(pilot)
            assert screen.query_one("#implementer-preset", Select).value != "deep"
            assert "Manual configuration; temporary this-run overrides" in str(
                screen.query_one("#implementer-preset-source", Static).renderable
            )
            screen.query_one("#task-checkbox-0", Checkbox).value = True
            screen.query_one("#start-workflow", Button).press()
            await settle_tui(pilot)
            assert service.calls[-1][3][AgentRole.IMPLEMENTER] == {
                "model": "temporary-model",
                "reasoning_effort": "xhigh",
            }
            assert config.agent_presets["implementer"]["deep"].model == "preset-model"

    asyncio.run(drive())


def test_workflow_runner_preset_omitting_claude_effort_inherits_provider_default(
    tmp_path: Path,
) -> None:
    controller, service, _ = _controller(tmp_path)
    config = controller.project_config("example")
    config.providers["claude"] = AgentProviderSettings.model_validate(
        {"type": "claude-code", "effort": "medium", "permission_mode": "dontAsk"}
    )
    config.agent_presets = {
        "planner": {
            "edit": AgentPresetConfig(provider="claude", permission_mode="acceptEdits")
        }
    }
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test() as pilot:
            await pilot.press("n")
            await settle_tui(pilot)
            screen = application.screen
            screen.query_one("#planner-preset", Select).value = "edit"
            await settle_tui(pilot)

            assert screen.query_one("#planner-reasoning", Input).value == "medium"
            assert "Selected preset: edit" in str(
                screen.query_one("#planner-preset-source", Static).renderable
            )
            screen.query_one("#task-checkbox-0", Checkbox).value = True
            screen.query_one("#start-workflow", Button).press()
            await settle_tui(pilot)
            assert service.calls[-1][3][AgentRole.PLANNER]["reasoning_effort"] == "medium"

    asyncio.run(drive())


def test_workflow_runner_names_preset_on_provider_with_uneditable_generation(
    tmp_path: Path,
) -> None:
    """A provider generation key without a control must not hide the preset name.

    Generation options the launcher cannot edit are inherited unchanged by
    every run, so a preset selected on such a provider still describes the
    configured controls exactly.
    """

    controller, _, _ = _controller(tmp_path)
    config = controller.project_config("example")
    config.providers["ollama"] = AgentProviderSettings.model_validate(
        {
            "type": "litellm",
            "model": "ollama/gemma4:e4b",
            "temperature": 0.2,
            "generation": {"num_ctx": 32768},
        }
    )
    config.agent_presets = {
        "planner": {"coder": AgentPresetConfig(provider="ollama", model="ollama/qwen2.5-coder:14b")}
    }
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test() as pilot:
            await pilot.press("n")
            await settle_tui(pilot)
            screen = application.screen
            screen.query_one("#planner-preset", Select).value = "coder"
            await settle_tui(pilot)

            assert screen.query_one("#planner-model", Input).value == "ollama/qwen2.5-coder:14b"
            assert "Selected preset: coder" == str(
                screen.query_one("#planner-preset-source", Static).renderable
            )
            assert "preset: coder)" in str(
                screen.query_one("#planner-summary", Static).renderable
            )

    asyncio.run(drive())


def test_workflow_runner_applies_and_resets_reviewer_preset_independently(tmp_path: Path) -> None:
    controller, _, _ = _controller(tmp_path)
    controller.project_config("example").agent_presets = {
        "implementer": {"deep": AgentPresetConfig(provider="implementer", model="build")},
        "reviewer": {
            "thorough": AgentPresetConfig(
                provider="vertex",
                model="review-model",
                generation={"temperature": "0.4", "max_output_tokens": "600"},
            )
        },
    }
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test() as pilot:
            await pilot.press("n")
            await settle_tui(pilot)
            screen = application.screen
            reviewer_preset = screen.query_one("#reviewer-preset", Select)
            assert {option[1] for option in reviewer_preset._options} == {
                reviewer_preset._options[0][1],
                "thorough",
            }
            assert "deep" not in {option[1] for option in reviewer_preset._options}

            reviewer_preset.value = "thorough"
            await settle_tui(pilot)
            assert screen.query_one("#reviewer-provider", Select).value == "vertex"
            assert screen.query_one("#reviewer-model", Input).value == "review-model"
            assert screen.query_one("#reviewer-temperature", Input).value == "0.4"

            reviewer_preset.value = reviewer_preset._options[0][1]
            await settle_tui(pilot)
            assert screen.query_one("#reviewer-provider", Select).value == "reviewer"
            assert screen.query_one("#reviewer-model", Input).value == "test/model"
            assert screen.query_one("#reviewer-temperature", Input).value == ""
            assert "Provider/YAML default (manual)" in str(
                screen.query_one("#reviewer-preset-source", Static).renderable
            )

    asyncio.run(drive())


def test_agent_presets_screen_manages_all_role_presets(tmp_path: Path) -> None:
    controller, _, _ = _controller(tmp_path)
    config = controller.project_config("example")
    config.agent_presets = {
        "implementer": {"deep": AgentPresetConfig(provider="implementer", model="old")}
    }
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test() as pilot:
            await pilot.press("p")
            await settle_tui(pilot)
            assert isinstance(application.screen, AgentPresetsScreen)
            screen = application.screen
            repository = screen.query_one("#agent-presets-repository", Select)
            repository.value = "example"
            await settle_tui(pilot)
            role = screen.query_one("#agent-preset-role", Select)
            assert {option[1] for option in role._options} == {
                "planner", "implementer", "reviewer"
            }

            role.value = "implementer"
            await settle_tui(pilot)
            screen.query_one("#agent-preset-select", Select).value = "deep"
            await settle_tui(pilot)
            fallback = screen.query_one("#agent-fallback-preset", Select)
            fallback.value = "deep"
            screen.query_one("#save-agent-fallback", Button).press()
            await settle_tui(pilot)
            assert config.agents.implementer.fallback_preset == "deep"
            screen.query_one("#agent-preset-model", Input).value = "updated"
            screen.query_one("#update-agent-preset", Button).press()
            await settle_tui(pilot)
            assert config.agent_presets["implementer"]["deep"].model == "updated"

            screen.query_one("#agent-preset-name", Input).value = "renamed"
            screen.query_one("#rename-agent-preset", Button).press()
            await settle_tui(pilot)
            assert "renamed" in config.agent_presets["implementer"]
            fallback.value = fallback._options[0][1]
            screen.query_one("#save-agent-fallback", Button).press()
            await settle_tui(pilot)
            assert config.agents.implementer.fallback_preset is None
            screen.query_one("#delete-agent-preset", Button).press()
            await settle_tui(pilot)
            assert "implementer" not in config.agent_presets

            role.value = "planner"
            await settle_tui(pilot)
            screen.query_one("#agent-preset-name", Input).value = "fast"
            screen.query_one("#agent-preset-provider", Select).value = "planner"
            screen.query_one("#create-agent-preset", Button).press()
            await settle_tui(pilot)
            assert config.agent_presets["planner"]["fast"].provider == "planner"

            role.value = "reviewer"
            await settle_tui(pilot)
            preset_selector = screen.query_one("#agent-preset-select", Select)
            assert "fast" not in {option[1] for option in preset_selector._options}
            screen.query_one("#agent-preset-name", Input).value = "focused"
            screen.query_one("#agent-preset-provider", Select).value = "reviewer"
            screen.query_one("#create-agent-preset", Button).press()
            await settle_tui(pilot)
            assert config.agent_presets["reviewer"]["focused"].provider == "reviewer"
            screen.query_one("#agent-preset-name", Input).value = "focused-review"
            screen.query_one("#rename-agent-preset", Button).press()
            await settle_tui(pilot)
            assert "focused-review" in config.agent_presets["reviewer"]
            screen.query_one("#delete-agent-preset", Button).press()
            await settle_tui(pilot)
            assert "reviewer" not in config.agent_presets
            assert "fast" in config.agent_presets["planner"]

    asyncio.run(drive())


def test_agent_presets_screen_manages_claude_permission_mode_and_effort(tmp_path: Path) -> None:
    controller, _, _ = _controller(tmp_path)
    config = controller.project_config("example")
    config.providers["claude"] = AgentProviderSettings.model_validate(
        {"type": "claude-code", "effort": "medium", "permission_mode": "dontAsk"}
    )
    config.agents.planner.provider = "claude"
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test() as pilot:
            await pilot.press("p")
            await settle_tui(pilot)
            screen = application.screen
            screen.query_one("#agent-presets-repository", Select).value = "example"
            await settle_tui(pilot)

            permission = screen.query_one("#agent-preset-permission", OrchestratorSelect)
            reasoning = screen.query_one("#agent-preset-reasoning", Input)
            assert permission.display is True
            assert reasoning.display is True
            assert "Provider/YAML default (dontAsk)" in {
                option[0] for option in permission._options
            }
            permission.value = "acceptEdits"
            reasoning.value = "high"
            screen.query_one("#agent-preset-name", Input).value = "editing"
            screen.query_one("#create-agent-preset", Button).press()
            await settle_tui(pilot)
            assert config.agent_presets["planner"]["editing"].permission_mode == "acceptEdits"
            assert config.agent_presets["planner"]["editing"].reasoning_effort == "high"

            screen.query_one("#agent-preset-provider", Select).value = "planner"
            await settle_tui(pilot)
            assert permission.display is False

            screen.query_one("#agent-preset-provider", Select).value = "claude"
            await settle_tui(pilot)
            assert permission.display is True
            assert reasoning.display is True
            assert permission.value == "acceptEdits"
            assert reasoning.value == "high"

            # A delayed provider event from an earlier switch must not reload
            # permission controls for a provider the selector no longer names.
            provider_select = screen.query_one("#agent-preset-provider", OrchestratorSelect)
            screen.on_select_changed(OrchestratorSelect.Changed(provider_select, "planner"))
            await settle_tui(pilot)
            assert permission.display is True
            assert permission.value == "acceptEdits"

            permission.value = permission._options[0][1]
            screen.query_one("#update-agent-preset", Button).press()
            await settle_tui(pilot)
            assert config.agent_presets["planner"]["editing"].permission_mode is None

    asyncio.run(drive())


def test_agent_presets_screen_omits_hidden_fields_when_switching_provider(
    tmp_path: Path,
) -> None:
    controller, _, _ = _controller(tmp_path)
    config = controller.project_config("example")
    config.providers["claude"] = AgentProviderSettings.model_validate(
        {"type": "claude-code", "effort": "medium", "permission_mode": "dontAsk"}
    )
    config.agents.planner.provider = "claude"
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test() as pilot:
            await pilot.press("p")
            await settle_tui(pilot)
            screen = application.screen
            screen.query_one("#agent-presets-repository", Select).value = "example"
            await settle_tui(pilot)

            model = screen.query_one("#agent-preset-model", Input)
            reasoning = screen.query_one("#agent-preset-reasoning", Input)
            permission = screen.query_one("#agent-preset-permission", OrchestratorSelect)
            temperature = screen.query_one("#agent-preset-temperature", Input)
            max_tokens = screen.query_one("#agent-preset-max-output-tokens", Input)

            model.value = "claude-model"
            reasoning.value = "high"
            permission.value = "acceptEdits"
            screen.query_one("#agent-preset-name", Input).value = "mixed"
            screen.query_one("#create-agent-preset", Button).press()
            await settle_tui(pilot)
            saved = config.agent_presets["planner"]["mixed"]
            assert saved.provider == "claude"
            assert saved.model == "claude-model"
            assert saved.reasoning_effort == "high"
            assert saved.permission_mode == "acceptEdits"
            assert saved.generation == {}

            # Switch to a generation-capable provider that hides reasoning and
            # permission but still exposes model; hidden values must not leak
            # into the saved preset.
            screen.query_one("#agent-preset-provider", Select).value = "vertex"
            await settle_tui(pilot)
            assert reasoning.display is False
            assert permission.display is False
            assert model.display is True
            assert temperature.display is True
            assert max_tokens.display is True

            temperature.value = "0.5"
            max_tokens.value = "300"
            screen.query_one("#update-agent-preset", Button).press()
            await settle_tui(pilot)
            saved = config.agent_presets["planner"]["mixed"]
            assert saved.provider == "vertex"
            assert saved.model == "claude-model"
            assert saved.reasoning_effort is None
            assert saved.permission_mode is None
            assert saved.generation == {"temperature": "0.5", "max_output_tokens": "300"}
            config.with_agent_runtime_overrides(
                {"planner": saved.provider}, {"planner": saved.runtime_options()}
            )

            # Switch to another API provider; reasoning and permission remain hidden.
            screen.query_one("#agent-preset-provider", Select).value = "planner"
            await settle_tui(pilot)
            assert model.display is True
            assert reasoning.display is False
            assert permission.display is False
            assert temperature.display is True
            assert max_tokens.display is True

            screen.query_one("#update-agent-preset", Button).press()
            await settle_tui(pilot)
            saved = config.agent_presets["planner"]["mixed"]
            assert saved.provider == "planner"
            assert saved.model == "claude-model"
            assert saved.reasoning_effort is None
            assert saved.permission_mode is None
            assert saved.generation == {"temperature": "0.5", "max_output_tokens": "300"}
            config.with_agent_runtime_overrides(
                {"planner": saved.provider}, {"planner": saved.runtime_options()}
            )

    asyncio.run(drive())


def test_agent_presets_screen_reports_invalid_claude_permission_default(tmp_path: Path) -> None:
    controller, _, _ = _controller(tmp_path)
    config = controller.project_config("example")
    config.providers["claude"] = AgentProviderSettings.model_validate(
        {"type": "claude-code", "permission_mode": "invalid"}
    )
    config.agents.planner.provider = "claude"
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test() as pilot:
            await pilot.press("p")
            await settle_tui(pilot)
            screen = application.screen
            screen.query_one("#agent-presets-repository", Select).value = "example"
            await settle_tui(pilot)

            assert screen.query_one(
                "#agent-preset-permission", OrchestratorSelect
            ).display is False
            status = str(screen.query_one("#agent-presets-status", Static).renderable)
            assert "Cannot load permission default for provider 'claude'" in status
            assert "permission_mode" in status

    asyncio.run(drive())


def test_agent_presets_role_selector_shows_all_roles_and_supports_keyboard_and_mouse(
    tmp_path: Path,
) -> None:
    controller, _, _ = _controller(tmp_path)
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test(size=(80, 24)) as pilot:
            await pilot.press("p")
            await settle_tui(pilot)
            screen = application.screen
            role = screen.query_one("#agent-preset-role", Select)

            assert role.value == "planner"
            assert [option[0] for option in role._options] == [
                "Planner",
                "Implementer",
                "Reviewer",
            ]

            role.focus()
            await pilot.press("enter", "down", "enter")
            await settle_tui(pilot)
            assert role.value == "implementer"
            assert "Implementer presets loaded" in str(
                screen.query_one("#agent-presets-status", Static).renderable
            )

            await pilot.click("#agent-preset-role")
            await settle_tui(pilot)
            overlay = role.query_one("SelectOverlay")
            assert role.expanded is True
            # The menu includes a top and bottom border plus all three rows.
            assert overlay.region.height >= len(role._options) + 2

            # The third option is visible and selectable directly by mouse.
            await pilot.click(overlay, offset=(2, 3))
            await settle_tui(pilot)
            assert role.value == "reviewer"
            assert role.expanded is False
            assert "Reviewer presets loaded" in str(
                screen.query_one("#agent-presets-status", Static).renderable
            )

    asyncio.run(drive())


def test_workflow_runner_preselects_matching_configured_preset(tmp_path: Path) -> None:
    controller, _, _ = _controller(tmp_path)
    controller.project_config("example").agent_presets = {
        "implementer": {"configured": AgentPresetConfig(provider="implementer")}
    }
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test() as pilot:
            await pilot.press("n")
            await settle_tui(pilot)
            screen = application.screen
            assert screen.query_one("#implementer-preset", Select).value == "configured"
            assert screen.query_one("#planner-preset", Select).value is not None

    asyncio.run(drive())


def test_workflow_runner_warns_when_a_preset_provider_is_missing(tmp_path: Path) -> None:
    controller, _, _ = _controller(tmp_path)
    controller.project_config("example").agent_presets = {
        "planner": {"retired": AgentPresetConfig(provider="missing-provider")}
    }
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test() as pilot:
            await pilot.press("n")
            await settle_tui(pilot)
            screen = application.screen
            screen.query_one("#planner-configure", Button).press()
            screen.query_one("#planner-preset", Select).value = "retired"
            await settle_tui(pilot)
            assert "unavailable provider" in str(
                screen.query_one("#planner-preset-source", Static).renderable
            )
            assert screen.query_one("#planner-provider", Select).value == "planner"

    asyncio.run(drive())


def test_workflow_runner_expands_advanced_controls_and_preserves_overrides(
    tmp_path: Path,
) -> None:
    controller, service, _ = _controller(tmp_path)
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test(size=(120, 50)) as pilot:
            await pilot.press("n")
            await settle_tui(pilot)
            screen = application.screen
            configure = screen.query_one("#planner-configure", Button)
            configure.focus()
            await pilot.press("enter")
            assert screen.query_one("#planner-config").display is True

            screen.query_one("#planner-provider", Select).value = "vertex"
            await settle_tui(pilot)
            assert "vertex (this-run provider override)" in str(
                screen.query_one("#planner-summary").renderable
            )
            assert screen.query_one("#planner-advanced-toggle", Button).display is True
            assert screen.query_one("#planner-advanced").display is False

            advanced = screen.query_one("#planner-advanced-toggle", Button)
            advanced.press()
            await settle_tui(pilot)
            assert screen.query_one("#planner-advanced").display is True
            assert screen.query_one("#planner-temperature", Input).value == "0.2"
            assert "Provider/YAML default" in str(
                screen.query_one("#planner-temperature-source").renderable
            )
            assert "Provider/YAML default" in str(
                screen.query_one("#planner-max-output-tokens-source").renderable
            )

            screen.query_one("#planner-temperature", Input).value = "0.8"
            screen.query_one("#planner-max-output-tokens", Input).value = "512"
            await settle_tui(pilot)
            assert "This-run override" in str(
                screen.query_one("#planner-temperature-source").renderable
            )

            advanced.press()
            await settle_tui(pilot)
            assert screen.query_one("#planner-advanced").display is False
            assert screen.query_one("#planner-temperature", Input).value == "0.8"

            screen.query_one("#task-checkbox-0", Checkbox).value = True
            await settle_tui(pilot)
            screen.query_one("#start-workflow", Button).focus()
            await pilot.press("enter")
            await settle_tui(pilot)
            assert service.calls[0][3][AgentRole.PLANNER]["generation"] == {
                "temperature": "0.8",
                "max_output_tokens": "512",
            }

    asyncio.run(drive())


def test_workflow_runner_role_configuration_uses_responsive_rows(tmp_path: Path) -> None:
    controller, _, _ = _controller(tmp_path)

    async def drive() -> None:
        narrow_application = OrchestratorApp(controller)
        async with narrow_application.run_test(size=(70, 50)) as pilot:
            await pilot.press("n")
            await settle_tui(pilot)
            screen = narrow_application.screen
            screen.query_one("#implementer-configure", Button).focus()
            await pilot.press("enter")
            row = next(iter(screen.query(".role-config-row")))
            content = screen.query_one("#workflow-runner", VerticalScroll)
            assert screen.has_class("-narrow")
            assert row.size.width <= screen.size.width
            assert content.max_scroll_x == 0

        wide_application = OrchestratorApp(controller)
        async with wide_application.run_test(size=(120, 50)) as pilot:
            await pilot.press("n")
            await settle_tui(pilot)
            screen = wide_application.screen
            screen.query_one("#implementer-configure", Button).focus()
            await pilot.press("enter")
            row = next(iter(screen.query(".role-config-row")))
            assert screen.has_class("-wide")
            assert row.size.width <= screen.size.width

    asyncio.run(drive())


def test_workflow_runner_auto_loads_tasks_on_mount(tmp_path: Path) -> None:
    controller, _, _ = _controller(tmp_path)
    task_loads: list[tuple[str, str]] = []

    def list_tasks(*, repository: str, task_provider: str) -> list[DevelopmentTask]:
        task_loads.append((repository, task_provider))
        return [DevelopmentTask("42", task_provider, "Automatically loaded", "", "")]

    controller.list_tasks = list_tasks  # type: ignore[method-assign]
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test() as pilot:
            await pilot.press("n")
            await settle_tui(pilot)
            screen = application.screen
            assert task_loads == [("example", "github")]
            assert screen.query_one("#task-selector", TaskSelector).selected_task_ids == ()
            assert "Selected 0 of 1 available github tasks." in str(
                screen.query_one("#task-hint").renderable
            )
            assert len(screen.query("#task-id")) == 0

    asyncio.run(drive())


def test_workflow_runner_repository_selector_uses_configured_non_blank_options(
    tmp_path: Path,
) -> None:
    controller, _, _ = _controller(tmp_path)
    controller.repositories = lambda: [  # type: ignore[method-assign]
        ConfiguredRepository("example", tmp_path),
        ConfiguredRepository("other", tmp_path),
    ]
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test() as pilot:
            await pilot.press("n")
            await settle_tui(pilot)
            selector = application.screen.query_one("#repository", OrchestratorSelect)
            options = selector._options

            assert [(str(label), value) for label, value in options] == [
                ("example", "example"),
                ("other", "other"),
            ]
            assert selector.value == "example"
            assert selector.value in {value for _, value in options}
            assert all(str(label).strip() for label, _ in options)

            await pilot.click("#repository")
            assert selector.expanded is True
            assert selector.value == "example"
            await pilot.press("escape")
            assert selector.value == "example"

    asyncio.run(drive())


@pytest.mark.parametrize(
    ("key", "selector_id"),
    [
        ("n", "#repository"),
        ("c", "#github-app-repository"),
        ("i", "#agent-instructions-repository"),
    ],
)
def test_repository_select_populates_mounted_overlay_from_configured_repository(
    tmp_path: Path, key: str, selector_id: str
) -> None:
    """A resolved repository key must be a real, visible Select choice everywhere."""

    repository_key = "OrlyJunior/autonomous-development-orchestrator"
    controller, _, _ = _controller(tmp_path)
    controller.repositories = lambda: [  # type: ignore[method-assign]
        ConfiguredRepository(repository_key, tmp_path)
    ]
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test() as pilot:
            await pilot.press(key)
            await settle_tui(pilot)
            selector = application.screen.query_one(selector_id, RepositorySelect)
            overlay = selector.query_one("SelectOverlay")

            assert selector.value == repository_key
            assert selector._options == [(repository_key, repository_key)]
            assert len(overlay._options) == 1
            assert str(overlay._options[0].prompt) == repository_key

            await pilot.click(selector_id)
            assert selector.expanded is True
            assert len(overlay._options) == 1

    asyncio.run(drive())


def test_workflow_runner_reloads_tasks_on_repository_or_provider_change(tmp_path: Path) -> None:
    controller, _, _ = _controller(tmp_path)
    task_loads: list[tuple[str, str]] = []
    capabilities = {
        provider: controller.provider_runtime_capabilities("example", provider)
        for provider in ("planner", "implementer", "reviewer")
    }
    runtime_values = {
        provider: controller.provider_runtime_values("example", provider)
        for provider in ("planner", "implementer", "reviewer")
    }
    github_settings = controller.github_app_settings("example")
    agent_providers = controller.agent_providers("example")

    controller.repositories = lambda: [  # type: ignore[method-assign]
        ConfiguredRepository("example", tmp_path),
        ConfiguredRepository("other", tmp_path),
    ]
    controller.task_providers = (  # type: ignore[method-assign]
        lambda repository: ["github", "trello"]
    )
    controller.configured_agent_provider_options = (  # type: ignore[method-assign]
        lambda repository: [(provider, provider) for provider in capabilities]
    )
    controller.agent_providers = lambda repository: agent_providers  # type: ignore[method-assign]
    controller.agent_fallback_preset = (  # type: ignore[method-assign]
        lambda repository, role: None
    )
    controller.provider_runtime_capabilities = (  # type: ignore[method-assign]
        lambda repository, provider: capabilities[provider]
    )
    controller.provider_runtime_values = (  # type: ignore[method-assign]
        lambda repository, provider: runtime_values[provider]
    )
    controller.github_app_settings = (  # type: ignore[method-assign]
        lambda repository: github_settings
    )

    def list_tasks(*, repository: str, task_provider: str) -> list[DevelopmentTask]:
        task_loads.append((repository, task_provider))
        return []

    controller.list_tasks = list_tasks  # type: ignore[method-assign]
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test() as pilot:
            await pilot.press("n")
            await wait_for_tui(
                pilot,
                lambda: task_loads == [("example", "github")],
                description="initial task load",
            )
            screen = application.screen
            screen.query_one("#task-provider", Select).value = "trello"
            await wait_for_tui(
                pilot,
                lambda: task_loads == [("example", "github"), ("example", "trello")],
                description="task reload after provider change",
            )
            screen.query_one("#repository", Select).value = "other"
            await wait_for_tui(
                pilot,
                lambda: controller.selected_repository == "other"
                and task_loads
                == [("example", "github"), ("example", "trello"), ("other", "github")],
                description="task reload after repository change",
            )
            assert controller.selected_repository == "other"
            assert task_loads == [
                ("example", "github"),
                ("example", "trello"),
                ("other", "github"),
            ]
            screen.query_one("#workflow-agent-instructions", Button).press()
            await wait_for_tui(
                pilot,
                lambda: isinstance(application.screen, AgentInstructionsScreen)
                and application.screen.query_one(
                    "#agent-instructions-repository", OrchestratorSelect
                ).value
                == "other",
                description="agent instructions repository selection",
            )
            assert (
                application.screen.query_one(
                    "#agent-instructions-repository", OrchestratorSelect
                ).value
                == "other"
            )

    asyncio.run(drive())


def test_workflow_runner_lists_supported_provider_choices(tmp_path: Path) -> None:
    controller, _, _ = _controller(tmp_path)
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test() as pilot:
            await pilot.press("n")
            await settle_tui(pilot)
            selector = application.screen.query_one("#planner-provider", OrchestratorSelect)

            assert selector._options == [
                ("implementer", "implementer"),
                ("planner", "planner"),
                ("reviewer", "reviewer"),
                ("vertex", "vertex"),
            ]

    asyncio.run(drive())


def test_workflow_runner_manual_refresh_tasks(tmp_path: Path) -> None:
    controller, _, _ = _controller(tmp_path)
    task_loads: list[tuple[str, str]] = []

    def list_tasks(*, repository: str, task_provider: str) -> list[DevelopmentTask]:
        task_loads.append((repository, task_provider))
        return []

    controller.list_tasks = list_tasks  # type: ignore[method-assign]
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test() as pilot:
            await pilot.press("n")
            await settle_tui(pilot)
            screen = application.screen
            screen.query_one("#refresh-tasks", Button).focus()
            await pilot.press("enter")
            await settle_tui(pilot)
            assert task_loads == [("example", "github"), ("example", "github")]

    asyncio.run(drive())


def test_workflow_runner_displays_inline_task_load_error(tmp_path: Path) -> None:
    controller, _, _ = _controller(tmp_path)

    def list_tasks(*, repository: str, task_provider: str) -> list[DevelopmentTask]:
        del repository, task_provider
        raise RuntimeError("credentials rejected")

    controller.list_tasks = list_tasks  # type: ignore[method-assign]
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test() as pilot:
            await pilot.press("n")
            await settle_tui(pilot)
            screen = application.screen
            assert "credentials rejected" in str(
                screen.query_one("#task-error").renderable
            )
            assert screen.query_one("#refresh-tasks", Button).disabled is False

    asyncio.run(drive())


def test_tui_inspects_and_saves_independent_github_app_references(tmp_path: Path) -> None:
    controller, _, _ = _controller(tmp_path)
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test(size=(80, 20)) as pilot:
            await pilot.press("c")
            assert isinstance(application.screen, GitHubAppSettingsScreen)
            repository = application.screen.query_one("#github-app-repository")
            repository.value = "example"
            await settle_tui(pilot)
            assert application.screen.query_one("#planner-app-id").value == "90"
            assert application.screen.query_one("#implementer-app-id").value == "56"
            assert application.screen.query_one("#reviewer-app-id").value == "12"
            application.screen.query_one("#implementer-app-id").value = "123"
            application.screen.query_one("#implementer-installation-id").value = "456"
            application.screen.query_one("#implementer-key-env").value = "IMPLEMENTER_KEY"
            application.screen.query_one("#planner-app-id").value = ""
            application.screen.query_one("#planner-installation-id").value = ""
            application.screen.query_one("#planner-key-env").value = ""
            application.screen.query_one("#save-github-apps", Button).focus()
            await settle_tui(pilot)
            await pilot.press("enter")
            assert "Saved App references" in str(
                application.screen.query_one("#github-app-status").renderable
            )
            assert controller._config_loader.saved_github_apps is not None
            assert not controller._config_loader.saved_github_apps[1].is_configured

    asyncio.run(drive())


def test_tui_creates_updates_and_switches_agent_instruction_files(tmp_path: Path) -> None:
    controller, _, _ = _controller(tmp_path)
    other_path = tmp_path / "other"
    other_path.mkdir()
    controller.repositories = lambda: [  # type: ignore[method-assign]
        ConfiguredRepository("example", tmp_path),
        ConfiguredRepository("other", other_path),
    ]
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test(size=(80, 24)) as pilot:
            await pilot.press("i")
            assert isinstance(application.screen, AgentInstructionsScreen)
            screen = application.screen
            repository = screen.query_one("#agent-instructions-repository", OrchestratorSelect)
            repository.value = "example"
            await settle_tui(pilot)
            for role in ("planner", "implementer", "reviewer"):
                editor = screen.query_one(f"#{role}-instructions", TextArea)
                assert editor.outer_size.height == 7
            assert "No PLANNER.md yet" in str(
                screen.query_one("#planner-instructions-status", Static).renderable
            )
            assert not (tmp_path / "PLANNER.md").exists()
            assert "No IMPLEMENTER.md yet" in str(
                screen.query_one("#implementer-instructions-status", Static).renderable
            )
            assert not (tmp_path / "IMPLEMENTER.md").exists()
            assert not (tmp_path / "REVIEWER.md").exists()

            screen.query_one("#planner-instructions", TextArea).text = "Plan scoped changes."
            screen.query_one("#save-planner-instructions", Button).press()
            await settle_tui(pilot)
            assert (tmp_path / "PLANNER.md").read_text(encoding="utf-8") == "Plan scoped changes."
            assert not (tmp_path / "IMPLEMENTER.md").exists()
            assert not (tmp_path / "REVIEWER.md").exists()

            screen.query_one("#implementer-instructions", TextArea).text = "Use focused tests."
            screen.query_one("#save-implementer-instructions", Button).press()
            await settle_tui(pilot)
            assert (tmp_path / "IMPLEMENTER.md").read_text(encoding="utf-8") == "Use focused tests."
            assert not (tmp_path / "REVIEWER.md").exists()

            screen.query_one("#reviewer-instructions", TextArea).text = "Check retries."
            screen.query_one("#save-reviewer-instructions", Button).press()
            await settle_tui(pilot)
            assert (tmp_path / "REVIEWER.md").read_text(encoding="utf-8") == "Check retries."
            assert "Git was not changed" in str(
                screen.query_one("#agent-instructions-status", Static).renderable
            )

            repository.value = "other"
            await settle_tui(pilot)
            assert screen.query_one("#planner-instructions", TextArea).text == ""
            assert "No PLANNER.md yet" in str(
                screen.query_one("#planner-instructions-status", Static).renderable
            )
            assert screen.query_one("#implementer-instructions", TextArea).text == ""
            assert "No REVIEWER.md yet" in str(
                screen.query_one("#reviewer-instructions-status", Static).renderable
            )
            assert not (other_path / "PLANNER.md").exists()

            repository.value = "example"
            await settle_tui(pilot)
            assert (
                screen.query_one("#planner-instructions", TextArea).text
                == "Plan scoped changes."
            )

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


@pytest.mark.parametrize("width", [80, 36])
def test_execution_detail_replaces_raw_json_with_compact_summary(
    tmp_path: Path, width: int
) -> None:
    controller, _, repository = _controller(tmp_path)
    execution = repository.execution
    execution.repository = "example"
    execution.task_url = "https://example/tasks/12"
    execution.pr_url = "https://example/pull/34"
    execution.workspace_path = str(tmp_path / "worktree")
    execution.working_branch = "workflow/12"
    execution.commit_sha = "a" * 40
    execution.validation_snapshot = {"passed": True}
    execution.review_snapshot = {"decision": "APPROVED", "findings": []}
    execution.spec_jules_session_url = "https://example/sessions/planner"
    execution.review_jules_session_url = "https://example/sessions/reviewer"
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test(size=(width, 24)) as pilot:
            application.push_screen(ExecutionDetailScreen(execution.id))
            await settle_tui(pilot)
            assert isinstance(application.screen, ExecutionDetailScreen)
            summary = application.screen.query_one("#execution-summary", Static)
            assert str(summary.renderable).splitlines() == [
                f"Workspace path: {execution.workspace_path}",
                "Working branch: workflow/12",
                f"Commit SHA: {execution.commit_sha}",
                "PR: https://example/pull/34",
            ]
            assert summary.parent is application.screen.query_one("#execution-detail")
            assert summary.size.height == 4
            assert not summary.show_vertical_scrollbar
            assert not application.screen.query("#execution-content")
            assert not application.screen.query("#execution-content-scroll")
            assert not application.screen.query("#open-spec-session")
            assert not application.screen.query("#open-review-session")
            assert application.screen._links == {
                "open-task": execution.task_url, "open-pr": execution.pr_url,
            }

    asyncio.run(drive())


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("workspace_path", r"C:\worktree [project]", r"Workspace path: C:\worktree [project]"),
        ("working_branch", "workflow/12", "Working branch: workflow/12"),
        ("commit_sha", "abc123", "Commit SHA: abc123"),
        ("pr_url", "https://example/pull/34", "PR: https://example/pull/34"),
    ],
)
def test_execution_detail_summary_shows_only_existing_values_and_refreshes(
    tmp_path: Path, field: str, value: str, expected: str
) -> None:
    controller, _, repository = _controller(tmp_path)
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test() as pilot:
            application.push_screen(ExecutionDetailScreen(repository.execution.id))
            await settle_tui(pilot)
            screen = application.screen
            summary = screen.query_one("#execution-summary", Static)
            assert not summary.display
            assert str(summary.renderable) == ""

            setattr(repository.execution, field, value)
            screen._refresh_execution()
            await settle_tui(pilot)
            assert summary.display
            assert str(summary.renderable) == expected
            assert summary.size.height == 1

            for missing in ("", None):
                setattr(repository.execution, field, missing)
                screen._refresh_execution()
                await settle_tui(pilot)
                assert not summary.display
                assert str(summary.renderable) == ""

    asyncio.run(drive())


def test_execution_detail_reconciles_history_execution_and_new_pr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller, _, repository = _controller(tmp_path)
    execution = repository.execution
    execution.status = WorkflowStatus.RUNNING
    execution.current_stage = WorkflowStage.IMPLEMENT
    steps = [WorkflowStep(
        id=1,
        workflow_execution_id=execution.id,
        stage=WorkflowStage.IMPLEMENT,
        status=StepStatus.RUNNING,
        started_at=datetime.now(UTC),
    )]
    monkeypatch.setattr(repository, "list_steps", lambda _id: steps)
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test() as pilot:
            application.push_screen(ExecutionHistoryScreen())
            await settle_tui(pilot)
            application.screen.query_one(
                "#execution-picker", OrchestratorSelect
            ).value = execution.id
            application.screen.query_one("#inspect-execution", Button).press()
            await settle_tui(pilot)
            screen = application.screen
            assert isinstance(screen, ExecutionDetailScreen)
            assert screen._reconcile_timer is not None
            assert screen.query_one("#open-pr", Button).disabled
            execution.current_stage = WorkflowStage.CREATE_PR
            execution.pr_url = "https://example/pull/34"
            steps[0].status = StepStatus.SUCCEEDED
            steps[0].finished_at = datetime.now(UTC)
            screen._refresh_execution()  # The same callback used by the two-second timer.
            await settle_tui(pilot)
            assert str(screen.query_one("#execution-stage", Static).renderable) == (
                "Stage: CREATE_PR"
            )
            assert "Changes implemented" in str(
                screen.query_one("#execution-timeline", Static).renderable
            )
            assert not screen.query_one("#open-pr", Button).disabled
            assert "PR: https://example/pull/34" in str(
                screen.query_one("#execution-summary", Static).renderable
            )
            assert screen._links["open-pr"] == execution.pr_url
            execution.status = WorkflowStatus.APPROVED
            screen._refresh_execution()
            assert screen._reconcile_timer is None

    asyncio.run(drive())


def test_execution_detail_matching_event_refreshes_durable_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller, service, repository = _controller(tmp_path)
    repository.execution.status = WorkflowStatus.RUNNING
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test() as pilot:
            application.push_screen(ExecutionDetailScreen(repository.execution.id))
            await settle_tui(pilot)
            screen = application.screen
            assert isinstance(screen, ExecutionDetailScreen)
            assert screen._reconcile_timer is not None
            screen._reconcile_timer.stop()  # Keep this event test independent of timer timing.
            reads: list[str] = []
            original = controller.execution_detail

            def read(execution_id: str) -> tuple[WorkflowExecution, list[WorkflowStep]]:
                reads.append(execution_id)
                return original(execution_id)

            monkeypatch.setattr(controller, "execution_detail", read)
            observe = controller._tracking_listener(service, None)  # type: ignore[arg-type]
            observe(WorkflowEvent("other", WorkflowEventKind.STATUS_CHANGED))
            await settle_tui(pilot)
            assert reads == []
            for _ in range(8):
                observe(WorkflowEvent(repository.execution.id, WorkflowEventKind.DIAGNOSTIC))
            await settle_tui(pilot)
            assert reads == [repository.execution.id]
            reads.clear()
            repository.execution.status = WorkflowStatus.APPROVED
            observe(WorkflowEvent(repository.execution.id, WorkflowEventKind.WORKFLOW_FINISHED))
            await settle_tui(pilot)
            assert reads == [repository.execution.id]
            assert screen._reconcile_timer is None
            assert str(screen.query_one("#execution-status", Static).renderable) == "APPROVED"

    asyncio.run(drive())


def test_execution_detail_observer_failure_does_not_affect_workflow(tmp_path: Path) -> None:
    controller, service, _ = _controller(tmp_path)

    def broken(_event: WorkflowEvent) -> None:
        raise RuntimeError("detached UI callback")

    controller.subscribe_execution_events(broken)
    observed: list[WorkflowEventKind] = []
    controller._tracking_listener(  # type: ignore[arg-type]
        service, lambda event: observed.append(event.kind)
    )(WorkflowEvent("execution-1", WorkflowEventKind.STATUS_CHANGED))
    controller.unsubscribe_execution_events(broken)
    assert observed == [WorkflowEventKind.STATUS_CHANGED]


def test_execution_detail_reopens_persisted_waiting_and_human_timeline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller, _, repository = _controller(tmp_path)
    execution = repository.execution
    execution.status = WorkflowStatus.WAITING
    execution.current_stage = WorkflowStage.GENERATE_SPEC
    started = datetime.now(UTC)
    steps = [WorkflowStep(
        id=1,
        workflow_execution_id=execution.id,
        stage=WorkflowStage.GENERATE_SPEC,
        status=StepStatus.RUNNING,
        started_at=started,
    )]
    monkeypatch.setattr(repository, "list_steps", lambda _id: steps)
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test() as pilot:
            application.push_screen(ExecutionDetailScreen(execution.id))
            await settle_tui(pilot)
            timeline = str(application.screen.query_one("#execution-timeline", Static).renderable)
            assert "Generating specification" in timeline
            assert "Waiting for agent capacity" in timeline
            await pilot.press("escape")
            steps[0].status = StepStatus.SUCCEEDED
            steps[0].finished_at = datetime.now(UTC)
            execution.status = WorkflowStatus.NEEDS_HUMAN
            execution.human_interactions = [HumanInteraction(
                id=1,
                workflow_execution_id=execution.id,
                requesting_stage=WorkflowStage.GENERATE_SPEC,
                prompt="Clarify the task",
                requested_at=datetime.now(UTC),
            )]
            application.push_screen(ExecutionDetailScreen(execution.id))
            await settle_tui(pilot)
            timeline = str(application.screen.query_one("#execution-timeline", Static).renderable)
            assert "Generating specification — waiting for human input" in timeline
            assert "Waiting for human input" in timeline

    asyncio.run(drive())


@pytest.mark.parametrize(("width", "maximum_rows"), [(80, 2), (64, 2), (36, 4)])
@pytest.mark.parametrize("status", [WorkflowStatus.RUNNING, WorkflowStatus.APPROVED])
def test_execution_detail_actions_reflow_without_diagnostic_overflow(
    tmp_path: Path, width: int, maximum_rows: int, status: WorkflowStatus,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller, _, repository = _controller(tmp_path)
    execution = repository.execution
    execution.status = status
    execution.task_url = "https://example/tasks/12"
    execution.pr_url = "https://example/pull/34"
    long_diagnostics = [ExecutionDiagnostic(
        execution_id=execution.id,
        category=DiagnosticCategory.RESPONSE,
        payload={"output": "diagnostic" * 300},
    )]
    opened: list[str] = []
    monkeypatch.setattr("orchestrator.tui.screens.execution_detail.webbrowser.open", opened.append)
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test(size=(width, 24)) as pilot:
            application.push_screen(ExecutionDetailScreen(execution.id))
            await settle_tui(pilot)
            screen = application.screen
            action_ids = ("open-task", "open-pr", "copy-detailed-logs-button")
            if status is WorkflowStatus.RUNNING:
                action_ids = ("execution-cancel", *action_ids)
            buttons = [screen.query_one(f"#{button_id}", Button) for button_id in action_ids]
            assert not screen.query("#open-spec-session")
            assert not screen.query("#open-review-session")
            actions = screen.query_one("#execution-detail-actions")
            assert [
                button.id for button in actions.query(Button) if button.display
            ] == list(action_ids)
            positions = [(button.virtual_region.x, button.virtual_region.y) for button in buttons]
            repository.diagnostics = long_diagnostics
            screen._refresh_execution()
            screen.query_one("#execution-toggle-logs", Button).press()
            await settle_tui(pilot)
            assert positions == [
                (button.virtual_region.x, button.virtual_region.y) for button in buttons
            ]
            assert all(button.display and not button.disabled for button in buttons)
            assert len({button.virtual_region.y for button in buttons}) <= maximum_rows
            # Every occupied row fills the grid, with only the configured gutter between buttons.
            for row_y in {button.region.y for button in buttons}:
                row = [button for button in buttons if button.region.y == row_y]
                assert row[0].region.x == actions.region.x
                assert row[-1].region.right == actions.region.right
                assert all(
                    right.region.x - left.region.right == 1
                    for left, right in zip(row, row[1:], strict=False)
                )
            assert screen.query_one("#execution-detail", VerticalScroll).max_scroll_x == 0
            content = screen.query_one("#execution-detailed-logs", Log)
            assert content.max_scroll_x > 0

            for button in buttons:
                button.focus()
                await settle_tui(pilot)
                assert application.focused is button
                assert 0 <= button.region.x < button.region.right <= width
                assert 0 <= button.region.y < button.region.bottom <= application.size.height

            buttons[0].focus()
            await settle_tui(pilot)
            for button in buttons[1:]:
                await pilot.press("tab")
                assert application.focused is button
            assert await pilot.click("#open-task")
            await settle_tui(pilot)
            assert opened == [execution.task_url]

    asyncio.run(drive())


@pytest.mark.parametrize(
    "status", [WorkflowStatus.RUNNING, WorkflowStatus.APPROVED, WorkflowStatus.CHANGES_REQUESTED]
)
def test_execution_detail_loads_logs_only_when_expanded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: WorkflowStatus
) -> None:
    controller, _, repository = _controller(tmp_path)
    repository.execution.status = status
    repository.diagnostics = [ExecutionDiagnostic(
        execution_id=repository.execution.id,
        category=DiagnosticCategory.RESPONSE,
        payload={"output": "first diagnostic"},
    )]
    reads: list[int | None] = []
    original_list = repository.list_diagnostics
    original_after = repository.list_diagnostics_after

    def full_read(execution_id: str) -> list[ExecutionDiagnostic]:
        reads.append(None)
        return original_list(execution_id)

    def incremental_read(execution_id: str, after_id: int) -> list[ExecutionDiagnostic]:
        reads.append(after_id)
        return original_after(execution_id, after_id)

    monkeypatch.setattr(repository, "list_diagnostics", full_read)
    monkeypatch.setattr(repository, "list_diagnostics_after", incremental_read)
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test(size=(80, 24)) as pilot:
            application.push_screen(ExecutionDetailScreen(repository.execution.id))
            await settle_tui(pilot)
            screen = application.screen
            log = screen.query_one("#execution-detailed-logs", Log)
            toggle = screen.query_one("#execution-toggle-logs", Button)
            assert not log.display
            assert not log.lines
            assert str(toggle.label) == "Show detailed logs"
            screen._refresh_execution()
            await settle_tui(pilot)
            assert reads == []
            assert not log.lines

            toggle.focus()
            await settle_tui(pilot)
            assert await pilot.click("#execution-toggle-logs")
            await settle_tui(pilot)
            assert log.display
            assert str(toggle.label) == "Hide detailed logs"
            assert "first diagnostic" in str(log.lines)
            assert reads == [None]

            toggle.press()
            await settle_tui(pilot)
            assert not log.display
            assert str(toggle.label) == "Show detailed logs"
            lines_before = list(log.lines)
            repository.diagnostics.append(ExecutionDiagnostic(
                execution_id=repository.execution.id,
                category=DiagnosticCategory.RESPONSE,
                payload={"output": "diagnostic while collapsed"},
            ))
            screen._refresh_execution()
            await settle_tui(pilot)
            assert reads == [None]
            assert list(log.lines) == lines_before

            toggle.press()
            await settle_tui(pilot)
            assert log.display
            assert reads == [None, 1]
            assert str(log.lines).count("first diagnostic") == 1
            assert "diagnostic while collapsed" in str(log.lines)
            assert log.max_scroll_y == 0

    asyncio.run(drive())


def test_execution_detail_log_toggle_stays_visible_above_compact_summary(tmp_path: Path) -> None:
    controller, _, repository = _controller(tmp_path)
    repository.execution.validation_snapshot = {
        f"check_{number}": "passed" for number in range(400)
    }
    repository.execution.working_branch = "workflow/12"
    repository.diagnostics = [ExecutionDiagnostic(
        execution_id=repository.execution.id,
        category=DiagnosticCategory.RESPONSE,
        payload={"output": "expanded diagnostic"},
    )]
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test(size=(80, 24)) as pilot:
            application.push_screen(ExecutionDetailScreen(repository.execution.id))
            await settle_tui(pilot)
            screen = application.screen
            toggle = screen.query_one("#execution-toggle-logs", Button)
            log = screen.query_one("#execution-detailed-logs", Log)
            summary = screen.query_one("#execution-summary", Static)
            actions = screen.query_one("#execution-detail-actions")
            action_region = actions.region
            toggle_region = toggle.region
            toggle.active_effect_duration = 0
            assert toggle.display
            assert 0 <= toggle.region.y < toggle.region.bottom <= application.size.height
            assert summary.size.height == 1
            assert toggle.region.bottom <= summary.region.y
            assert not log.display
            assert not log.lines

            assert await pilot.click("#execution-toggle-logs")
            await settle_tui(pilot)
            assert str(toggle.label) == "Hide detailed logs"
            assert log.display
            assert "expanded diagnostic" in str(log.lines)
            assert actions.region == action_region
            assert toggle.region == toggle_region
            assert toggle.region.bottom <= log.region.y < summary.region.y

            assert await pilot.click("#execution-toggle-logs")
            await settle_tui(pilot)
            assert str(toggle.label) == "Show detailed logs"
            assert not log.display
            assert actions.region == action_region
            assert toggle.region == toggle_region

    asyncio.run(drive())


def test_execution_detail_open_and_refresh_do_not_fetch_task_or_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller, _, repository = _controller(tmp_path)
    long_payload = "diagnostic" * 1000
    repository.diagnostics = [
        ExecutionDiagnostic(
            execution_id=repository.execution.id,
            category=DiagnosticCategory.RESPONSE,
            payload={"output": long_payload},
        )
    ]

    def unexpected_call(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("Execution detail must use persisted data only")

    controller._task_provider_factory = unexpected_call
    monkeypatch.setattr(
        "orchestrator.services.project_context.load_project_context", unexpected_call
    )
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test(size=(80, 24)) as pilot:
            application.push_screen(ExecutionDetailScreen(repository.execution.id))
            await settle_tui(pilot)
            screen = application.screen
            content = screen.query_one("#execution-detailed-logs", Log)
            assert not content.display
            assert not content.lines
            screen.query_one("#execution-toggle-logs", Button).press()
            await settle_tui(pilot)
            assert long_payload in str(content.lines)
            assert content.max_scroll_x > 0
            screen._refresh_execution()
            await settle_tui(pilot)
            assert long_payload in str(content.lines)

    asyncio.run(drive())


@pytest.mark.parametrize("status", list(WorkflowStatus))
@pytest.mark.parametrize("requested", [False, True])
def test_execution_detail_cancel_visibility(
    tmp_path: Path, status: WorkflowStatus, requested: bool
) -> None:
    controller, _, repository = _controller(tmp_path)
    repository.execution.status = status
    if requested:
        repository.execution.cancellation_requested_at = datetime.now(UTC)
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test() as pilot:
            application.push_screen(ExecutionDetailScreen(repository.execution.id))
            await settle_tui(pilot)
            cancel = application.screen.query_one("#execution-cancel", Button)
            assert cancel.display is (
                status in {WorkflowStatus.NEEDS_HUMAN, WorkflowStatus.INTERRUPTED}
                or (
                    status in {
                        WorkflowStatus.PENDING, WorkflowStatus.WAITING, WorkflowStatus.RUNNING
                    }
                    and not requested
                )
            )
            if requested and status is WorkflowStatus.NEEDS_HUMAN:
                assert application.screen.query_one("#execution-status", Static).has_class(
                    "cancelled"
                )

    asyncio.run(drive())


@pytest.mark.parametrize(
    "status",
    [
        WorkflowStatus.PENDING,
        WorkflowStatus.WAITING,
        WorkflowStatus.RUNNING,
        WorkflowStatus.NEEDS_HUMAN,
    ],
)
def test_execution_detail_cancel_is_visible_and_keyboard_reachable_with_long_diagnostics(
    tmp_path: Path, status: WorkflowStatus
) -> None:
    controller, _, repository = _controller(tmp_path)
    repository.execution.status = status
    repository.diagnostics = [
        ExecutionDiagnostic(
            execution_id=repository.execution.id,
            category=DiagnosticCategory.RESPONSE,
            payload={"output": "diagnostic" * 300},
        )
    ]
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test(size=(36, 14)) as pilot:
            application.push_screen(ExecutionHistoryScreen())
            await settle_tui(pilot)
            application.screen.query_one("#execution-picker", OrchestratorSelect).value = (
                repository.execution.id
            )
            application.screen.query_one("#inspect-execution", Button).press()
            await settle_tui(pilot)
            screen = application.screen
            assert isinstance(screen, ExecutionDetailScreen)
            cancel = screen.query_one("#execution-cancel", Button)
            assert cancel.display and not cancel.disabled
            assert 0 <= cancel.region.x < cancel.region.right <= application.size.width
            assert 0 <= cancel.region.y < cancel.region.bottom <= application.size.height
            assert screen.query_one("#execution-detail", VerticalScroll).max_scroll_x == 0
            content = screen.query_one("#execution-detailed-logs", Log)
            assert not content.display
            assert not content.lines

            for _ in range(12):
                if application.focused is cancel:
                    break
                await pilot.press("tab")
            assert application.focused is cancel
            await pilot.press("enter")
            await settle_tui(pilot)
            assert isinstance(application.screen, StopWorkflowConfirmation)
            assert repository.cancellation_requests == []
            application.screen.query_one("#confirm-stop-workflow", Button).press()
            await settle_tui(pilot)
            assert repository.cancellation_requests == [repository.execution.id]

    asyncio.run(drive())


def test_execution_detail_actions_and_diagnostics_stay_bounded_in_narrow_terminal(
    tmp_path: Path,
) -> None:
    controller, _, repository = _controller(tmp_path)
    repository.execution.status = WorkflowStatus.NEEDS_HUMAN
    repository.execution.task_url = "https://example/tasks/12"
    repository.execution.pr_url = "https://example/pull/34"
    repository.execution.human_interactions = [HumanInteraction(
        id=1,
        workflow_execution_id=repository.execution.id,
        requesting_stage=WorkflowStage.REVIEW,
        prompt="Review finding?",
        requested_at=datetime.now(UTC),
    )]
    repository.diagnostics = [
        ExecutionDiagnostic(
            execution_id=repository.execution.id,
            category=DiagnosticCategory.RESPONSE,
            payload={"output": "diagnostic" * 300, "row": number},
        )
        for number in range(30)
    ]
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test(size=(36, 14)) as pilot:
            application.push_screen(ExecutionDetailScreen(repository.execution.id))
            await settle_tui(pilot)
            screen = application.screen
            page = screen.query_one("#execution-detail", VerticalScroll)
            content = screen.query_one("#execution-detailed-logs", Log)
            screen.query_one("#execution-toggle-logs", Button).press()
            await settle_tui(pilot)
            assert page.max_scroll_x == 0
            assert page.max_scroll_y > 0
            assert content.max_scroll_x > 0
            assert content.max_scroll_y == 0
            assert not content.show_vertical_scrollbar
            assert content.scrollable_content_region.height == content.line_count
            assert not screen.query("#execution-content-scroll")
            assert content.region.right <= application.size.width

            screen.query_one("#execution-human-response", TextArea).text = "Answer"
            await settle_tui(pilot)
            for button_id in ("open-task", "open-pr", "execution-resume"):
                button = screen.query_one(f"#{button_id}", Button)
                assert not button.disabled
                button.focus()
                await settle_tui(pilot)
                await pilot.wait_for_scheduled_animations()
                assert application.focused is button
                assert 0 <= button.region.x < button.region.right <= application.size.width
                assert 0 <= button.region.y < button.region.bottom <= application.size.height

            cancel = screen.query_one("#execution-cancel", Button)
            cancel.focus()
            await settle_tui(pilot)
            # Draining messages does not finish focus-triggered scrolling. Click
            # the button's interior only after its rendered position has settled.
            await pilot.wait_for_scheduled_animations()
            assert application.focused is cancel
            assert screen.can_view_entire(cancel)
            assert await pilot.click(
                cancel, offset=(cancel.size.width // 2, cancel.size.height // 2)
            )
            await settle_tui(pilot)
            assert isinstance(application.screen, StopWorkflowConfirmation)
            assert repository.cancellation_requests == []

    asyncio.run(drive())


@pytest.mark.parametrize(
    ("initial_status", "requested", "expected_status"),
    [
        (WorkflowStatus.NEEDS_HUMAN, False, "CANCELLED"),
        (WorkflowStatus.NEEDS_HUMAN, True, "CANCELLED"),
        (WorkflowStatus.PENDING, False, "CANCELLATION REQUESTED"),
        (WorkflowStatus.WAITING, False, "CANCELLATION REQUESTED"),
        (WorkflowStatus.RUNNING, False, "CANCELLATION REQUESTED"),
    ],
)
def test_execution_detail_confirms_durable_cancellation_and_refreshes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    initial_status: WorkflowStatus,
    requested: bool,
    expected_status: str,
) -> None:
    controller, _, repository = _controller(tmp_path)
    repository.execution.status = initial_status
    if requested:
        repository.execution.cancellation_requested_at = datetime.now(UTC)
    calls: list[str] = []
    original_cancel = controller.cancel_workflow

    def cancel(execution_id: str) -> bool:
        calls.append(execution_id)
        return original_cancel(execution_id)

    monkeypatch.setattr(controller, "cancel_workflow", cancel)
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test() as pilot:
            application.push_screen(ExecutionDetailScreen(repository.execution.id))
            await settle_tui(pilot)
            screen = application.screen
            screen.query_one("#execution-cancel", Button).press()
            await settle_tui(pilot)
            assert isinstance(application.screen, StopWorkflowConfirmation)
            assert calls == []
            application.screen.query_one("#dismiss-stop-workflow", Button).press()
            await settle_tui(pilot)
            assert application.screen is screen
            assert calls == []
            assert screen.query_one("#execution-cancel", Button).display

            screen.query_one("#execution-cancel", Button).press()
            await settle_tui(pilot)
            assert isinstance(application.screen, StopWorkflowConfirmation)
            application.screen.query_one("#confirm-stop-workflow", Button).press()
            await settle_tui(pilot)
            assert application.screen is screen
            assert calls == [repository.execution.id]
            assert repository.cancellation_requests == calls
            assert str(screen.query_one("#execution-status", Static).renderable) == expected_status
            assert screen.query_one("#execution-cancel", Button).display is False
            if expected_status == "CANCELLATION REQUESTED":
                repository.execution.status = WorkflowStatus.CANCELLED
                screen._refresh_execution()
                assert str(screen.query_one("#execution-status", Static).renderable) == "CANCELLED"
                assert screen._reconcile_timer is None

    asyncio.run(drive())


def test_execution_detail_observes_requested_cancellation_settling(
    tmp_path: Path,
) -> None:
    controller, _, repository = _controller(tmp_path)
    repository.execution.status = WorkflowStatus.RUNNING
    repository.execution.cancellation_requested_at = datetime.now(UTC)
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test() as pilot:
            application.push_screen(ExecutionDetailScreen(repository.execution.id))
            await settle_tui(pilot)
            screen = application.screen
            assert isinstance(screen, ExecutionDetailScreen)
            assert screen._reconcile_timer is not None
            assert str(screen.query_one("#execution-status", Static).renderable) == (
                "CANCELLATION REQUESTED"
            )

            repository.execution.status = WorkflowStatus.CANCELLED
            screen._refresh_execution()
            assert str(screen.query_one("#execution-status", Static).renderable) == "CANCELLED"
            assert screen._reconcile_timer is None
            assert not screen.query_one("#execution-cancel", Button).display

    asyncio.run(drive())


def test_execution_detail_cancel_error_keeps_active_control(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller, _, repository = _controller(tmp_path)
    repository.execution.status = WorkflowStatus.RUNNING
    notifications: list[str] = []

    def fail(_: str) -> bool:
        raise RuntimeError("storage unavailable")

    def notify(_screen: ExecutionDetailScreen, message: str, **kwargs: Any) -> None:
        notifications.append(message)

    monkeypatch.setattr(controller, "cancel_workflow", fail)
    monkeypatch.setattr(ExecutionDetailScreen, "notify", notify)
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test() as pilot:
            application.push_screen(ExecutionDetailScreen(repository.execution.id))
            await settle_tui(pilot)
            screen = application.screen
            screen.query_one("#execution-cancel", Button).press()
            await settle_tui(pilot)
            application.screen.query_one("#confirm-stop-workflow", Button).press()
            await settle_tui(pilot)
            assert str(screen.query_one("#execution-status", Static).renderable) == "RUNNING"
            cancel = screen.query_one("#execution-cancel", Button)
            assert cancel.display and not cancel.disabled
            assert any("storage unavailable" in message for message in notifications)

    asyncio.run(drive())


@pytest.mark.parametrize("failure", ["rejected", "error"])
def test_execution_detail_cancel_race_refreshes_controls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    controller, _, repository = _controller(tmp_path)
    repository.execution.status = WorkflowStatus.RUNNING
    notifications: list[str] = []

    def cancel(_: str) -> bool:
        repository.execution.status = WorkflowStatus.APPROVED
        if failure == "error":
            raise RuntimeError("storage unavailable")
        return False

    def notify(_screen: ExecutionDetailScreen, message: str, **kwargs: Any) -> None:
        notifications.append(message)

    monkeypatch.setattr(controller, "cancel_workflow", cancel)
    monkeypatch.setattr(ExecutionDetailScreen, "notify", notify)
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test() as pilot:
            application.push_screen(ExecutionDetailScreen(repository.execution.id))
            await settle_tui(pilot)
            screen = application.screen
            screen.query_one("#execution-cancel", Button).press()
            await settle_tui(pilot)
            assert isinstance(application.screen, StopWorkflowConfirmation)
            application.screen.query_one("#confirm-stop-workflow", Button).press()
            await settle_tui(pilot)
            assert application.screen is screen
            assert str(screen.query_one("#execution-status", Static).renderable) == "APPROVED"
            assert screen.query_one("#execution-cancel", Button).display is False
            assert any(
                "already finished" in message if failure == "rejected"
                else "storage unavailable" in message
                for message in notifications
            )

    asyncio.run(drive())


@pytest.mark.parametrize(
    ("status", "workspace_path", "working_branch", "expected"),
    [
        (WorkflowStatus.CHANGES_REQUESTED, "worktree", "workflow/12", True),
        (WorkflowStatus.FAILED, "worktree", "workflow/12", True),
        (WorkflowStatus.CHANGES_REQUESTED, None, None, True),
        (WorkflowStatus.FAILED, None, "workflow/12", False),
        (WorkflowStatus.FAILED, "worktree", None, False),
        (WorkflowStatus.APPROVED, "worktree", "workflow/12", False),
        (WorkflowStatus.CANCELLED, "worktree", "workflow/12", False),
        (WorkflowStatus.NEEDS_HUMAN, "worktree", "workflow/12", False),
    ],
)
def test_execution_detail_continuation_follows_status_not_completed_stage(
    tmp_path: Path,
    status: WorkflowStatus,
    workspace_path: str | None,
    working_branch: str | None,
    expected: bool,
) -> None:
    """Every terminal execution reaches ``COMPLETED``, so only ``status`` may decide."""

    controller, _, repository = _controller(tmp_path)
    repository.execution.status = status
    repository.execution.current_stage = WorkflowStage.COMPLETED
    repository.execution.workspace_path = (
        str(tmp_path / workspace_path) if workspace_path is not None else None
    )
    repository.execution.working_branch = working_branch
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test() as pilot:
            application.push_screen(ExecutionDetailScreen(repository.execution.id))
            await settle_tui(pilot)
            screen = application.screen
            for widget_id in (
                "execution-continuation-title",
                "execution-continuation-feedback",
                "execution-continuation-actions",
            ):
                assert screen.query_one(f"#{widget_id}").display is expected

    asyncio.run(drive())


def test_execution_detail_offers_planner_continuation_after_planning_quota_failure(
    tmp_path: Path,
) -> None:
    """A quota failure during planning leaves no workspace, but can still be continued."""

    controller, _, repository = _controller(tmp_path)
    repository.execution.status = WorkflowStatus.FAILED
    repository.execution.current_stage = WorkflowStage.COMPLETED
    repository.execution.workspace_path = None
    repository.execution.working_branch = None
    repository.diagnostics = [
        ExecutionDiagnostic(
            execution_id=repository.execution.id,
            category=DiagnosticCategory.WORKFLOW_EVENT,
            payload={"kind": "PROVIDER_QUOTA_EXHAUSTED", "provider": "planner"},
            stage=WorkflowStage.GENERATE_SPEC,
        )
    ]
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test() as pilot:
            application.push_screen(ExecutionDetailScreen(repository.execution.id))
            await settle_tui(pilot)
            screen = application.screen
            assert screen.query_one("#execution-continuation-feedback").display is True
            assert str(
                screen.query_one("#execution-continuation-title", Static).renderable
            ) == "Instructions for the planner (sent when you click Continue Execution)."

    asyncio.run(drive())


def test_execution_detail_continuation_feedback_is_readable_beside_long_details(
    tmp_path: Path,
) -> None:
    """The feedback editor and Continue action remain reachable beside long details."""

    controller, _, repository = _controller(tmp_path)
    repository.execution.status = WorkflowStatus.CHANGES_REQUESTED
    repository.execution.current_stage = WorkflowStage.COMPLETED
    repository.execution.workspace_path = str(tmp_path / "worktree")
    repository.execution.working_branch = "workflow/12"
    repository.execution.validation_snapshot = {
        f"check_{number}": "passed" for number in range(40)
    }
    repository.diagnostics = [ExecutionDiagnostic(
        execution_id=repository.execution.id,
        category=DiagnosticCategory.RESPONSE,
        payload={"output": "diagnostic" * 300},
    )]
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test(size=(36, 14)) as pilot:
            application.push_screen(ExecutionDetailScreen(repository.execution.id))
            await settle_tui(pilot)
            screen = application.screen
            screen.query_one("#execution-toggle-logs", Button).press()
            await settle_tui(pilot)
            feedback = screen.query_one("#execution-continuation-feedback", TextArea)
            assert feedback.text == "Persisted terminal feedback"
            assert feedback.size.height > 0
            button = screen.query_one("#execution-continue", Button)
            assert not button.disabled
            button.focus()
            await settle_tui(pilot)
            assert application.focused is button
            assert 0 <= button.region.x < button.region.right <= application.size.width
            assert 0 <= button.region.y < button.region.bottom <= application.size.height
            assert screen.query_one("#execution-detail", VerticalScroll).max_scroll_x == 0
            content = screen.query_one("#execution-detailed-logs", Log)
            assert content.max_scroll_x > 0

    asyncio.run(drive())


def test_execution_detail_offers_editable_terminal_continuation(tmp_path: Path) -> None:
    controller, service, repository = _controller(tmp_path)
    repository.execution.status = WorkflowStatus.CHANGES_REQUESTED
    repository.execution.workspace_path = str(tmp_path / "worktree")
    repository.execution.working_branch = "workflow/12"
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test() as pilot:
            application.push_screen(ExecutionDetailScreen(repository.execution.id))
            await settle_tui(pilot)
            screen = application.screen
            feedback = screen.query_one("#execution-continuation-feedback", TextArea)
            assert str(screen.query_one("#execution-continuation-title", Static).renderable) == (
                "Instructions for the implementer (sent when you click Continue Execution)."
                " Work resumes in the existing worktree without repeating earlier steps."
            )
            assert feedback.display is True
            assert feedback.text == "Persisted terminal feedback"
            feedback.text = "Edited continuation feedback"
            feedback.focus()
            screen._refresh_execution()
            await settle_tui(pilot)
            assert feedback.text == "Edited continuation feedback"
            assert application.focused is feedback
            screen.query_one("#execution-continue", Button).press()
            await wait_for_tui(pilot, lambda: bool(service.continuations))

    asyncio.run(drive())

    assert service.continuations == [("execution-1", "Edited continuation feedback")]


@pytest.mark.parametrize("action", ["continuation", "answer"])
@pytest.mark.parametrize("live_batch", [False, True])
def test_execution_detail_refreshes_after_in_place_submission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    live_batch: bool,
) -> None:
    controller, service, repository = _controller(tmp_path)
    execution = repository.execution
    if action == "continuation":
        execution.status = WorkflowStatus.CHANGES_REQUESTED
        execution.workspace_path = str(tmp_path / "worktree")
        execution.working_branch = "workflow/12"
    else:
        execution.status = WorkflowStatus.NEEDS_HUMAN
        execution.current_stage = WorkflowStage.REVIEW
        execution.human_interactions = [HumanInteraction(
            id=1,
            workflow_execution_id=execution.id,
            requesting_stage=WorkflowStage.REVIEW,
            prompt="Clarify the change.",
            requested_at=datetime.now(UTC),
        )]

    batch_events: list[WorkflowEventKind] = []
    if live_batch:
        def observe_batch(event: WorkflowEvent) -> None:
            assert not run.condition._is_owned()
            batch_events.append(event.kind)

        run = _BatchRun("batch-1", 1, frozenset(), observe_batch)
        run.positions[execution.id] = 0
        run.statuses[0] = execution.status
        controller._active_batches[run.batch_id] = run
        controller._execution_batches[execution.id] = run.batch_id

    listeners: list[Callable[[WorkflowEvent], None]] = []

    def submit(
        execution_id: str,
        value: str,
        *,
        event_listener: Callable[[WorkflowEvent], None] | None = None,
        admission_gate: Callable[[], bool] | None = None,
    ) -> _Handle:
        assert execution_id == execution.id
        assert value == "Updated input"
        assert (admission_gate is not None) is live_batch
        if live_batch:
            assert not run.condition._is_owned()
        if event_listener is not None:
            listeners.append(event_listener)
        execution.status = WorkflowStatus.PENDING if live_batch else WorkflowStatus.APPROVED
        if event_listener is not None:
            event_listener(WorkflowEvent(
                execution.id, WorkflowEventKind.STATUS_CHANGED, status=execution.status
            ))
        # The handle result is deliberately stale: the detail must read persisted state.
        return _Handle(WorkflowExecution(id=execution.id, status=WorkflowStatus.CHANGES_REQUESTED))

    monkeypatch.setattr(
        service,
        "submit_continuation" if action == "continuation" else "submit_response",
        submit,
    )
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test(size=(36, 14)) as pilot:
            application.push_screen(ExecutionDetailScreen(execution.id))
            await settle_tui(pilot)
            screen = application.screen
            assert isinstance(screen, ExecutionDetailScreen)
            status = screen.query_one("#execution-status", Static)
            assert str(status.renderable) == (
                "CHANGES_REQUESTED" if action == "continuation" else "AWAITING HUMAN"
            )
            if action == "continuation":
                feedback = screen.query_one("#execution-continuation-feedback", TextArea)
                feedback.text = "Updated input"
                button = screen.query_one("#execution-continue", Button)
            else:
                screen.query_one("#execution-human-response", TextArea).text = "Updated input"
                button = screen.query_one("#execution-resume", Button)
            await settle_tui(pilot)
            if action == "answer" and live_batch:
                def unavailable(_: str) -> list[Any]:
                    raise RuntimeError("related rows unavailable")

                monkeypatch.setattr(repository, "list_steps", unavailable)
                monkeypatch.setattr(repository, "list_diagnostics", unavailable)
            button.press()
            expected = "QUEUED" if live_batch else "APPROVED"
            await wait_for_tui(pilot, lambda: str(status.renderable) == expected)
            assert application.screen is screen
            if live_batch:
                assert screen._reconcile_timer is not None
            if live_batch:
                assert listeners
                assert run.generations[0] == 1
                assert batch_events == [WorkflowEventKind.STATUS_CHANGED]
                execution.status = WorkflowStatus.RUNNING
                await asyncio.to_thread(
                    listeners[0],
                    WorkflowEvent(
                        execution.id, WorkflowEventKind.STATUS_CHANGED,
                        status=WorkflowStatus.RUNNING,
                    ),
                )
                await wait_for_tui(pilot, lambda: str(status.renderable) == "RUNNING")
                assert application.screen is screen
                cancel = screen.query_one("#execution-cancel", Button)
                assert cancel.display and not cancel.disabled
                cancel.focus()
                await settle_tui(pilot)
                assert application.focused is cancel
                assert 0 <= cancel.region.x < cancel.region.right <= application.size.width
                assert 0 <= cancel.region.y < cancel.region.bottom <= application.size.height
            await pilot.press("escape")
            await settle_tui(pilot)
            if live_batch:
                execution.status = WorkflowStatus.APPROVED
                await asyncio.to_thread(
                    listeners[0],
                    WorkflowEvent(
                        execution.id, WorkflowEventKind.WORKFLOW_FINISHED,
                        status=WorkflowStatus.APPROVED,
                    ),
                )
            application.push_screen(ExecutionDetailScreen(execution.id))
            await settle_tui(pilot)
            assert str(
                application.screen.query_one("#execution-status", Static).renderable
            ) == "APPROVED"

    asyncio.run(drive())


def test_execution_detail_submission_refresh_uses_latest_persisted_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller, service, repository = _controller(tmp_path)
    execution = repository.execution
    execution.status = WorkflowStatus.CHANGES_REQUESTED
    execution.workspace_path = str(tmp_path / "worktree")
    execution.working_branch = "workflow/12"
    run = _BatchRun("batch-1", 1, frozenset(), None)
    run.positions[execution.id] = 0
    run.statuses[0] = execution.status
    controller._active_batches[run.batch_id] = run
    controller._execution_batches[execution.id] = run.batch_id

    def submit(
        execution_id: str,
        feedback: str,
        *,
        event_listener: Callable[[WorkflowEvent], None] | None = None,
        admission_gate: Callable[[], bool] | None = None,
    ) -> _Handle:
        assert event_listener is not None
        execution.status = WorkflowStatus.PENDING
        event_listener(WorkflowEvent(
            execution_id, WorkflowEventKind.STATUS_CHANGED, status=WorkflowStatus.PENDING
        ))
        execution.status = WorkflowStatus.RUNNING
        event_listener(WorkflowEvent(
            execution_id, WorkflowEventKind.STATUS_CHANGED, status=WorkflowStatus.RUNNING
        ))
        return _Handle(WorkflowExecution(id=execution_id, status=WorkflowStatus.PENDING))

    monkeypatch.setattr(service, "submit_continuation", submit)
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test() as pilot:
            application.push_screen(ExecutionDetailScreen(execution.id))
            await settle_tui(pilot)
            screen = application.screen
            screen.query_one("#execution-continue", Button).press()
            await wait_for_tui(
                pilot,
                lambda: str(screen.query_one("#execution-status", Static).renderable) == "RUNNING",
            )
            await settle_tui(pilot)
            assert str(screen.query_one("#execution-status", Static).renderable) == "RUNNING"

    asyncio.run(drive())


def test_batch_answer_and_other_event_finish_while_detail_ui_is_busy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller, service, repository = _controller(tmp_path)
    execution = repository.execution
    execution.status = WorkflowStatus.NEEDS_HUMAN
    execution.current_stage = WorkflowStage.REVIEW
    execution.human_interactions = [HumanInteraction(
        id=1, workflow_execution_id=execution.id, requesting_stage=WorkflowStage.REVIEW,
        prompt="Clarify the change.", requested_at=datetime.now(UTC),
    )]
    other = WorkflowExecution(id="execution-2", status=WorkflowStatus.WAITING)
    repository.executions.append(other)
    batch = repository.create_batch("example", "github", [
        DevelopmentTask("12", "github", "First task", "", "https://example/12"),
        DevelopmentTask("7", "github", "Second task", "", "https://example/7"),
    ])
    repository.record_batch_item_execution(batch.id, 0, execution.id)
    repository.record_batch_item_execution(batch.id, 1, other.id)
    run = _BatchRun(batch.id, 2, frozenset({"planner"}), None)
    run.positions = {execution.id: 0, other.id: 1}
    run.statuses = {0: WorkflowStatus.NEEDS_HUMAN, 1: WorkflowStatus.WAITING}
    controller._active_batches[batch.id] = run
    controller._execution_batches.update({execution.id: batch.id, other.id: batch.id})

    submission_entered = threading.Event()
    release_submission = threading.Event()
    response_event_started = threading.Event()
    submission_done = threading.Event()
    other_done = threading.Event()
    ui_busy = threading.Event()
    release_ui = threading.Event()
    outcomes: list[bool] = []

    def submit(
        execution_id: str, response: str, *, event_listener: Any,
        admission_gate: Any,
    ) -> _Handle:
        assert response == "Answer" and admission_gate()
        submission_entered.set()
        assert release_submission.wait(3)
        execution.status = WorkflowStatus.PENDING
        response_event_started.set()
        event_listener(WorkflowEvent(
            execution_id, WorkflowEventKind.STATUS_CHANGED, status=WorkflowStatus.PENDING,
        ))
        return _Handle(WorkflowExecution(id=execution_id, status=WorkflowStatus.PENDING))

    monkeypatch.setattr(service, "submit_response", submit)
    original_submit = controller.submit_response

    def tracked_submit(*args: Any, **kwargs: Any) -> _Handle:
        try:
            return original_submit(*args, **kwargs)
        finally:
            submission_done.set()

    monkeypatch.setattr(controller, "submit_response", tracked_submit)
    application = OrchestratorApp(controller)

    def emit_other() -> None:
        try:
            other.status = WorkflowStatus.RUNNING
            controller._observe_batch_event(run, 1, WorkflowEvent(
                other.id, WorkflowEventKind.STATUS_CHANGED, status=WorkflowStatus.RUNNING,
            ))
            controller._observe_batch_event(run, 1, WorkflowEvent(
                other.id, WorkflowEventKind.DIAGNOSTIC,
                payload={"kind": "PROVIDER_QUOTA_EXHAUSTED", "provider": "planner"},
            ))
        finally:
            other_done.set()

    def monitor() -> None:
        emitter: threading.Thread | None = None
        try:
            if not ui_busy.wait(2):
                outcomes.append(False)
                return
            release_submission.set()
            if not response_event_started.wait(2):
                outcomes.append(False)
                return
            emitter = threading.Thread(target=emit_other, daemon=True)
            emitter.start()
            outcomes.append(submission_done.wait(2) and other_done.wait(2))
        finally:
            release_submission.set()
            release_ui.set()
            if emitter is not None:
                emitter.join(timeout=2)

    async def drive() -> None:
        async with application.run_test(size=(120, 50)) as pilot:
            application.push_screen(WorkflowRunnerScreen())
            await settle_tui(pilot)
            runner = application.screen
            assert isinstance(runner, WorkflowRunnerScreen)
            run.listener = runner._on_workflow_event
            runner._batch_running = True
            runner._batch_created(repository.get_batch(batch.id), _batch_launch("12", "7"))
            application.push_screen(ExecutionDetailScreen(execution.id))
            await settle_tui(pilot)
            detail = application.screen
            assert isinstance(detail, ExecutionDetailScreen)
            detail.query_one("#execution-human-response", TextArea).text = "Answer"
            await settle_tui(pilot)
            detail.query_one("#execution-resume", Button).press()
            assert await asyncio.to_thread(submission_entered.wait, 2)
            monitor_thread = threading.Thread(target=monitor, daemon=True)
            monitor_thread.start()

            def busy_ui() -> None:
                ui_busy.set()
                assert release_ui.wait(6)

            application.call_later(busy_ui)
            await asyncio.to_thread(monitor_thread.join, 7)
            assert not monitor_thread.is_alive()
            assert outcomes == [True], "workflow events waited for the busy UI"
            await wait_for_tui(
                pilot,
                lambda: str(detail.query_one("#execution-status", Static).renderable) == "QUEUED",
            )
            await pilot.press("escape")
            await wait_for_tui(
                pilot,
                lambda: "RUNNING" in str(
                    runner.query_one("#batch-row-1-header", Button).label
                ) and "planner" in runner.query_one(
                    "#batch-progress", BatchProgressView
                ).unavailable_providers,
            )
            assert "QUEUED" in str(runner.query_one("#batch-row-0-header", Button).label)
            assert "planner" in runner.query_one(
                "#batch-progress", BatchProgressView
            ).unavailable_providers
            assert run.generations[0] == 1

    asyncio.run(drive())


def test_execution_detail_reenables_continue_after_continuation_error(tmp_path: Path) -> None:
    controller, service, repository = _controller(tmp_path)
    repository.execution.status = WorkflowStatus.CHANGES_REQUESTED
    repository.execution.workspace_path = str(tmp_path / "worktree")
    repository.execution.working_branch = "workflow/12"
    service.continuation_error = RuntimeError("workspace is unavailable")
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test() as pilot:
            application.push_screen(ExecutionDetailScreen(repository.execution.id))
            await settle_tui(pilot)
            screen = application.screen
            button = screen.query_one("#execution-continue", Button)
            assert button.disabled is False
            button.press()
            await wait_for_tui(pilot, lambda: bool(service.continuations))
            await wait_for_tui(pilot, lambda: button.disabled is False)

    asyncio.run(drive())


def test_execution_detail_checks_continuation_eligibility_once_per_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller, _, repository = _controller(tmp_path)
    repository.execution.status = WorkflowStatus.FAILED
    reads = 0
    original_list = repository.list_diagnostics

    def tracked_list(execution_id: str) -> list[ExecutionDiagnostic]:
        nonlocal reads
        reads += 1
        return original_list(execution_id)

    monkeypatch.setattr(repository, "list_diagnostics", tracked_list)
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test() as pilot:
            application.push_screen(ExecutionDetailScreen(repository.execution.id))
            await settle_tui(pilot)
            screen = application.screen
            assert isinstance(screen, ExecutionDetailScreen)
            assert reads == 1  # Continuation eligibility check only.

            screen._refresh_execution()
            screen._refresh_execution()
            assert reads == 1

            repository.execution.status = WorkflowStatus.RUNNING
            screen._refresh_execution()
            repository.execution.status = WorkflowStatus.FAILED
            screen._refresh_execution()
            assert reads == 2

    asyncio.run(drive())


def test_execution_detail_copies_all_persisted_detailed_logs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller, _, repository = _controller(tmp_path)
    repository.diagnostics = [
        ExecutionDiagnostic(
            execution_id=repository.execution.id,
            category=DiagnosticCategory.REQUEST,
            stage=WorkflowStage.IMPLEMENT,
            role=AgentRole.IMPLEMENTER,
            payload={
                "effective_instructions": "implement the requested change",
                "instructions": "implement the requested change",
            },
        ),
        ExecutionDiagnostic(
            execution_id=repository.execution.id,
            category=DiagnosticCategory.RESPONSE,
            stage=WorkflowStage.IMPLEMENT,
            role=AgentRole.IMPLEMENTER,
            payload={"output": "\n".join(f"response line {number}" for number in range(400))},
        ),
    ]
    application = OrchestratorApp(controller)
    copied: list[str] = []
    monkeypatch.setattr(application, "copy_to_clipboard", copied.append)

    async def drive() -> None:
        async with application.run_test() as pilot:
            application.push_screen(ExecutionDetailScreen(repository.execution.id))
            await settle_tui(pilot)
            screen = application.screen
            screen.query_one("#execution-toggle-logs", Button).press()
            await settle_tui(pilot)
            detailed_logs = str(screen.query_one("#execution-detailed-logs", Log).lines)
            assert detailed_logs.count("implement the requested change") == 1
            copy_button = screen.query_one("#copy-detailed-logs-button", Button)
            assert not copy_button.disabled
            copy_button.press()
            await settle_tui(pilot)

    asyncio.run(drive())

    assert copied == [
        format_diagnostics_for_export(controller.execution_diagnostics(repository.execution.id))
    ]
    assert "=== REQUEST ===" in copied[0]
    assert "=== RESPONSE ===" in copied[0]
    assert "response line 399" in copied[0]
    assert copied[0].count("implement the requested change") == 1


def test_execution_detail_can_copy_after_terminal_initial_diagnostic_read_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller, _, repository = _controller(tmp_path)
    repository.diagnostics = [ExecutionDiagnostic(
        execution_id=repository.execution.id,
        category=DiagnosticCategory.ERROR,
        payload={"message": "persisted failure"},
    )]
    original_list = repository.list_diagnostics
    reads = 0

    def fail_once(execution_id: str) -> list[ExecutionDiagnostic]:
        nonlocal reads
        reads += 1
        if reads == 1:
            raise RuntimeError("temporary storage failure")
        return original_list(execution_id)

    monkeypatch.setattr(repository, "list_diagnostics", fail_once)
    application = OrchestratorApp(controller)
    copied: list[str] = []
    monkeypatch.setattr(application, "copy_to_clipboard", copied.append)

    async def drive() -> None:
        async with application.run_test() as pilot:
            application.push_screen(ExecutionDetailScreen(repository.execution.id))
            await settle_tui(pilot)
            screen = application.screen
            assert isinstance(screen, ExecutionDetailScreen)
            assert screen._reconcile_timer is None
            assert not screen.query_one("#copy-detailed-logs-button", Button).disabled
            screen.query_one("#execution-toggle-logs", Button).press()
            await settle_tui(pilot)
            assert not screen._diagnostics_loaded
            screen.query_one("#copy-detailed-logs-button", Button).press()
            await settle_tui(pilot)

    asyncio.run(drive())

    # Copying makes a fresh read even after expanding the logs failed.
    assert reads == 2
    assert copied == [format_diagnostics_for_export(original_list(repository.execution.id))]


def test_execution_detail_appends_only_new_diagnostics_and_preserves_ui_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller, _, repository = _controller(tmp_path)
    execution = repository.execution
    execution.status = WorkflowStatus.NEEDS_HUMAN
    execution.human_interactions = [HumanInteraction(
        id=1,
        workflow_execution_id=execution.id,
        requesting_stage=WorkflowStage.GENERATE_SPEC,
        prompt="Clarify the task",
        requested_at=datetime.now(UTC),
    )]
    repository.diagnostics = [ExecutionDiagnostic(
        execution_id=execution.id,
        category=DiagnosticCategory.RESPONSE,
        payload={"output": "old" * 200},
    )]
    application = OrchestratorApp(controller)
    copied: list[str] = []
    monkeypatch.setattr(application, "copy_to_clipboard", copied.append)

    async def drive() -> None:
        async with application.run_test(size=(36, 14)) as pilot:
            application.push_screen(ExecutionDetailScreen(execution.id))
            await settle_tui(pilot)
            screen = application.screen
            assert isinstance(screen, ExecutionDetailScreen)
            screen.query_one("#execution-toggle-logs", Button).press()
            await settle_tui(pilot)
            response = screen.query_one("#execution-human-response", TextArea)
            response.text = "My edited answer"
            response.focus()
            page = screen.query_one("#execution-detail", VerticalScroll)
            log = screen.query_one("#execution-detailed-logs", Log)
            page.scroll_to(y=page.max_scroll_y, animate=False)
            log.scroll_to(x=log.max_scroll_x, animate=False)
            await settle_tui(pilot)
            page_y, log_x = page.scroll_y, log.scroll_x
            lines_before = len(log.lines)

            def no_full_read(_id: str) -> list[ExecutionDiagnostic]:
                raise AssertionError("Periodic refresh must not read full diagnostics")

            with monkeypatch.context() as patch:
                patch.setattr(repository, "list_diagnostics", no_full_read)
                repository.diagnostics.append(ExecutionDiagnostic(
                    execution_id=execution.id,
                    category=DiagnosticCategory.RESPONSE,
                    payload={"output": "new"},
                ))
                screen._refresh_execution()
                await settle_tui(pilot)
                assert len(log.lines) > lines_before
                appended_lines = len(log.lines)
                screen._refresh_execution()
                await settle_tui(pilot)
                assert len(log.lines) == appended_lines
            assert response.text == "My edited answer"
            assert application.focused is response
            assert page.scroll_y == page_y
            assert log.scroll_x == log_x
            screen.query_one("#execution-toggle-logs", Button).press()
            await settle_tui(pilot)
            assert not log.display
            screen._refresh_execution()
            await settle_tui(pilot)
            assert not log.display
            screen.query_one("#copy-detailed-logs-button", Button).press()
            await settle_tui(pilot)
            assert "new" in copied[0]
            assert "old" in copied[0]

    asyncio.run(drive())


def test_execution_detail_reports_clipboard_failures_without_crashing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller, _, repository = _controller(tmp_path)
    repository.diagnostics = [
        ExecutionDiagnostic(
            execution_id=repository.execution.id,
            category=DiagnosticCategory.ERROR,
            payload={"message": "validation failed"},
        )
    ]
    application = OrchestratorApp(controller)
    notifications: list[tuple[str, str | None]] = []

    def clipboard_failure(_: str) -> None:
        raise RuntimeError("clipboard integration is unavailable")

    def record_notification(
        _screen: ExecutionDetailScreen, message: str, **kwargs: Any
    ) -> None:
        notifications.append((message, kwargs.get("severity")))

    monkeypatch.setattr(application, "copy_to_clipboard", clipboard_failure)
    monkeypatch.setattr(ExecutionDetailScreen, "notify", record_notification)

    async def drive() -> None:
        async with application.run_test() as pilot:
            application.push_screen(ExecutionDetailScreen(repository.execution.id))
            await settle_tui(pilot)
            application.screen.query_one("#copy-detailed-logs-button", Button).press()
            await settle_tui(pilot)

    asyncio.run(drive())

    assert notifications == [
        ("Could not copy detailed logs: clipboard integration is unavailable", "error")
    ]


def test_workflow_runner_submits_selected_task_id_in_workflow_launch(
    tmp_path: Path,
) -> None:
    controller, service, _ = _controller(tmp_path)
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test(size=(80, 20)) as pilot:
            await pilot.press("n")
            screen = application.screen
            content = screen.query_one("#workflow-runner", VerticalScroll)
            assert content.max_scroll_y > 0

            await settle_tui(pilot)
            screen.query_one("#task-checkbox-0", Checkbox).value = True
            await settle_tui(pilot)
            assert screen.query_one("#task-selector", TaskSelector).selected_task_id == "12"
            start_button = screen.query_one("#start-workflow", Button)
            start_button.focus()
            await wait_for_tui(
                pilot,
                lambda: start_button.has_focus
                and start_button.region.y >= content.content_region.y
                and start_button.region.bottom <= content.content_region.bottom,
                description="focused Start workflow button in the runner viewport",
            )
            assert start_button.has_focus
            assert start_button.region.y >= content.content_region.y
            assert start_button.region.bottom <= content.content_region.bottom
            await pilot.press("enter")
            await settle_tui(pilot)
            assert service.calls[0][0:2] == ("12", "example")

    asyncio.run(drive())


def test_workflow_runner_start_with_nothing_selected_submits_nothing(
    tmp_path: Path,
) -> None:
    controller, service, repository = _controller(tmp_path)
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test() as pilot:
            await pilot.press("n")
            await settle_tui(pilot)
            application.screen.query_one("#start-workflow", Button).press()
            await settle_tui(pilot)

    asyncio.run(drive())

    assert service.calls == []
    assert repository.batch_rows == []


def test_workflow_runner_refresh_preserves_selection_for_same_task_source(
    tmp_path: Path,
) -> None:
    controller, _, _ = _controller(tmp_path)
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test() as pilot:
            await pilot.press("n")
            await wait_for_tui(
                pilot,
                lambda: len(application.screen.query(".task-row")) == 1,
                description="initial task list",
            )
            screen = application.screen
            screen.query_one("#task-checkbox-0", Checkbox).value = True
            await settle_tui(pilot)
            screen.query_one("#refresh-tasks", Button).press()
            await wait_for_tui(
                pilot,
                lambda: (
                    len(screen.query(".task-row")) == 1
                    and screen.query_one(
                        "#task-selector", TaskSelector
                    ).selected_task_ids
                    == ("12",)
                ),
                description="refreshed task selection",
            )
            assert screen.query_one("#task-checkbox-0", Checkbox).value is True
            assert str(screen.query_one("#start-workflow", Button).label) == (
                "Start workflow (1)"
            )

    asyncio.run(drive())


def test_workflow_runner_resume_does_not_reload_tasks_during_single_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller, _, _ = _controller(tmp_path)
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test() as pilot:
            await pilot.press("n")
            await wait_for_tui(
                pilot,
                lambda: len(application.screen.query(".task-row")) == 1,
                description="initial task list",
            )
            screen = application.screen
            loads: list[tuple[str, str]] = []
            monkeypatch.setattr(
                screen,
                "_request_task_load",
                lambda repository, provider: loads.append((repository, provider)),
            )
            screen.query_one("#start-workflow", Button).disabled = True

            screen.on_screen_resume()
            await settle_tui(pilot)

            assert loads == []

    asyncio.run(drive())


def test_workflow_runner_keeps_tasks_when_execution_status_lookup_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller, _, _ = _controller(tmp_path)

    def fail_status_lookup(*args: Any, **kwargs: Any) -> dict[str, WorkflowExecution]:
        raise RuntimeError("status database unavailable")

    monkeypatch.setattr(controller, "task_execution_status", fail_status_lookup)
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test() as pilot:
            await pilot.press("n")
            await wait_for_tui(
                pilot,
                lambda: len(application.screen.query(".task-row")) == 1,
                description="task list without execution statuses",
            )
            checkbox = application.screen.query_one("#task-checkbox-0", Checkbox)
            assert checkbox.disabled is False
            assert "12:" in str(checkbox.label)

    asyncio.run(drive())


def test_execution_history_reopens_persisted_batch_and_linked_execution(
    tmp_path: Path,
) -> None:
    controller, _, repository = _controller(tmp_path)
    repository.execution.task_id = "12"
    repository.execution.task_source = "github"
    repository.execution.repository_ref = "example"
    batch = repository.create_batch(
        "example",
        "github",
        [DevelopmentTask("12", "github", "Persisted task", "", "https://example/12")],
    )
    repository.record_batch_item_execution(batch.id, 0, repository.execution.id)
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test() as pilot:
            application.push_screen(ExecutionHistoryScreen())
            await settle_tui(pilot)
            history = application.screen
            assert "1 persisted workflow batch(es)." in str(
                history.query_one("#batch-history-summary", Static).renderable
            )
            history.query_one("#batch-picker", OrchestratorSelect).value = batch.id
            history.query_one("#inspect-batch", Button).press()
            await settle_tui(pilot)
            assert isinstance(application.screen, BatchDetailScreen)

            detail = application.screen
            detail.query_one("#batch-row-0-header", Button).press()
            await settle_tui(pilot)
            detail.query_one("#batch-row-0-open-execution", Button).press()
            await settle_tui(pilot)
            assert isinstance(application.screen, ExecutionDetailScreen)

    asyncio.run(drive())


def test_execution_history_filter_does_not_reload_batches_or_execution_steps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller, _, repository = _controller(tmp_path)
    batch = repository.create_batch(
        "example",
        "github",
        [DevelopmentTask("12", "github", "Persisted task", "", "https://example/12")],
    )
    repository.record_batch_item_execution(batch.id, 0, repository.execution.id)
    batch_loads = 0
    original_list_batches = repository.list_batches

    def list_batches() -> list[WorkflowBatch]:
        nonlocal batch_loads
        batch_loads += 1
        return original_list_batches()

    def fail_step_load(execution_id: str) -> list[Any]:
        pytest.fail(f"history loaded steps for {execution_id}")

    monkeypatch.setattr(repository, "list_batches", list_batches)
    monkeypatch.setattr(repository, "list_steps", fail_step_load)
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test() as pilot:
            application.push_screen(ExecutionHistoryScreen())
            await settle_tui(pilot)
            assert batch_loads == 1
            application.screen.query_one("#history-filter", Input).value = "execution"
            await settle_tui(pilot)
            assert batch_loads == 1

    asyncio.run(drive())


def test_workflow_runner_selects_available_tasks_and_starts_batch_in_list_order(
    tmp_path: Path,
) -> None:
    controller, service, repository = _controller(tmp_path)
    repository.execution.task_source = "github"
    repository.execution.task_id = "7"
    repository.execution.repository_ref = "example"
    repository.execution.status = WorkflowStatus.RUNNING
    tasks = [
        DevelopmentTask(
            "12", "github", "First", "", "https://example/12", metadata={"labels": ["bug"]}
        ),
        DevelopmentTask(
            "7", "github", "Busy", "", "https://example/7", metadata={"labels": ["active"]}
        ),
        DevelopmentTask(
            "3", "github", "Third", "", "https://example/3", metadata={"labels": []}
        ),
    ]
    provider = _TaskProvider()
    provider.list_tasks = lambda limit=50: tasks  # type: ignore[method-assign]
    controller._task_provider_factory = lambda source, selected_repository: provider
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test(size=(80, 20)) as pilot:
            await pilot.press("n")
            await wait_for_tui(
                pilot,
                lambda: len(application.screen.query(".task-row")) == 3,
                description="task checkboxes",
            )
            screen = application.screen
            selector = screen.query_one("#task-selector", TaskSelector)
            first = screen.query_one("#task-checkbox-0", Checkbox)
            blocked = screen.query_one("#task-checkbox-1", Checkbox)
            third = screen.query_one("#task-checkbox-2", Checkbox)
            header = screen.query_one("#select-all-tasks", Checkbox)

            assert "bug" in str(first.label)
            assert blocked.disabled is True
            assert "RUNNING" in str(blocked.label)
            header.value = True
            await settle_tui(pilot)
            assert selector.selected_task_ids == ("12", "3")
            assert first.value is True and third.value is True

            first.value = False
            await settle_tui(pilot)
            assert selector.selected_task_ids == ("3",)
            assert header.value is False

            header.value = True
            await settle_tui(pilot)
            screen.query_one("#start-workflow", Button).press()
            await wait_for_tui(
                pilot,
                lambda: len(service.calls) == 2,
                description="ordered batch submissions",
            )
            assert [call[0] for call in service.calls] == ["12", "3"]

    asyncio.run(drive())


def test_fresh_run_confirmation_preserves_selection_and_starts_checked_task(
    tmp_path: Path,
) -> None:
    controller, service, repository = _controller(tmp_path)
    repository.execution.task_source = "github"
    repository.execution.task_id = "12"
    repository.execution.repository_ref = "example"
    repository.execution.status = WorkflowStatus.NEEDS_HUMAN
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test() as pilot:
            await pilot.press("n")
            await wait_for_tui(
                pilot,
                lambda: len(application.screen.query(TaskRow)) == 1,
                description="fresh-run row",
            )
            screen = application.screen
            row = screen.query_one("#task-checkbox-0", TaskRow)
            assert row.disabled is False
            assert "Latest execution: NEEDS_HUMAN" in str(row.label)
            assert "earlier runs remain in Execution history" in str(row.label)
            assert screen.query_one("#select-all-tasks", Checkbox).disabled is True
            row.value = True
            await settle_tui(pilot)
            screen.query_one("#start-workflow", Button).press()
            await wait_for_tui(
                pilot,
                lambda: isinstance(application.screen, FreshRunConfirmation),
                description="fresh-run confirmation",
            )
            modal = application.screen
            assert modal.query_one("#fresh-run-0", Checkbox).value is True
            assert any(
                "pending question" in str(widget.renderable)
                for widget in modal.query(Static)
            )
            modal.query_one("#fresh-run-0", Checkbox).value = False
            await settle_tui(pilot)
            assert modal.query_one("#confirm-fresh-run", Button).disabled is True
            modal.query_one("#dismiss-fresh-run", Button).press()
            await settle_tui(pilot)
            assert screen.query_one("#task-selector", TaskSelector).selected_task_ids == ("12",)
            screen.query_one("#start-workflow", Button).press()
            await wait_for_tui(
                pilot,
                lambda: isinstance(application.screen, FreshRunConfirmation),
                description="fresh-run confirmation again",
            )
            application.screen.query_one("#confirm-fresh-run", Button).press()
            await wait_for_tui(
                pilot, lambda: len(service.calls) == 1,
                description="confirmed fresh run",
            )
            assert service.calls[0][0] == "12"

    asyncio.run(drive())


def test_fresh_run_confirmation_omits_unchecked_prior_run_but_launches_new_task(
    tmp_path: Path,
) -> None:
    controller, service, repository = _controller(tmp_path)
    repository.execution.task_source = "github"
    repository.execution.task_id = "12"
    repository.execution.repository_ref = "example"
    repository.execution.status = WorkflowStatus.CHANGES_REQUESTED
    repository.execution.pr_url = "https://example/pr/1"
    provider = _TaskProvider()
    provider.list_tasks = lambda limit=50: [
        DevelopmentTask("12", "github", "Retry", "", "https://example/12"),
        DevelopmentTask("7", "github", "New", "", "https://example/7"),
    ]
    controller._task_provider_factory = lambda source, selected_repository: provider
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test() as pilot:
            await pilot.press("n")
            await wait_for_tui(
                pilot, lambda: len(application.screen.query(TaskRow)) == 2,
                description="mixed fresh-run tasks",
            )
            screen = application.screen
            screen.query_one("#select-all-tasks", Checkbox).value = True
            await settle_tui(pilot)
            screen.query_one("#task-checkbox-0", TaskRow).value = True
            await settle_tui(pilot)
            assert screen.query_one("#task-selector", TaskSelector).selected_task_ids == ("12", "7")
            screen.query_one("#start-workflow", Button).press()
            await wait_for_tui(
                pilot, lambda: isinstance(application.screen, FreshRunConfirmation),
                description="mixed fresh-run confirmation",
            )
            modal = application.screen
            assert any(
                "second PR" in str(widget.renderable) for widget in modal.query(Static)
            )
            modal.query_one("#fresh-run-0", Checkbox).value = False
            await settle_tui(pilot)
            assert modal.query_one("#confirm-fresh-run", Button).disabled is False
            modal.query_one("#confirm-fresh-run", Button).press()
            await wait_for_tui(
                pilot, lambda: len(service.calls) == 1,
                description="new task launch after excluding rerun",
            )
            assert [call[0] for call in service.calls] == ["7"]

    asyncio.run(drive())


@pytest.mark.parametrize("size", [(40, 20), (30, 16)])
def test_fresh_run_confirmation_fits_narrow_terminal_and_keeps_actions_visible(
    size: tuple[int, int],
) -> None:
    task = DevelopmentTask("12", "github", "A task with a long title", "", "https://example/12")
    execution = WorkflowExecution("earlier", status=WorkflowStatus.NEEDS_HUMAN)

    class ConfirmationApp(App[None]):
        def on_mount(self) -> None:
            self.push_screen(FreshRunConfirmation(((task, execution),), 0))

    async def drive() -> None:
        application = ConfirmationApp()
        async with application.run_test(size=size) as pilot:
            await settle_tui(pilot)
            modal = application.screen
            dialog = modal.query_one("#fresh-run-confirmation", Vertical)
            launch = modal.query_one("#confirm-fresh-run", Button)
            back = modal.query_one("#dismiss-fresh-run", Button)
            assert str(modal.query_one("#fresh-run-0", Checkbox).label).startswith(
                "12 [NEEDS_HUMAN]"
            )
            assert dialog.region.x >= 0
            assert dialog.region.x + dialog.region.width <= size[0]
            assert dialog.region.y >= 0
            assert dialog.region.y + dialog.region.height <= size[1]
            for button in (launch, back):
                assert button.region.x >= 0
                assert button.region.x + button.region.width <= size[0]
                assert button.region.y >= 0
                assert button.region.y + button.region.height <= size[1]
            assert launch.disabled is False

    asyncio.run(drive())


def _batch_launch(*task_ids: str) -> WorkflowBatchLaunch:
    return WorkflowBatchLaunch(
        repository="example",
        task_provider="github",
        task_ids=task_ids,
        agent_providers={
            AgentRole.PLANNER: "planner",
            AgentRole.IMPLEMENTER: "implementer",
            AgentRole.REVIEWER: "reviewer",
        },
    )


def _index_event(execution_id: str, message: str, position: int | None = None) -> WorkflowEvent:
    payload: dict[str, Any] = {"kind": "REPOSITORY_INDEX_PROGRESS", "message": message}
    if position is not None:
        payload["batch_position"] = position
    return WorkflowEvent(
        execution_id=execution_id,
        kind=WorkflowEventKind.DIAGNOSTIC,
        stage=WorkflowStage.GENERATE_SPEC,
        category=DiagnosticCategory.WORKFLOW_EVENT.value,
        payload=payload,
    )


@pytest.mark.parametrize("batch_run", [False, True])
def test_workflow_runner_drains_index_burst_before_later_stage_and_status(
    tmp_path: Path, batch_run: bool
) -> None:
    controller, _, _ = _controller(tmp_path)
    application = OrchestratorApp(controller)
    batch = WorkflowBatch(
        id="batch-burst", repository="example", task_source="github",
        items=(WorkflowBatchItem(0, "12", "First"), WorkflowBatchItem(1, "7", "Second")),
    )

    async def drive() -> None:
        async with application.run_test(size=(80, 20)) as pilot:
            application.push_screen(WorkflowRunnerScreen())
            await settle_tui(pilot)
            screen = application.screen
            assert isinstance(screen, WorkflowRunnerScreen)
            assert screen._reconcile_timer is not None
            screen._reconcile_timer.stop()  # Events here are synthetic, with no durable rows.
            if batch_run:
                screen._batch_running = True
                screen._batch_created(batch, _batch_launch("12", "7"))
                await settle_tui(pilot)

            def emit() -> None:
                positions = range(2) if batch_run else range(1)
                for position in positions:
                    screen._on_workflow_event(WorkflowEvent(
                        f"execution-{position}", WorkflowEventKind.EXECUTION_CREATED,
                        payload={"batch_position": position} if batch_run else None,
                    ))
                    screen._on_workflow_event(WorkflowEvent(
                        f"execution-{position}", WorkflowEventKind.STAGE_STARTED,
                        stage=WorkflowStage.GENERATE_SPEC,
                    ))
                for index in range(1500):
                    for position in positions:
                        screen._on_workflow_event(_index_event(
                            f"execution-{position}", f"Embedding chunks {index}/1500",
                            position if batch_run else None,
                        ))
                for position in positions:
                    execution_id = f"execution-{position}"
                    screen._on_workflow_event(_index_event(
                        execution_id, "Semantic index ready", position if batch_run else None,
                    ))
                    screen._on_workflow_event(WorkflowEvent(
                        execution_id, WorkflowEventKind.STAGE_FINISHED,
                        stage=WorkflowStage.GENERATE_SPEC, step_status=StepStatus.SUCCEEDED,
                    ))
                    screen._on_workflow_event(WorkflowEvent(
                        execution_id, WorkflowEventKind.STAGE_STARTED,
                        stage=WorkflowStage.REVIEW,
                    ))
                    screen._on_workflow_event(WorkflowEvent(
                        execution_id, WorkflowEventKind.STATUS_CHANGED,
                        status=WorkflowStatus.APPROVED,
                    ))
                    screen._on_workflow_event(WorkflowEvent(
                        execution_id, WorkflowEventKind.WORKFLOW_FINISHED,
                        status=WorkflowStatus.APPROVED,
                    ))

            worker = threading.Thread(target=emit, daemon=True)
            worker.start()
            await asyncio.to_thread(worker.join, 5)
            assert not worker.is_alive()
            with screen._event_lock:
                assert sum(1 for event in screen._pending_events if (
                    event.payload or {}
                ).get("kind") == "REPOSITORY_INDEX_PROGRESS") <= (2 if batch_run else 1)
            if batch_run:
                await wait_for_tui(pilot, lambda: (
                    screen._batch_progress is not None
                    and all(row.category == "APPROVED" for row in screen._batch_progress.rows())
                ))
                assert screen._batch_progress is not None
                for row in screen._batch_progress.rows():
                    assert "Reviewing pull request" in "\n".join(row.timeline)
                    assert "Embedding chunks" not in "\n".join(row.timeline)
                    assert "Semantic index ready" in "\n".join(row.timeline)
                    assert row.can_cancel is False
                assert "Approved: 2" in str(
                    screen.query_one("#batch-progress-summary", Static).renderable
                )
                assert screen.query_one("#workflow-stop-actions", Horizontal).display is False
                assert screen.query_one("#start-workflow", Button).disabled is False
                screen._render_workflow_event(_index_event(
                    "execution-0", "Late progress", 0
                ))
                assert "Late progress" not in "\n".join(screen._batch_progress.rows()[0].timeline)
            else:
                await wait_for_tui(pilot, lambda: (
                    not screen._pending_events
                    and "Reviewing pull request" in "\n".join(screen._event_lines)
                    and str(screen.query_one("#workflow-progress", Static).renderable)
                    == screen._status_message(WorkflowStatus.APPROVED)
                ))
                assert "Embedding chunks" not in "\n".join(screen._event_lines)
                assert "Semantic index ready" in "\n".join(screen._event_lines)
                assert screen.query_one("#workflow-stop-actions", Horizontal).display is False
                assert screen.query_one("#start-workflow", Button).disabled is False
                screen._render_workflow_event(_index_event("execution-0", "Late progress"))
                assert "Late progress" not in "\n".join(screen._event_lines)

    asyncio.run(drive())


def test_workflow_runner_reconciles_missed_stage_and_rejects_queued_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller, _, _ = _controller(tmp_path)
    application = OrchestratorApp(controller)
    execution = WorkflowExecution(
        id="execution-reconcile", current_stage=WorkflowStage.REVIEW,
        status=WorkflowStatus.APPROVED,
    )
    steps = [WorkflowStep(
        id=1, workflow_execution_id=execution.id, stage=WorkflowStage.GENERATE_SPEC,
        status=StepStatus.SUCCEEDED, started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
    ), WorkflowStep(
        id=2, workflow_execution_id=execution.id, stage=WorkflowStage.REVIEW,
        status=StepStatus.SUCCEEDED, started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
    )]
    monkeypatch.setattr(controller, "execution_detail", lambda _id: (execution, steps))

    async def drive() -> None:
        async with application.run_test() as pilot:
            application.push_screen(WorkflowRunnerScreen())
            await settle_tui(pilot)
            screen = application.screen
            assert isinstance(screen, WorkflowRunnerScreen)
            screen._active_execution_id = execution.id
            screen._render_workflow_event(
                _index_event(execution.id, "Preparing 1610 semantic chunks")
            )
            stale = _index_event(execution.id, "Embedding chunks 1/1610")
            screen._render_workflow_event(_index_event(execution.id, "Semantic index ready"))
            assert "Preparing 1610" not in "\n".join(screen._event_lines)
            assert "Semantic index ready" in "\n".join(screen._event_lines)
            screen._render_workflow_event(WorkflowEvent(
                execution.id, WorkflowEventKind.STAGE_STARTED, stage=WorkflowStage.REVIEW
            ))
            assert "Preparing 1610" not in "\n".join(screen._event_lines)
            screen._reconcile_visible_execution()
            screen._on_workflow_event(stale)
            await settle_tui(pilot)
            assert "Review completed" in "\n".join(screen._event_lines)
            assert "Semantic index:" not in "\n".join(screen._event_lines)
            assert screen.query_one("#workflow-stop-actions", Horizontal).display is False
            assert screen.query_one("#start-workflow", Button).disabled is False

    asyncio.run(drive())


def test_workflow_runner_reconciles_live_batch_row_without_remounting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller, _, _ = _controller(tmp_path)
    application = OrchestratorApp(controller)
    execution = WorkflowExecution(
        id="execution-batch-reconcile", current_stage=WorkflowStage.REVIEW,
        status=WorkflowStatus.APPROVED,
    )
    steps = [WorkflowStep(
        id=1, workflow_execution_id=execution.id, stage=WorkflowStage.REVIEW,
        status=StepStatus.SUCCEEDED, started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
    )]
    batch = WorkflowBatch(
        id="batch-reconcile", repository="example", task_source="github",
        items=(WorkflowBatchItem(0, "12", "First", execution.id),),
    )
    monkeypatch.setattr(controller, "batch_detail", lambda _id: batch)
    monkeypatch.setattr(controller, "execution_detail", lambda _id: (execution, steps))

    async def drive() -> None:
        async with application.run_test(size=(80, 20)) as pilot:
            application.push_screen(WorkflowRunnerScreen())
            await settle_tui(pilot)
            screen = application.screen
            assert isinstance(screen, WorkflowRunnerScreen)
            screen._batch_running = True
            screen._batch_created(batch, _batch_launch("12"))
            await settle_tui(pilot)
            view = screen.query_one("#batch-progress", BatchProgressView)
            widget = view.row(0)
            assert widget is not None
            widget.query_one("#batch-row-0-header", Button).press()
            stale = _index_event(execution.id, "Embedding chunks 1/1610", 0)
            screen._render_workflow_event(_index_event(
                execution.id, "Preparing 1610 semantic chunks", 0
            ))
            screen._render_workflow_event(_index_event(execution.id, "Semantic index ready", 0))
            assert "Preparing 1610" not in "\n".join(screen._batch_progress.rows()[0].timeline)
            screen._render_workflow_event(WorkflowEvent(
                execution.id, WorkflowEventKind.STAGE_STARTED, stage=WorkflowStage.REVIEW
            ))
            assert screen._batch_progress is not None
            assert "Preparing 1610" not in "\n".join(
                screen._batch_progress.rows()[0].timeline
            )
            screen._reconcile_visible_execution()
            screen._on_workflow_event(stale)
            await settle_tui(pilot)
            assert view.row(0) is widget
            assert widget.query_one("#batch-row-0-body", Vertical).display is True
            row = screen._batch_progress.rows()[0] if screen._batch_progress else None
            assert row is not None and row.category == "APPROVED"
            assert "Review completed" in "\n".join(row.timeline)
            assert "Semantic index:" not in "\n".join(row.timeline)
            assert "Approved: 1" in str(
                screen.query_one("#batch-progress-summary", Static).renderable
            )
            assert screen.query_one("#workflow-stop-actions", Horizontal).display is False
            assert screen.query_one("#start-workflow", Button).disabled is False

    asyncio.run(drive())


def test_persisted_batch_row_does_not_append_finished_index_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller, _, _ = _controller(tmp_path)
    execution = WorkflowExecution(
        id="execution-persisted", current_stage=WorkflowStage.REVIEW,
        status=WorkflowStatus.APPROVED,
    )
    steps = [WorkflowStep(
        id=1, workflow_execution_id=execution.id, stage=WorkflowStage.REVIEW,
        status=StepStatus.SUCCEEDED, started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
    )]
    monkeypatch.setattr(controller, "execution_detail", lambda _id: (execution, steps))
    monkeypatch.setattr(controller, "execution_diagnostics", lambda _id: [
        ExecutionDiagnostic(
            execution_id=execution.id, category=DiagnosticCategory.WORKFLOW_EVENT,
            stage=WorkflowStage.GENERATE_SPEC,
            payload={
                "kind": "REPOSITORY_INDEX_PROGRESS",
                "message": "Preparing 1610 semantic chunks",
            },
        ),
    ])
    row = persisted_row(
        WorkflowBatchItem(0, "12", "First", execution.id), controller
    )
    assert "Review completed" in "\n".join(row.timeline)
    assert "Semantic index:" not in "\n".join(row.timeline)


def test_workflow_runner_batch_progress_renders_every_task_in_order(
    tmp_path: Path,
) -> None:
    controller, _, _ = _controller(tmp_path)
    application = OrchestratorApp(controller)
    batch = WorkflowBatch(
        id="batch-1",
        repository="example",
        task_source="github",
        items=(
            WorkflowBatchItem(0, "12", "First task"),
            WorkflowBatchItem(1, "7", "Second task"),
            WorkflowBatchItem(2, "3", "Third task"),
        ),
    )

    async def drive() -> None:
        async with application.run_test(size=(120, 50)) as pilot:
            application.push_screen(WorkflowRunnerScreen())
            await settle_tui(pilot)
            screen = application.screen
            screen._batch_running = True
            screen._batch_created(batch, _batch_launch("12", "7", "3"))
            await settle_tui(pilot)

            view = screen.query_one("#batch-progress", BatchProgressView)
            assert view.display is True
            assert screen.query_one("#workflow-outcome", Vertical).display is False
            assert str(screen.query_one("#stop-workflow", Button).label) == "Stop batch"
            headers = [
                str(screen.query_one(f"#batch-row-{position}-header", Button).label)
                for position in range(3)
            ]
            assert all("QUEUED" in header for header in headers)
            assert "12: First task" in headers[0]
            assert "7: Second task" in headers[1]
            assert "3: Third task" in headers[2]
            summary = str(screen.query_one("#batch-progress-summary", Static).renderable)
            assert "Queued: 3" in summary

    asyncio.run(drive())


def test_workflow_runner_batch_progress_updates_only_the_targeted_row_and_marks_provider_hold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller, _, _ = _controller(tmp_path)
    unavailable: set[str] = set()
    controller.batch_required_providers = lambda batch_id: frozenset({"planner"})  # type: ignore[method-assign]
    controller.batch_unavailable_providers = lambda batch_id: tuple(unavailable)  # type: ignore[method-assign]
    def mark_available(batch_id: str, provider: str) -> None:
        unavailable.discard(provider)
    controller.mark_batch_provider_available = mark_available  # type: ignore[method-assign]
    application = OrchestratorApp(controller)
    notifications: list[tuple[str, str | None]] = []

    def record_notification(
        _screen: WorkflowRunnerScreen, message: str, **kwargs: Any
    ) -> None:
        notifications.append((message, kwargs.get("severity")))

    monkeypatch.setattr(WorkflowRunnerScreen, "notify", record_notification)
    batch = WorkflowBatch(
        id="batch-1",
        repository="example",
        task_source="github",
        items=(
            WorkflowBatchItem(0, "12", "First task"),
            WorkflowBatchItem(1, "7", "Second task"),
            WorkflowBatchItem(2, "3", "Third task"),
        ),
    )

    async def drive() -> None:
        async with application.run_test(size=(120, 50)) as pilot:
            application.push_screen(WorkflowRunnerScreen())
            await settle_tui(pilot)
            screen = application.screen
            screen._batch_running = True
            screen._batch_created(batch, _batch_launch("12", "7", "3"))
            await settle_tui(pilot)

            screen._render_workflow_event(
                WorkflowEvent(
                    execution_id="execution-1",
                    kind=WorkflowEventKind.EXECUTION_CREATED,
                    payload={"batch_position": 0, "batch_size": 3},
                )
            )
            screen._render_workflow_event(
                WorkflowEvent(
                    execution_id="execution-1",
                    kind=WorkflowEventKind.STATUS_CHANGED,
                    status=WorkflowStatus.WAITING,
                    payload={"batch_position": 0, "batch_size": 3, "provider": "planner"},
                )
            )
            await settle_tui(pilot)

            assert "WAITING FOR AGENT (planner)" in str(
                screen.query_one("#batch-row-0-header", Button).label
            )
            assert "QUEUED" in str(
                screen.query_one("#batch-row-1-header", Button).label
            )
            assert "QUEUED" in str(
                screen.query_one("#batch-row-2-header", Button).label
            )
            summary = str(screen.query_one("#batch-progress-summary", Static).renderable)
            assert "Waiting for provider: 1" in summary
            assert "Queued: 2" in summary

            unavailable.add("planner")
            screen._render_workflow_event(
                WorkflowEvent(
                    execution_id="execution-1",
                    kind=WorkflowEventKind.DIAGNOSTIC,
                    payload={
                        "batch_position": 0,
                        "kind": "PROVIDER_QUOTA_EXHAUSTED",
                        "provider": "planner",
                    },
                )
            )
            await settle_tui(pilot)
            assert "planner unavailable (quota)" in str(
                screen.query_one("#batch-row-1-header", Button).label
            )
            assert "2 queued task(s) held" in str(
                screen.query_one("#batch-provider-availability", Vertical)
                .query_one(Static)
                .renderable
            )
            assert [severity for _, severity in notifications] == ["warning"]
            assert "Provider planner quota exhausted" in notifications[0][0]
            available = screen.query_one("#batch-provider-available-0", Button)

            screen._render_workflow_event(
                WorkflowEvent(
                    execution_id="execution-1",
                    kind=WorkflowEventKind.STATUS_CHANGED,
                    status=WorkflowStatus.RUNNING,
                    payload={"batch_position": 0, "batch_size": 3},
                )
            )
            await settle_tui(pilot)
            assert "RUNNING" in str(screen.query_one("#batch-row-0-header", Button).label)
            # Later events keep the same control mounted (focus and clicks
            # survive) and do not repeat the notification.
            assert screen.query_one("#batch-provider-available-0", Button) is available
            assert len(notifications) == 1
            # The batch progress sits below the launch form; reach it as a
            # user would, by scrolling it into view.
            available.scroll_visible(animate=False)
            await settle_tui(pilot)
            await pilot.click("#batch-provider-available-0")
            await settle_tui(pilot)
            assert not unavailable
            assert "unavailable" not in str(screen.query_one("#batch-row-1-header", Button).label)
            summary = str(screen.query_one("#batch-progress-summary", Static).renderable)
            assert "Running: 1" in summary
            assert "Queued: 2" in summary

    asyncio.run(drive())


def test_workflow_runner_batch_row_expands_and_reveals_detailed_logs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller, _, _ = _controller(tmp_path)
    application = OrchestratorApp(controller)
    copied: list[str] = []
    monkeypatch.setattr(application, "copy_to_clipboard", copied.append)
    batch = WorkflowBatch(
        id="batch-1",
        repository="example",
        task_source="github",
        items=(WorkflowBatchItem(0, "12", "First task"),),
    )

    async def drive() -> None:
        async with application.run_test(size=(120, 50)) as pilot:
            application.push_screen(WorkflowRunnerScreen())
            await settle_tui(pilot)
            screen = application.screen
            screen._batch_running = True
            screen._batch_created(batch, _batch_launch("12"))
            await settle_tui(pilot)
            screen._render_workflow_event(
                WorkflowEvent(
                    execution_id="execution-1",
                    kind=WorkflowEventKind.EXECUTION_CREATED,
                    payload={"batch_position": 0, "batch_size": 1},
                )
            )
            screen._render_workflow_event(
                WorkflowEvent(
                    execution_id="execution-1",
                    kind=WorkflowEventKind.DIAGNOSTIC,
                    category=DiagnosticCategory.REQUEST.value,
                    payload={
                        "batch_position": 0,
                        "effective_instructions": "implement the feature",
                        "authorization": "Bearer secret-token",
                    },
                )
            )
            await settle_tui(pilot)

            row = screen.query_one("#batch-row-0", BatchItemRow)
            assert screen.query_one("#batch-row-0-body", Vertical).display is False
            row.query_one("#batch-row-0-header", Button).press()
            await settle_tui(pilot)
            assert screen.query_one("#batch-row-0-body", Vertical).display is True

            toggle = screen.query_one("#batch-row-0-toggle-logs", Button)
            assert not toggle.disabled
            toggle.press()
            await settle_tui(pilot)
            detailed = screen.query_one("#batch-row-0-detailed", Static)
            assert detailed.display is True
            rendered = str(detailed.renderable)
            assert "implement the feature" in rendered
            assert "secret-token" not in rendered

            screen.query_one("#batch-row-0-copy-logs", Button).press()
            await settle_tui(pilot)

    asyncio.run(drive())

    assert len(copied) == 1
    assert "implement the feature" in copied[0]
    assert "secret-token" not in copied[0]


@pytest.mark.parametrize("persisted", [False, True], ids=["live", "persisted"])
def test_batch_rows_fit_content_and_last_row_remains_scrollable(
    tmp_path: Path, persisted: bool
) -> None:
    controller, _, repository = _controller(tmp_path)
    application = OrchestratorApp(controller)
    items = tuple(
        WorkflowBatchItem(position, str(position + 1), f"Task {position + 1}")
        for position in range(4)
    )
    if persisted:
        batch = repository.create_batch(
            "example",
            "github",
            [
                DevelopmentTask(item.task_id, "github", item.task_title, "", "")
                for item in items
            ],
        )
    else:
        batch = WorkflowBatch(
            id="batch-1", repository="example", task_source="github", items=items
        )

    async def drive() -> None:
        async with application.run_test(size=(80, 18)) as pilot:
            if persisted:
                application.push_screen(BatchDetailScreen(batch.id))
            else:
                application.push_screen(WorkflowRunnerScreen())
            await settle_tui(pilot)
            screen = application.screen
            view = screen.query_one("#batch-progress", BatchProgressView)
            if not persisted:
                screen._batch_running = True
                screen._set_single_task_controls_visible(False)
                screen._batch_created(batch, _batch_launch("1", "2", "3", "4"))
                await settle_tui(pilot)
            view.set_rows([
                BatchRowView(
                    position=position,
                    task_id=item.task_id,
                    task_title=item.task_title,
                    execution_id=f"execution-{position}",
                    category="RUNNING",
                    status_label="RUNNING",
                    stage_label="",
                    timeline=tuple(f"Timeline line {line}" for line in range(11))
                    if position == 0 else ("One line",),
                    action="answer",
                    can_cancel=True,
                )
                for position, item in enumerate(items)
            ])
            await settle_tui(pilot)
            scroll = screen.query_one(
                "#batch-detail" if persisted else "#workflow-runner", VerticalScroll
            )
            first = view.row(0)
            last = view.row(3)
            assert first is not None and last is not None
            compact_height = first.region.height

            first.query_one("#batch-row-0-header", Button).press()
            await settle_tui(pilot)
            expanded_height = first.region.height
            actions = first.query_one(".batch-item-actions", Horizontal)
            action = first.query_one("#batch-row-0-action", Button)
            assert actions.region.height == action.region.height
            assert first.region.bottom - actions.region.bottom <= 2
            assert expanded_height > compact_height
            expanded_scroll_extent = scroll.max_scroll_y

            scroll.scroll_end(animate=False)
            await settle_tui(pilot)
            assert screen.can_view_entire(last)

            first.query_one("#batch-row-0-toggle-logs", Button).press()
            await settle_tui(pilot)
            first.set_detailed_logs("\n".join(f"Detailed line {line}" for line in range(25)))
            await settle_tui(pilot)
            detailed = first.query_one("#batch-row-0-detailed", Static)
            assert first.region.bottom - detailed.region.bottom <= 2
            assert first.region.height > expanded_height
            assert scroll.max_scroll_y > expanded_scroll_extent

            second = view.row(1)
            assert second is not None
            second.query_one("#batch-row-1-header", Button).press()
            await settle_tui(pilot)
            scroll.scroll_end(animate=False)
            await settle_tui(pilot)
            assert screen.can_view_entire(last)

            first.query_one("#batch-row-0-toggle-logs", Button).press()
            second.query_one("#batch-row-1-header", Button).press()
            await settle_tui(pilot)
            assert first.region.height == expanded_height
            first.query_one("#batch-row-0-header", Button).press()
            await settle_tui(pilot)
            assert first.region.height == compact_height
            assert scroll.max_scroll_y < expanded_scroll_extent
            scroll.scroll_end(animate=False)
            await settle_tui(pilot)
            assert screen.can_view_entire(last)

    asyncio.run(drive())


def test_workflow_runner_batch_finished_keeps_final_rows_and_summary_visible(
    tmp_path: Path,
) -> None:
    controller, _, repository = _controller(tmp_path)
    approved = WorkflowExecution(id="execution-1", status=WorkflowStatus.APPROVED)
    failed = WorkflowExecution(id="execution-2", status=WorkflowStatus.FAILED)
    repository.executions = [repository.execution, approved, failed]
    application = OrchestratorApp(controller)
    batch = WorkflowBatch(
        id="batch-1",
        repository="example",
        task_source="github",
        items=(
            WorkflowBatchItem(0, "12", "First task", "execution-1"),
            WorkflowBatchItem(1, "7", "Second task", "execution-2"),
        ),
    )

    async def drive() -> None:
        async with application.run_test(size=(120, 50)) as pilot:
            application.push_screen(WorkflowRunnerScreen())
            await settle_tui(pilot)
            screen = application.screen
            screen._batch_running = True
            screen._batch_created(batch, _batch_launch("12", "7"))
            await settle_tui(pilot)

            screen._batch_finished(batch)
            await settle_tui(pilot)

            assert screen.query_one("#batch-progress", BatchProgressView).display is True
            assert "APPROVED" in str(
                screen.query_one("#batch-row-0-header", Button).label
            )
            assert "FAILED" in str(screen.query_one("#batch-row-1-header", Button).label)
            summary = str(screen.query_one("#batch-progress-summary", Static).renderable)
            assert "Approved: 1" in summary
            assert "Failed: 1" in summary
            assert "Batch finished" in str(
                screen.query_one("#workflow-progress", Static).renderable
            )

    asyncio.run(drive())


def test_batch_detail_screen_renders_mixed_batch_and_supports_refresh(
    tmp_path: Path,
) -> None:
    controller, _, repository = _controller(tmp_path)
    running = WorkflowExecution(id="execution-2", status=WorkflowStatus.RUNNING)
    repository.executions = [repository.execution, running]
    batch = repository.create_batch(
        "example",
        "github",
        [
            DevelopmentTask("12", "github", "First task", "", "https://example/12"),
            DevelopmentTask("7", "github", "Second task", "", "https://example/7"),
        ],
    )
    repository.record_batch_item_execution(batch.id, 0, repository.execution.id)
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test(size=(120, 50)) as pilot:
            application.push_screen(BatchDetailScreen(batch.id))
            await settle_tui(pilot)
            screen = application.screen
            assert "APPROVED" in str(
                screen.query_one("#batch-row-0-header", Button).label
            )
            assert "QUEUED" in str(screen.query_one("#batch-row-1-header", Button).label)
            summary = str(screen.query_one("#batch-progress-summary", Static).renderable)
            assert "Approved: 1" in summary
            assert "Queued: 1" in summary

            # A later submission links the second item; refreshing should pick
            # it up without losing the first item's already-shown detail.
            repository.record_batch_item_execution(batch.id, 1, running.id)
            screen.query_one("#refresh-batch", Button).press()
            await settle_tui(pilot)
            assert "RUNNING" in str(screen.query_one("#batch-row-1-header", Button).label)

    asyncio.run(drive())


@pytest.mark.parametrize("action", ["continue", "answer"])
def test_closed_batch_detail_reconciles_after_execution_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    controller, service, repository = _controller(tmp_path)
    execution = repository.execution
    execution.status = (
        WorkflowStatus.CHANGES_REQUESTED if action == "continue" else WorkflowStatus.NEEDS_HUMAN
    )
    if action == "continue":
        execution.workspace_path = str(tmp_path / "worktree")
        execution.working_branch = "workflow/12"
    else:
        execution.current_stage = WorkflowStage.REVIEW
        execution.human_interactions = [HumanInteraction(
            id=1, workflow_execution_id=execution.id,
            requesting_stage=WorkflowStage.REVIEW, prompt="Clarify the change.",
            requested_at=datetime.now(UTC),
        )]
    other = WorkflowExecution(id="execution-2", status=WorkflowStatus.APPROVED)
    repository.executions.append(other)
    batch = repository.create_batch("example", "github", [
        DevelopmentTask("12", "github", "First task", "", "https://example/12"),
        DevelopmentTask("7", "github", "Second task", "", "https://example/7"),
    ])
    repository.record_batch_item_execution(batch.id, 0, execution.id)
    repository.record_batch_item_execution(batch.id, 1, other.id)

    def submit(execution_id: str, value: str, *, event_listener: Any = None) -> _Handle:
        assert execution_id == execution.id and value == "Updated input"
        assert not controller.execution_in_live_batch(execution_id)
        execution.status = WorkflowStatus.RUNNING
        execution.current_stage = WorkflowStage.IMPLEMENT
        return _Handle(WorkflowExecution(id=execution_id, status=WorkflowStatus.APPROVED))

    monkeypatch.setattr(
        service, "submit_continuation" if action == "continue" else "submit_response", submit
    )
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test(size=(120, 50)) as pilot:
            application.push_screen(BatchDetailScreen(batch.id))
            await settle_tui(pilot)
            screen = application.screen
            assert isinstance(screen, BatchDetailScreen)
            assert not controller.execution_in_live_batch(execution.id)
            screen.query_one("#batch-row-0-header", Button).press()
            await settle_tui(pilot)
            unchanged = screen.query_one("#batch-row-1", BatchItemRow)
            screen.query_one("#batch-row-0-action", Button).press()
            await settle_tui(pilot)
            detail = application.screen
            assert isinstance(detail, ExecutionDetailScreen)
            def hidden_read(_: str) -> WorkflowExecution:
                raise AssertionError("hidden batch screen must not read executions")

            with monkeypatch.context() as hidden_patch:
                hidden_patch.setattr(controller, "execution_snapshot", hidden_read)
                screen._reconcile_rows()
            if action == "continue":
                detail.query_one(
                    "#execution-continuation-feedback", TextArea
                ).text = "Updated input"
                button = detail.query_one("#execution-continue", Button)
            else:
                detail.query_one("#execution-human-response", TextArea).text = "Updated input"
                button = detail.query_one("#execution-resume", Button)
            await settle_tui(pilot)
            button.press()
            await wait_for_tui(
                pilot,
                lambda: str(detail.query_one("#execution-status", Static).renderable) == "RUNNING",
            )
            await pilot.press("escape")
            await settle_tui(pilot)
            assert application.screen is screen
            assert "RUNNING" in str(screen.query_one("#batch-row-0-header", Button).label)
            assert "Implement" in str(screen.query_one("#batch-row-0-header", Button).label)
            assert not screen.query_one("#batch-row-0-action", Button).display
            assert screen.query_one("#batch-row-1", BatchItemRow) is unchanged
            assert "APPROVED" in str(screen.query_one("#batch-row-1-header", Button).label)
            assert "Running: 1" in str(
                screen.query_one("#batch-progress-summary", Static).renderable
            )
            assert "Approved: 1" in str(
                screen.query_one("#batch-progress-summary", Static).renderable
            )
            assert not controller.execution_in_live_batch(execution.id)

    asyncio.run(drive())


def test_closed_batch_detail_reconciles_visible_row_without_remounting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller, service, repository = _controller(tmp_path)
    repository.execution.status = WorkflowStatus.CHANGES_REQUESTED
    repository.execution.workspace_path = str(tmp_path / "worktree")
    repository.execution.working_branch = "workflow/12"
    other = WorkflowExecution(id="execution-2", status=WorkflowStatus.FAILED)
    repository.executions.append(other)
    batch = repository.create_batch("example", "github", [
        DevelopmentTask("12", "github", "First task", "", "https://example/12"),
        DevelopmentTask("7", "github", "Second task", "", "https://example/7"),
    ])
    repository.record_batch_item_execution(batch.id, 0, repository.execution.id)
    repository.record_batch_item_execution(batch.id, 1, other.id)

    def submit(execution_id: str, feedback: str, *, event_listener: Any = None) -> _Handle:
        assert execution_id == repository.execution.id and feedback == "Continue"
        assert not controller.execution_in_live_batch(execution_id)
        repository.execution.status = WorkflowStatus.RUNNING
        repository.execution.current_stage = WorkflowStage.IMPLEMENT
        return _Handle(repository.execution)

    monkeypatch.setattr(service, "submit_continuation", submit)
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test(size=(120, 50)) as pilot:
            application.push_screen(BatchDetailScreen(batch.id))
            await settle_tui(pilot)
            screen = application.screen
            assert isinstance(screen, BatchDetailScreen)
            screen.query_one("#batch-row-0-header", Button).press()
            screen.query_one("#batch-row-1-header", Button).press()
            await settle_tui(pilot)
            changed = screen.query_one("#batch-row-0", BatchItemRow)
            unchanged = screen.query_one("#batch-row-1", BatchItemRow)
            unchanged.query_one("#batch-row-1-header", Button).focus()
            scroll = screen.query_one("#batch-detail", VerticalScroll)
            await settle_tui(pilot)
            # Focus may start a scroll animation; finish it before measuring.
            await scroll.stop_animation("scroll_y")
            scroll.scroll_to(y=0, animate=False, immediate=True)
            await settle_tui(pilot)
            old_scroll_y = scroll.scroll_y
            screen._diagnostics_cache[0] = []
            controller.submit_continuation(repository.execution.id, "Continue")
            assert screen._reconcile_timer is not None
            screen._reconcile_rows()  # The foreground timer's callback, without clock sleeps.
            await settle_tui(pilot)
            assert screen.query_one("#batch-row-0", BatchItemRow) is changed
            assert screen.query_one("#batch-row-1", BatchItemRow) is unchanged
            assert changed._expanded and unchanged._expanded
            assert application.focused is unchanged.query_one("#batch-row-1-header", Button)
            assert scroll.scroll_y == old_scroll_y
            assert 0 not in screen._diagnostics_cache
            assert "RUNNING" in str(screen.query_one("#batch-row-0-header", Button).label)
            assert "FAILED" in str(screen.query_one("#batch-row-1-header", Button).label)
            assert "Running: 1" in str(
                screen.query_one("#batch-progress-summary", Static).renderable
            )
            assert not controller.execution_in_live_batch(repository.execution.id)

            original_detail = controller.execution_detail

            def fail_detail(execution_id: str) -> Any:
                if execution_id == repository.execution.id:
                    raise RuntimeError("temporary read failure")
                return original_detail(execution_id)

            repository.execution.status = WorkflowStatus.APPROVED
            monkeypatch.setattr(controller, "execution_detail", fail_detail)
            screen._reconcile_rows()
            assert "RUNNING" in str(screen.query_one("#batch-row-0-header", Button).label)
            assert "FAILED" in str(screen.query_one("#batch-row-1-header", Button).label)
            screen.query_one("#refresh-batch", Button).press()
            await settle_tui(pilot)
            assert "RUNNING" in str(screen.query_one("#batch-row-0-header", Button).label)
            monkeypatch.setattr(controller, "execution_detail", original_detail)
            screen.query_one("#refresh-batch", Button).press()
            await settle_tui(pilot)
            assert "APPROVED" in str(screen.query_one("#batch-row-0-header", Button).label)

    asyncio.run(drive())


@pytest.mark.parametrize("legacy_cancel_marker", [False, True])
def test_batch_detail_cancel_reconciles_human_blocked_row(
    tmp_path: Path, legacy_cancel_marker: bool
) -> None:
    controller, _, repository = _controller(tmp_path)
    repository.execution.status = WorkflowStatus.NEEDS_HUMAN
    if legacy_cancel_marker:
        repository.execution.cancellation_requested_at = datetime.now(UTC)
    batch = repository.create_batch(
        "example", "github",
        [DevelopmentTask("12", "github", "First task", "", "https://example/12")],
    )
    repository.record_batch_item_execution(batch.id, 0, repository.execution.id)
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test(size=(120, 50)) as pilot:
            application.push_screen(BatchDetailScreen(batch.id))
            await settle_tui(pilot)
            screen = application.screen
            screen.query_one("#batch-row-0-header", Button).press()
            await settle_tui(pilot)
            action = screen.query_one("#batch-row-0-action", Button)
            assert action.display is not legacy_cancel_marker
            cancel = screen.query_one("#batch-row-0-cancel", Button)
            assert cancel.display and not cancel.disabled
            cancel.press()
            await settle_tui(pilot)
            assert repository.cancellation_requests == [repository.execution.id]
            assert "CANCELLED" in str(screen.query_one("#batch-row-0-header", Button).label)
            assert "Cancelled: 1" in str(
                screen.query_one("#batch-progress-summary", Static).renderable
            )

    asyncio.run(drive())


def test_batch_detail_refreshes_after_cancellation_from_execution_detail(tmp_path: Path) -> None:
    controller, _, repository = _controller(tmp_path)
    repository.execution.status = WorkflowStatus.NEEDS_HUMAN
    batch = repository.create_batch(
        "example", "github",
        [DevelopmentTask("12", "github", "First task", "", "https://example/12")],
    )
    repository.record_batch_item_execution(batch.id, 0, repository.execution.id)
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test(size=(120, 50)) as pilot:
            application.push_screen(BatchDetailScreen(batch.id))
            await settle_tui(pilot)
            batch_screen = application.screen
            assert "Needs human: 1" in str(
                batch_screen.query_one("#batch-progress-summary", Static).renderable
            )
            batch_screen.query_one("#batch-row-0-header", Button).press()
            await settle_tui(pilot)
            batch_screen.query_one("#batch-row-0-open-execution", Button).press()
            await settle_tui(pilot)
            detail = application.screen
            assert isinstance(detail, ExecutionDetailScreen)
            detail.query_one("#execution-cancel", Button).press()
            await settle_tui(pilot)
            assert isinstance(application.screen, StopWorkflowConfirmation)
            application.screen.query_one("#confirm-stop-workflow", Button).press()
            await settle_tui(pilot)
            assert str(detail.query_one("#execution-status", Static).renderable) == "CANCELLED"
            await pilot.press("escape")
            await settle_tui(pilot)
            assert application.screen is batch_screen
            assert "CANCELLED" in str(
                batch_screen.query_one("#batch-row-0-header", Button).label
            )
            assert "Cancelled: 1" in str(
                batch_screen.query_one("#batch-progress-summary", Static).renderable
            )

    asyncio.run(drive())


def test_detail_cancel_refreshes_still_running_batch_on_return(tmp_path: Path) -> None:
    controller, _, repository = _controller(tmp_path)
    repository.execution.status = WorkflowStatus.NEEDS_HUMAN
    other = WorkflowExecution(id="execution-2", status=WorkflowStatus.RUNNING)
    repository.executions.append(other)
    batch = repository.create_batch(
        "example", "github",
        [
            DevelopmentTask("12", "github", "First task", "", "https://example/12"),
            DevelopmentTask("7", "github", "Second task", "", "https://example/7"),
        ],
    )
    repository.record_batch_item_execution(batch.id, 0, repository.execution.id)
    repository.record_batch_item_execution(batch.id, 1, other.id)
    batch = repository.get_batch(batch.id)
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test(size=(120, 50)) as pilot:
            application.push_screen(WorkflowRunnerScreen())
            await settle_tui(pilot)
            runner = application.screen
            assert isinstance(runner, WorkflowRunnerScreen)
            runner._batch_running = True
            runner._batch_created(batch, _batch_launch("12", "7"))
            for position, status in enumerate((WorkflowStatus.NEEDS_HUMAN, WorkflowStatus.RUNNING)):
                runner._render_workflow_event(WorkflowEvent(
                    execution_id=f"execution-{position + 1}",
                    kind=WorkflowEventKind.STATUS_CHANGED,
                    status=status,
                    payload={"batch_position": position},
                ))
            await settle_tui(pilot)
            assert "Needs human: 1" in str(
                runner.query_one("#batch-progress-summary", Static).renderable
            )

            application.push_screen(BatchDetailScreen(batch.id))
            await settle_tui(pilot)
            detail = application.screen
            assert isinstance(detail, BatchDetailScreen)
            detail.query_one("#batch-row-0-header", Button).press()
            await settle_tui(pilot)
            detail.query_one("#batch-row-0-cancel", Button).press()
            await settle_tui(pilot)
            assert repository.cancellation_requests == ["execution-1"]
            assert other.status is WorkflowStatus.RUNNING

            await pilot.press("escape")
            await settle_tui(pilot)
            assert application.screen is runner
            assert runner._batch_running
            assert "CANCELLED" in str(runner.query_one("#batch-row-0-header", Button).label)
            assert not runner.query_one("#batch-row-0-cancel", Button).display
            assert "RUNNING" in str(runner.query_one("#batch-row-1-header", Button).label)
            assert runner.query_one("#batch-row-1-cancel", Button).display
            summary = str(runner.query_one("#batch-progress-summary", Static).renderable)
            assert "Cancelled: 1" in summary
            assert "Needs human: 0" in summary
            assert "Running: 1" in summary

            runner._render_workflow_event(WorkflowEvent(
                execution_id="execution-2", kind=WorkflowEventKind.STATUS_CHANGED,
                status=WorkflowStatus.WAITING, payload={"batch_position": 1},
            ))
            await settle_tui(pilot)
            assert "CANCELLED" in str(runner.query_one("#batch-row-0-header", Button).label)
            assert "Cancelled: 1" in str(
                runner.query_one("#batch-progress-summary", Static).renderable
            )

    asyncio.run(drive())


def test_execution_detail_hides_answer_for_legacy_human_cancellation(tmp_path: Path) -> None:
    controller, _, repository = _controller(tmp_path)
    repository.execution.status = WorkflowStatus.NEEDS_HUMAN
    repository.execution.cancellation_requested_at = datetime.now(UTC)
    repository.execution.human_interactions = [HumanInteraction(
        id=1,
        workflow_execution_id=repository.execution.id,
        requesting_stage=WorkflowStage.REVIEW,
        prompt="Review finding?",
        requested_at=datetime.now(UTC),
    )]
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test() as pilot:
            application.push_screen(ExecutionDetailScreen(repository.execution.id))
            await settle_tui(pilot)
            screen = application.screen
            assert "CANCELLATION REQUESTED" in str(
                screen.query_one("#execution-status", Static).renderable
            )
            assert not screen.query_one("#execution-human-actions").display

    asyncio.run(drive())


def test_batch_item_row_cancel_emits_distinct_request_beside_answer() -> None:
    class RowApp(App[None]):
        def __init__(self) -> None:
            super().__init__()
            self.cancellations: list[int] = []
            self.actions: list[int] = []

        def compose(self) -> ComposeResult:
            yield BatchItemRow(BatchRowView(
                position=0, task_id="12", task_title="First task",
                execution_id="execution-1", category="NEEDS_HUMAN",
                status_label="AWAITING HUMAN", stage_label="Review",
                action="answer", can_cancel=True,
            ))

        def on_batch_item_row_cancel_requested(
            self, message: BatchItemRow.CancelRequested
        ) -> None:
            self.cancellations.append(message.position)

        def on_batch_item_row_action_requested(
            self, message: BatchItemRow.ActionRequested
        ) -> None:
            self.actions.append(message.position)

    application = RowApp()

    async def drive() -> None:
        async with application.run_test(size=(120, 30)) as pilot:
            await settle_tui(pilot)
            application.query_one("#batch-row-0-header", Button).press()
            await settle_tui(pilot)
            assert application.query_one("#batch-row-0-action", Button).display
            application.query_one("#batch-row-0-cancel", Button).press()
            await settle_tui(pilot)
            assert application.cancellations == [0]
            assert application.actions == []

    asyncio.run(drive())


def test_live_batch_cancel_updates_only_selected_row(tmp_path: Path) -> None:
    controller, _, repository = _controller(tmp_path)
    repository.execution.status = WorkflowStatus.NEEDS_HUMAN
    other = WorkflowExecution(id="execution-2", status=WorkflowStatus.WAITING)
    repository.executions.append(other)
    application = OrchestratorApp(controller)
    batch = WorkflowBatch(
        id="batch-1", repository="example", task_source="github",
        items=(
            WorkflowBatchItem(0, "12", "First task", repository.execution.id),
            WorkflowBatchItem(1, "7", "Second task", other.id),
        ),
    )

    async def drive() -> None:
        async with application.run_test(size=(120, 50)) as pilot:
            application.push_screen(WorkflowRunnerScreen())
            await settle_tui(pilot)
            screen = application.screen
            screen._batch_running = True
            screen._batch_created(batch, _batch_launch("12", "7"))
            await settle_tui(pilot)
            screen._render_workflow_event(WorkflowEvent(
                execution_id=repository.execution.id,
                kind=WorkflowEventKind.STATUS_CHANGED,
                status=WorkflowStatus.NEEDS_HUMAN,
                payload={"batch_position": 0},
            ))
            screen._render_workflow_event(WorkflowEvent(
                execution_id=other.id,
                kind=WorkflowEventKind.STATUS_CHANGED,
                status=WorkflowStatus.WAITING,
                payload={"batch_position": 1},
            ))
            screen.query_one("#batch-row-0-header", Button).press()
            await settle_tui(pilot)
            assert screen.query_one("#batch-row-0-action", Button).display
            screen.query_one("#batch-row-0-cancel", Button).press()
            await settle_tui(pilot)
            assert repository.cancellation_requests == [repository.execution.id]
            assert "CANCELLED" in str(screen.query_one("#batch-row-0-header", Button).label)
            assert "WAITING" in str(screen.query_one("#batch-row-1-header", Button).label)
            assert other.status is WorkflowStatus.WAITING
            summary = str(screen.query_one("#batch-progress-summary", Static).renderable)
            assert "Cancelled: 1" in summary and "Waiting for provider: 1" in summary

    asyncio.run(drive())


def test_batch_detail_screen_reports_batch_read_failure_without_crashing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller, _, repository = _controller(tmp_path)

    def fail_get_batch(batch_id: str) -> WorkflowBatch:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(repository, "get_batch", fail_get_batch)
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test(size=(120, 50)) as pilot:
            application.push_screen(BatchDetailScreen("missing-batch"))
            await settle_tui(pilot)
            screen = application.screen
            assert "Could not load batch" in str(
                screen.query_one("#batch-summary", Static).renderable
            )

    asyncio.run(drive())


def test_batch_detail_screen_reports_empty_batch() -> None:
    class _EmptyRepository:
        def get_batch(self, batch_id: str) -> WorkflowBatch:
            return WorkflowBatch(id=batch_id, repository="example", task_source="github")

    controller = WorkflowController(
        workflow_repository=_EmptyRepository(),  # type: ignore[arg-type]
        config_loader=object(),  # type: ignore[arg-type]
        client_factory=object(),  # type: ignore[arg-type]
        task_provider_factory=lambda source, repository: _TaskProvider(),
    )
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test(size=(120, 50)) as pilot:
            application.push_screen(BatchDetailScreen("batch-1"))
            await settle_tui(pilot)
            screen = application.screen
            note = str(screen.query_one("#batch-progress-note", Static).renderable)
            assert "no tasks" in note

    asyncio.run(drive())


_LONG_TASK_TITLE = (
    "Refactor the workflow batch dispatcher so partially failed batches resume from "
    "the first unfinished task instead of restarting the whole run"
)
_MANY_LABELS = ["bug", "enhancement", "needs-triage", "backend", "priority:high"]


def _backlog_tasks() -> list[DevelopmentTask]:
    """A realistic backlog: many rows, one overlong title, one blocked task."""

    tasks = [
        DevelopmentTask(
            "101",
            "github",
            _LONG_TASK_TITLE,
            "",
            "https://example/101",
            metadata={"labels": _MANY_LABELS},
        ),
        DevelopmentTask(
            "7",
            "github",
            "Busy task",
            "",
            "https://example/7",
            metadata={"labels": ["active"]},
        ),
        DevelopmentTask("1042", "github", "", "", "https://example/1042", metadata={}),
    ]
    tasks.extend(
        DevelopmentTask(
            str(number),
            "github",
            f"Task number {number} with an ordinary title",
            "",
            f"https://example/{number}",
            metadata={"labels": ["bug"] if number % 2 else ["enhancement", "ui"]},
        )
        for number in range(200, 230)
    )
    return tasks


def _backlog_controller(
    tmp_path: Path,
) -> tuple[WorkflowController, _WorkflowService, _Repository]:
    controller, service, repository = _controller(tmp_path)
    repository.execution.task_source = "github"
    repository.execution.task_id = "7"
    repository.execution.repository_ref = "example"
    repository.execution.status = WorkflowStatus.RUNNING
    tasks = _backlog_tasks()
    provider = _TaskProvider()
    provider.list_tasks = lambda limit=50: tasks  # type: ignore[method-assign]
    controller._task_provider_factory = lambda source, selected_repository: provider
    return controller, service, repository


def _row_text(row: TaskRow) -> str:
    rendered = row.render()
    assert isinstance(rendered, Text)
    return rendered.plain


def test_task_selector_renders_rows_as_an_aligned_truncated_table(tmp_path: Path) -> None:
    controller, _, _ = _backlog_controller(tmp_path)
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test(size=(100, 30)) as pilot:
            await pilot.press("n")
            await wait_for_tui(
                pilot,
                lambda: len(application.screen.query(TaskRow)) == 33,
                description="backlog task rows",
            )
            await settle_tui(pilot)
            screen = application.screen
            header = screen.query_one("#task-table-header", TaskTableHeader)
            header_text = header.render()
            assert isinstance(header_text, Text)
            title_column = header_text.plain.index("Title")
            labels_column = header_text.plain.index("Labels")

            rows = list(screen.query(TaskRow))
            for row in rows:
                text = _row_text(row)
                # Every row is clipped to the row width, so nothing spills off
                # the side of the terminal.
                assert len(text) <= row.content_size.width
                # Columns line up with the heading positions.
                assert text[:title_column].rstrip().endswith(row._number)
                assert text[title_column - 1] == " "
                assert text[labels_column - 1] == " "

            long_row = screen.query_one("#task-checkbox-0", TaskRow)
            long_text = _row_text(long_row)
            assert _LONG_TASK_TITLE not in long_text
            assert "Refactor the workflow batch" in long_text
            assert "…" in long_text
            assert "bug, enhancement" in long_text
            assert ", ".join(_MANY_LABELS) not in long_text

            untitled_row = screen.query_one("#task-checkbox-2", TaskRow)
            assert "(untitled)" in _row_text(untitled_row)

            # A long backlog stays within the terminal width.
            content = screen.query_one("#workflow-runner", VerticalScroll)
            assert content.max_scroll_x == 0

    asyncio.run(drive())


def test_task_selector_rows_show_checked_unchecked_and_blocked_glyphs(
    tmp_path: Path,
) -> None:
    controller, _, _ = _backlog_controller(tmp_path)
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test(size=(100, 30)) as pilot:
            await pilot.press("n")
            await wait_for_tui(
                pilot,
                lambda: len(application.screen.query(TaskRow)) == 33,
                description="backlog task rows",
            )
            await settle_tui(pilot)
            screen = application.screen
            selectable = screen.query_one("#task-checkbox-0", TaskRow)
            blocked = screen.query_one("#task-checkbox-1", TaskRow)

            assert _row_text(selectable).startswith("[ ]")
            assert _row_text(blocked).startswith("[-]")

            selectable.value = True
            await settle_tui(pilot)
            assert _row_text(selectable).startswith("[x]")

            selectable.value = False
            await settle_tui(pilot)
            assert _row_text(selectable).startswith("[ ]")

    asyncio.run(drive())


def test_task_checkbox_mouse_click_updates_selection_and_screen_on_first_click(
    tmp_path: Path,
) -> None:
    controller, _, _ = _controller(tmp_path)
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test(size=(100, 60)) as pilot:
            await pilot.press("n")
            await wait_for_tui(
                pilot,
                lambda: len(application.screen.query(TaskRow)) == 1,
                description="task checkbox row",
            )
            screen = application.screen
            selector = screen.query_one("#task-selector", TaskSelector)
            row = screen.query_one("#task-checkbox-0", TaskRow)
            content = screen.query_one("#workflow-runner", VerticalScroll)

            def painted_glyph() -> str:
                # Read the composed terminal cells, not TaskRow.render().
                line = screen._compositor.render_strips()[row.region.y]
                return line.crop(row.region.x, row.region.x + 3).text

            await settle_tui(pilot)
            assert painted_glyph() == "[ ]"
            scroll_y = content.scroll_y

            assert await pilot.click(row, offset=(1, 0))
            await settle_tui(pilot)
            assert row.value is True
            assert selector.selected_task_ids == ("12",)
            assert "Selected 1 of 1 available github tasks." in str(
                screen.query_one("#task-hint", Static).renderable
            )
            assert painted_glyph() == "[x]"
            assert screen.query_one("#task-checkbox-0", TaskRow) is row
            assert row.has_focus
            assert content.scroll_y == scroll_y

            assert await pilot.click(row, offset=(1, 0))
            await settle_tui(pilot)
            assert row.value is False
            assert selector.selected_task_ids == ()
            assert "Selected 0 of 1 available github tasks." in str(
                screen.query_one("#task-hint", Static).renderable
            )
            assert painted_glyph() == "[ ]"
            assert screen.query_one("#task-checkbox-0", TaskRow) is row
            assert row.has_focus
            assert content.scroll_y == scroll_y

            await pilot.press("space")
            await settle_tui(pilot)
            assert selector.selected_task_ids == ("12",)
            assert painted_glyph() == "[x]"

            header = screen.query_one("#select-all-tasks", Checkbox)
            assert header.value is True
            assert await pilot.click(header, offset=(1, 0))
            await settle_tui(pilot)
            assert header.value is False
            assert selector.selected_task_ids == ()
            assert painted_glyph() == "[ ]"

            # Keyboard focus still reaches a row in a short viewport.
            await pilot.resize_terminal(100, 20)
            row.focus()
            await wait_for_tui(
                pilot,
                lambda: screen.can_view_partial(row),
                description="focused task row after terminal resize",
            )
            assert row.has_focus
            assert screen.can_view_partial(row)
            await pilot.press("space")
            await settle_tui(pilot)
            assert selector.selected_task_ids == ("12",)
            assert painted_glyph() == "[x]"

    asyncio.run(drive())


def test_later_task_checkbox_click_in_short_viewport_updates_on_first_click(
    tmp_path: Path,
) -> None:
    controller, _, _ = _backlog_controller(tmp_path)
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test(size=(80, 20)) as pilot:
            await pilot.press("n")
            await wait_for_tui(
                pilot,
                lambda: len(application.screen.query(TaskRow)) == 33,
                description="backlog task rows",
            )
            screen = application.screen
            selector = screen.query_one("#task-selector", TaskSelector)
            content = screen.query_one("#workflow-runner", VerticalScroll)
            row = screen.query_one("#task-checkbox-12", TaskRow)
            content.scroll_to_widget(row, animate=False, center=True)
            await settle_tui(pilot)
            assert content.scroll_y > 0
            assert screen.can_view_entire(row)
            scroll_y = content.scroll_y

            def painted_glyph() -> str:
                line = screen._compositor.render_strips()[row.region.y]
                return line.crop(row.region.x, row.region.x + 3).text

            assert painted_glyph() == "[ ]"
            assert await pilot.click(row, offset=(1, 0))
            await settle_tui(pilot)
            assert row.value is True
            assert selector.selected_task_ids == ("209",)
            assert painted_glyph() == "[x]"
            assert content.scroll_y == scroll_y

            assert await pilot.click(row, offset=(1, 0))
            await settle_tui(pilot)
            assert row.value is False
            assert selector.selected_task_ids == ()
            assert painted_glyph() == "[ ]"
            assert content.scroll_y == scroll_y

    asyncio.run(drive())


def test_task_selector_separates_select_all_from_the_task_rows(tmp_path: Path) -> None:
    controller, _, _ = _backlog_controller(tmp_path)
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test(size=(100, 30)) as pilot:
            await pilot.press("n")
            await wait_for_tui(
                pilot,
                lambda: len(application.screen.query(TaskRow)) == 33,
                description="backlog task rows",
            )
            await settle_tui(pilot)
            screen = application.screen
            select_all = screen.query_one("#select-all-tasks", Checkbox)

            # The select-all band sits outside the row list and is styled apart
            # from it.
            assert not isinstance(select_all, TaskRow)
            assert select_all.has_class("task-select-all")
            assert select_all not in list(
                screen.query_one("#available-tasks", Vertical).children
            )
            assert select_all.styles.text_style.bold is True
            assert select_all.styles.border_bottom[0] != ""

    asyncio.run(drive())


class _LabelledTaskProvider(_TaskProvider):
    """A task provider with a label catalog, as GitHub issues have."""

    def __init__(
        self,
        tasks: list[DevelopmentTask],
        labels: list[str],
        label_error: Exception | None = None,
    ) -> None:
        self.tasks = tasks
        self.labels = labels
        self.label_error = label_error

    def list_tasks(self, *, limit: int = 50) -> list[DevelopmentTask]:
        return list(self.tasks)

    def list_labels(self) -> list[str]:
        if self.label_error is not None:
            raise self.label_error
        return list(self.labels)


def _labelled_tasks() -> list[DevelopmentTask]:
    def task(task_id: str, title: str, labels: list[str]) -> DevelopmentTask:
        return DevelopmentTask(
            task_id, "github", title, "", f"https://example/{task_id}",
            metadata={"labels": labels},
        )

    return [
        task("1", "Fix crash", ["bug"]),
        task("2", "Retrieval bug", ["bug", "RAG"]),
        task("3", "Capitalised label", ["Bug"]),
        task("4", "Tester work", ["Tester Agent"]),
        task("7", "Busy", ["bug", "RAG"]),
    ]


def _labelled_controller(
    tmp_path: Path,
    *,
    labels: list[str] | None = None,
    label_error: Exception | None = None,
) -> tuple[WorkflowController, _WorkflowService, _LabelledTaskProvider]:
    controller, service, repository = _controller(tmp_path)
    repository.execution.task_source = "github"
    repository.execution.task_id = "7"
    repository.execution.repository_ref = "example"
    repository.execution.status = WorkflowStatus.RUNNING
    provider = _LabelledTaskProvider(
        _labelled_tasks(),
        ["bug", "Bug", "RAG", "Tester Agent"] if labels is None else labels,
        label_error,
    )
    # Trello has no label catalog, like the real provider.
    controller._task_provider_factory = lambda source, selected_repository: (
        provider if source == "github" else _TaskProvider()
    )
    return controller, service, provider


async def _wait_for_labelled_tasks(pilot: Any, application: OrchestratorApp) -> TaskSelector:
    await wait_for_tui(
        pilot,
        lambda: len(application.screen.query(TaskRow)) == 5
        and application.screen.query_one("#task-selector", TaskSelector).label_catalog
        is not None,
        description="labelled task rows",
    )
    return application.screen.query_one("#task-selector", TaskSelector)


def test_controller_distinguishes_unsupported_and_empty_label_catalogs(tmp_path: Path) -> None:
    controller, _, _ = _controller(tmp_path)
    assert controller.list_task_labels(repository="example", task_provider="github") is None

    labelled, _, provider = _labelled_controller(tmp_path, labels=[])
    assert labelled.list_task_labels(repository="example", task_provider="github") == ()
    provider.labels = ["bug", "RAG"]
    assert labelled.list_task_labels(repository="example", task_provider="github") == (
        "bug",
        "RAG",
    )
    assert labelled.list_task_labels(repository="example", task_provider="trello") is None


def test_task_label_filter_matches_exact_names_with_and_and_clears(tmp_path: Path) -> None:
    controller, _, _ = _labelled_controller(tmp_path)
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test(size=(100, 30)) as pilot:
            await pilot.press("n")
            selector = await _wait_for_labelled_tasks(pilot, application)
            screen = application.screen
            label_list = screen.query_one("#task-label-list", SelectionList)
            assert screen.query_one("#task-label-filter", Vertical).display is True
            assert selector.label_catalog == ("bug", "Bug", "RAG", "Tester Agent")

            # Keyboard toggling selects "bug", which must not match "Bug".
            label_list.focus()
            label_list.highlighted = 0
            await pilot.press("space")
            await wait_for_tui(
                pilot,
                lambda: selector.label_filter == ("bug",),
                description="single label filter",
            )
            assert selector.visible_task_ids == ("1", "2", "7")
            assert screen.query_one("#task-checkbox-2", TaskRow).display is False
            assert screen.query_one("#task-checkbox-0", TaskRow).display is True

            # Several labels must all be present on a shown task.
            label_list.select("RAG")
            await wait_for_tui(
                pilot,
                lambda: selector.label_filter == ("bug", "RAG"),
                description="two-label filter",
            )
            assert selector.visible_task_ids == ("2", "7")
            assert "Showing 2 of 5 loaded tasks labelled bug + RAG." in str(
                screen.query_one("#task-hint", Static).renderable
            )

            label_list.select("Tester Agent")
            await wait_for_tui(
                pilot,
                lambda: selector.label_filter == ("bug", "RAG", "Tester Agent"),
                description="no-match filter",
            )
            assert selector.visible_task_ids == ()
            hint = str(screen.query_one("#task-hint", Static).renderable)
            assert "No loaded github tasks have every selected label" in hint
            assert "No open github tasks were returned." not in hint
            assert screen.query_one("#task-table-header", TaskTableHeader).display is False
            assert screen.query_one("#select-all-tasks", Checkbox).disabled is True

            clear = screen.query_one("#clear-task-label-filter", Button)
            assert clear.disabled is False
            clear.press()
            await wait_for_tui(
                pilot,
                lambda: selector.label_filter == () and label_list.selected == [],
                description="cleared label filter",
            )
            assert selector.visible_task_ids == ("1", "2", "3", "4", "7")
            assert all(row.display for row in screen.query(TaskRow))
            assert screen.query_one("#clear-task-label-filter", Button).disabled is True
            assert "Selected 0 of 4 available github tasks." in str(
                screen.query_one("#task-hint", Static).renderable
            )

    asyncio.run(drive())


def test_select_all_applies_only_to_shown_available_tasks(tmp_path: Path) -> None:
    controller, service, _ = _labelled_controller(tmp_path)
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test(size=(100, 30)) as pilot:
            await pilot.press("n")
            selector = await _wait_for_labelled_tasks(pilot, application)
            screen = application.screen
            header = screen.query_one("#select-all-tasks", Checkbox)
            screen.query_one("#task-checkbox-2", TaskRow).value = True
            await wait_for_tui(
                pilot,
                lambda: selector.selected_task_ids == ("3",),
                description="selection before filtering",
            )

            screen.query_one("#task-label-list", SelectionList).select("bug")
            await wait_for_tui(
                pilot,
                lambda: selector.label_filter == ("bug",),
                description="bug label filter",
            )
            assert header.value is False
            assert str(header.label) == "Select all shown tasks without earlier runs"

            header.value = True
            await wait_for_tui(
                pilot,
                lambda: selector.selected_task_ids == ("1", "2", "3"),
                description="select all shown tasks",
            )
            # Task 7 has an execution; task 3 stays selected while hidden.
            assert screen.query_one("#task-checkbox-4", TaskRow).value is False
            assert header.value is True
            assert "Select all includes only tasks without earlier runs." in str(
                screen.query_one("#task-hint", Static).renderable
            )
            assert "1 selected task(s) hidden by the filter will also start." in str(
                screen.query_one("#task-hint", Static).renderable
            )
            assert str(screen.query_one("#start-workflow", Button).label) == (
                "Start workflow (3)"
            )

            header.value = False
            await wait_for_tui(
                pilot,
                lambda: selector.selected_task_ids == ("3",),
                description="unselect all shown tasks",
            )
            assert screen.query_one("#task-checkbox-2", TaskRow).value is True

            screen.query_one("#clear-task-label-filter", Button).press()
            await wait_for_tui(
                pilot,
                lambda: selector.label_filter == (),
                description="cleared label filter",
            )
            assert selector.selected_task_ids == ("3",)
            assert header.value is False
            assert str(header.label) == "Select all tasks without earlier runs"

            screen.query_one("#start-workflow", Button).press()
            await wait_for_tui(
                pilot,
                lambda: len(service.calls) == 1,
                description="selected hidden-then-shown task launch",
            )
            assert service.calls[0][0] == "3"

    asyncio.run(drive())


@pytest.mark.parametrize("state", ["unsupported", "empty", "failed"])
def test_task_label_filter_distinguishes_unsupported_empty_and_failed(
    tmp_path: Path, state: str
) -> None:
    if state == "unsupported":
        controller, _, _ = _controller(tmp_path)
    elif state == "empty":
        controller, _, _ = _labelled_controller(tmp_path, labels=[])
    else:
        controller, _, _ = _labelled_controller(
            tmp_path, label_error=RuntimeError("label scope missing")
        )
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test(size=(100, 30)) as pilot:
            await pilot.press("n")
            await wait_for_tui(
                pilot,
                lambda: len(application.screen.query(TaskRow)) > 0,
                description="loaded task rows",
            )
            screen = application.screen
            selector = screen.query_one("#task-selector", TaskSelector)
            filter_panel = screen.query_one("#task-label-filter", Vertical)
            status = str(screen.query_one("#task-label-status", Static).renderable)
            assert len(screen.query("#task-label-list")) == 0
            assert selector.label_filter == ()
            if state == "unsupported":
                assert selector.label_catalog is None
                assert filter_panel.display is False
            elif state == "empty":
                assert selector.label_catalog == ()
                assert filter_panel.display is True
                assert "no labels to filter by" in status
            else:
                assert selector.label_catalog == ()
                assert filter_panel.display is True
                assert "Could not load labels: label scope missing" in status
                assert screen.query_one("#task-label-status", Static).has_class("-error")
                # Tasks remain usable when only the catalog failed.
                assert len(screen.query(TaskRow)) == 5
                assert str(screen.query_one("#task-error", Static).renderable) == ""

    asyncio.run(drive())


def test_task_label_filter_reconciles_on_refresh_and_resets_on_source_change(
    tmp_path: Path,
) -> None:
    controller, _, provider = _labelled_controller(tmp_path)
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test(size=(100, 30)) as pilot:
            await pilot.press("n")
            selector = await _wait_for_labelled_tasks(pilot, application)
            screen = application.screen
            label_list = screen.query_one("#task-label-list", SelectionList)
            label_list.select("bug")
            label_list.select("RAG")
            await wait_for_tui(
                pilot,
                lambda: selector.label_filter == ("bug", "RAG"),
                description="two-label filter",
            )

            # A refresh keeps selected labels that still exist in the catalog.
            provider.labels = ["bug", "Tester Agent"]
            screen.query_one("#refresh-tasks", Button).press()
            await wait_for_tui(
                pilot,
                lambda: selector.label_catalog == ("bug", "Tester Agent"),
                description="refreshed label catalog",
            )
            assert selector.label_filter == ("bug",)
            assert selector.visible_task_ids == ("1", "2", "7")
            assert screen.query_one("#task-label-list", SelectionList).selected == ["bug"]

            # A stale response cannot restore an old catalog or filter.
            stale_token = screen._task_load_token - 1
            screen._set_available_tasks(
                provider.tasks[:1], "github", stale_token, ("RAG",), ""
            )
            await settle_tui(pilot)
            assert selector.label_catalog == ("bug", "Tester Agent")
            assert selector.label_filter == ("bug",)

            # A provider without labels offers no filter, and switching back
            # starts unfiltered.
            screen.query_one("#task-provider", Select).value = "trello"
            await wait_for_tui(
                pilot,
                lambda: selector.label_catalog is None
                and len(screen.query(TaskRow)) == 1,
                description="trello task list",
            )
            assert selector.label_filter == ()
            assert screen.query_one("#task-label-filter", Vertical).display is False

            screen.query_one("#task-provider", Select).value = "github"
            await _wait_for_labelled_tasks(pilot, application)
            assert selector.label_filter == ()
            assert screen.query_one("#task-label-list", SelectionList).selected == []
            assert selector.visible_task_ids == ("1", "2", "3", "4", "7")

    asyncio.run(drive())


def test_execution_history_and_detail_display_cancelled_status(tmp_path: Path) -> None:
    controller, _, repository = _controller(tmp_path)
    repository.execution.status = WorkflowStatus.CANCELLED
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test() as pilot:
            application.push_screen(ExecutionHistoryScreen())
            await settle_tui(pilot)
            picker = application.screen.query_one("#execution-picker", OrchestratorSelect)
            assert "CANCELLED" in str(picker._options)

            application.push_screen(ExecutionDetailScreen(repository.execution.id))
            await settle_tui(pilot)
            status = application.screen.query_one("#execution-status", Static)
            assert str(status.renderable) == "CANCELLED"
            assert status.has_class("cancelled")

    asyncio.run(drive())


def test_workflow_runner_confirms_and_requests_cancellation(tmp_path: Path) -> None:
    controller, _, repository = _controller(tmp_path)
    repository.execution.status = WorkflowStatus.RUNNING
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test() as pilot:
            application.push_screen(WorkflowRunnerScreen())
            await settle_tui(pilot)
            screen = application.screen
            assert isinstance(screen, WorkflowRunnerScreen)
            screen._render_workflow_event(
                WorkflowEvent(
                    execution_id=repository.execution.id,
                    kind=WorkflowEventKind.EXECUTION_CREATED,
                    status=WorkflowStatus.RUNNING,
                )
            )
            screen._render_workflow_event(
                WorkflowEvent(
                    execution_id=repository.execution.id,
                    kind=WorkflowEventKind.STATUS_CHANGED,
                    status=WorkflowStatus.RUNNING,
                )
            )
            assert screen.query_one("#workflow-stop-actions", Horizontal).display is True
            screen.query_one("#stop-workflow", Button).press()
            await settle_tui(pilot)
            assert isinstance(application.screen, StopWorkflowConfirmation)
            application.screen.query_one("#confirm-stop-workflow", Button).press()
            await settle_tui(pilot)
            assert repository.cancellation_requests == [repository.execution.id]
            assert "Cancellation requested" in str(
                screen.query_one("#workflow-progress", Static).renderable
            )

    asyncio.run(drive())


@pytest.mark.parametrize(
    ("final_status", "expected_text"),
    [
        (WorkflowStatus.CHANGES_REQUESTED, "Review changes requested"),
        (WorkflowStatus.APPROVED, "Workflow approved"),
        (WorkflowStatus.FAILED, "Workflow failed"),
        (WorkflowStatus.CANCELLED, "Workflow cancelled"),
    ],
)
def test_workflow_runner_terminal_workflow_finished_resets_active_controls(
    tmp_path: Path, final_status: WorkflowStatus, expected_text: str
) -> None:
    controller, _, repository = _controller(tmp_path)
    repository.execution.status = WorkflowStatus.RUNNING
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test() as pilot:
            application.push_screen(WorkflowRunnerScreen())
            await settle_tui(pilot)
            screen = application.screen
            assert isinstance(screen, WorkflowRunnerScreen)
            for event in (
                WorkflowEvent(
                    execution_id=repository.execution.id,
                    kind=WorkflowEventKind.EXECUTION_CREATED,
                    status=WorkflowStatus.RUNNING,
                ),
                WorkflowEvent(
                    execution_id=repository.execution.id,
                    kind=WorkflowEventKind.STATUS_CHANGED,
                    status=WorkflowStatus.RUNNING,
                ),
                WorkflowEvent(
                    execution_id=repository.execution.id,
                    kind=WorkflowEventKind.STAGE_STARTED,
                    stage=WorkflowStage.REVIEW,
                ),
                WorkflowEvent(
                    execution_id=repository.execution.id,
                    kind=WorkflowEventKind.STAGE_FINISHED,
                    stage=WorkflowStage.REVIEW,
                    step_status=StepStatus.SUCCEEDED,
                ),
                WorkflowEvent(
                    execution_id=repository.execution.id,
                    kind=WorkflowEventKind.STATUS_CHANGED,
                    status=final_status,
                ),
                WorkflowEvent(
                    execution_id=repository.execution.id,
                    kind=WorkflowEventKind.WORKFLOW_FINISHED,
                    status=final_status,
                ),
            ):
                screen._render_workflow_event(event)
            await settle_tui(pilot)

            assert len(screen._event_lines) == 1
            assert "Workflow " not in screen._event_lines[0]
            repository.execution.status = final_status
            screen._workflow_finished(repository.execution)
            await settle_tui(pilot)

            assert screen.query_one("#workflow-stop-actions", Horizontal).display is False
            assert screen.query_one("#start-workflow", Button).disabled is False
            assert screen.query_one("#workflow-outcome", Vertical).display is True
            assert expected_text in str(
                screen.query_one("#workflow-progress", Static).renderable
            )
            assert screen.query_one("#copy-detailed-logs-button", Button).disabled is False
            assert screen.query_one("#outcome-continue", Button).display is (
                final_status is WorkflowStatus.CHANGES_REQUESTED
            )
            assert screen.query_one("#outcome-answer-later", Button).display is False

    asyncio.run(drive())


def test_workflow_runner_human_outcome_can_be_returned_to_and_answered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller, service, repository = _controller(tmp_path)
    interaction = HumanInteraction(
        id=1,
        workflow_execution_id=repository.execution.id,
        requesting_stage=WorkflowStage.REVIEW,
        prompt="Please clarify the requested change.",
        requested_at=datetime.now(UTC),
    )
    repository.execution.status = WorkflowStatus.NEEDS_HUMAN
    repository.execution.current_stage = WorkflowStage.REVIEW
    repository.execution.human_interactions = [interaction]
    review_step = WorkflowStep(
        id=1,
        workflow_execution_id=repository.execution.id,
        stage=WorkflowStage.REVIEW,
        status=StepStatus.SUCCEEDED,
        started_at=interaction.requested_at,
        finished_at=interaction.requested_at,
    )
    monkeypatch.setattr(repository, "list_steps", lambda _execution_id: [review_step])
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test() as pilot:
            application.push_screen(WorkflowRunnerScreen())
            await settle_tui(pilot)
            screen = application.screen
            assert isinstance(screen, WorkflowRunnerScreen)
            screen._render_workflow_event(
                WorkflowEvent(
                    execution_id=repository.execution.id,
                    kind=WorkflowEventKind.STAGE_STARTED,
                    stage=WorkflowStage.REVIEW,
                )
            )
            screen._render_workflow_event(
                WorkflowEvent(
                    execution_id=repository.execution.id,
                    kind=WorkflowEventKind.STAGE_FINISHED,
                    stage=WorkflowStage.REVIEW,
                    step_status=StepStatus.SUCCEEDED,
                )
            )
            screen._render_workflow_event(
                WorkflowEvent(
                    execution_id=repository.execution.id,
                    kind=WorkflowEventKind.WORKFLOW_FINISHED,
                    stage=WorkflowStage.REVIEW,
                    status=WorkflowStatus.NEEDS_HUMAN,
                )
            )
            screen._workflow_finished(repository.execution)
            await settle_tui(pilot)

            outcome = screen.query_one("#workflow-outcome", Vertical)
            assert outcome.display is True
            assert "stopped" in str(
                screen.query_one("#workflow-outcome-title", Static).renderable
            ).lower()
            assert "waiting for your response" in str(
                screen.query_one("#workflow-outcome-title", Static).renderable
            ).lower()
            assert screen.query_one("#human-input-panel", Vertical).display is True
            assert screen.query_one("#outcome-answer-later", Button).display is False
            assert "waiting for human input" in str(
                screen.query_one("#workflow-events", Static).renderable
            )
            assert "Workflow" not in str(screen.query_one("#workflow-events", Static).renderable)

            screen.query_one("#return-later", Button).press()
            await settle_tui(pilot)
            assert outcome.display is True
            assert screen.query_one("#human-input-panel", Vertical).display is False
            assert screen.query_one("#outcome-answer-later", Button).display is True
            assert screen.query_one("#workflow-outcome-actions", Horizontal).display is True

            screen.query_one("#outcome-answer-later", Button).press()
            await settle_tui(pilot)
            assert isinstance(application.screen, ExecutionDetailScreen)
            assert application.screen.query_one("#execution-human-title", Static).display is True
            await pilot.press("escape")
            await settle_tui(pilot)
            assert isinstance(application.screen, WorkflowRunnerScreen)
            assert screen.query_one("#human-input-panel", Vertical).display is True

            screen.query_one("#human-response", TextArea).text = "Add a concrete example."
            await settle_tui(pilot)
            assert screen.query_one("#resume-workflow", Button).disabled is False
            screen.query_one("#resume-workflow", Button).press()
            await settle_tui(pilot)
            assert service.responses == [
                (repository.execution.id, "Add a concrete example.")
            ]
            assert "Workflow completed" in str(
                screen.query_one("#workflow-outcome-title", Static).renderable
            )
            assert screen.query_one("#human-input-panel", Vertical).display is False

    asyncio.run(drive())


def test_workflow_runner_continue_action_opens_detail_and_reconciles_state(
    tmp_path: Path,
) -> None:
    controller, _, repository = _controller(tmp_path)
    repository.execution.status = WorkflowStatus.CHANGES_REQUESTED
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test(size=(48, 16)) as pilot:
            application.push_screen(WorkflowRunnerScreen())
            await settle_tui(pilot)
            screen = application.screen
            assert isinstance(screen, WorkflowRunnerScreen)
            screen._workflow_finished(repository.execution)
            await settle_tui(pilot)

            continue_button = screen.query_one("#outcome-continue", Button)
            assert continue_button.display is True
            assert "stopped" in str(
                screen.query_one("#workflow-outcome-title", Static).renderable
            ).lower()
            runner_scroll = screen.query_one("#workflow-runner", VerticalScroll)
            continue_button.focus()
            await wait_for_tui(
                pilot,
                lambda: runner_scroll.scroll_y > 0
                and screen.can_view_partial(continue_button),
                description="focused Continue button in the runner viewport",
            )
            assert runner_scroll.scroll_y > 0
            assert continue_button.region.y < screen.size.height

            continue_button.press()
            await settle_tui(pilot)
            detail = application.screen
            assert isinstance(detail, ExecutionDetailScreen)
            assert detail.query_one("#execution-continuation-title", Static).display is True
            assert str(
                detail.query_one("#execution-continuation-feedback", TextArea).text
            ) == "Persisted terminal feedback"

            repository.execution.status = WorkflowStatus.APPROVED
            await pilot.press("escape")
            await settle_tui(pilot)
            assert isinstance(application.screen, WorkflowRunnerScreen)
            assert "completed" in str(
                screen.query_one("#workflow-outcome-title", Static).renderable
            ).lower()
            assert screen.query_one("#outcome-continue", Button).display is False

            repository.execution.status = WorkflowStatus.FAILED
            repository.execution.workspace_path = str(tmp_path / "worktree")
            repository.execution.working_branch = "workflow/12"
            screen._workflow_finished(repository.execution)
            await settle_tui(pilot)
            assert screen.query_one("#outcome-continue", Button).display is True

            repository.execution.workspace_path = None
            repository.execution.working_branch = None
            screen._workflow_finished(repository.execution)
            await settle_tui(pilot)
            assert screen.query_one("#outcome-continue", Button).display is False
            assert "no continuation" in str(
                screen.query_one("#workflow-outcome-message", Static).renderable
            ).lower()

    asyncio.run(drive())


def test_workflow_runner_does_not_need_to_scroll_in_a_tall_terminal(tmp_path: Path) -> None:
    controller, _, _ = _controller(tmp_path)
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test(size=(120, 200)) as pilot:
            application.push_screen(WorkflowRunnerScreen())
            await settle_tui(pilot)
            content = application.screen.query_one("#workflow-runner", VerticalScroll)
            assert content.max_scroll_y == 0

    asyncio.run(drive())


def test_secondary_screens_keep_content_in_scrollable_regions_in_small_terminals(
    tmp_path: Path,
) -> None:
    controller, _, repository = _controller(tmp_path)
    repository.diagnostics = [ExecutionDiagnostic(
        execution_id=repository.execution.id,
        category=DiagnosticCategory.RESPONSE,
        payload={"output": f"response {number}"},
    ) for number in range(40)]
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test(size=(80, 20)) as pilot:
            application.push_screen(ExecutionHistoryScreen())
            await settle_tui(pilot)
            assert isinstance(application.screen.query_one("#execution-history"), VerticalScroll)

            application.push_screen(ExecutionDetailScreen(repository.execution.id))
            await settle_tui(pilot)
            detail = application.screen.query_one("#execution-detail", VerticalScroll)
            application.screen.query_one("#execution-toggle-logs", Button).press()
            await settle_tui(pilot)
            detail_content = application.screen.query_one("#execution-detailed-logs", Log)
            assert detail.max_scroll_y > 0
            assert detail_content.size.height > 20
            detail.scroll_end(animate=False)
            await settle_tui(pilot)
            assert detail.scroll_y > 0

            await pilot.press("escape")
            application.push_screen(GitHubAppSettingsScreen())
            await settle_tui(pilot)
            settings = application.screen.query_one("#github-app-settings", VerticalScroll)
            assert settings.max_scroll_y > 0
            application.screen.query_one("#save-github-apps", Button).focus()
            await settle_tui(pilot)
            assert settings.scroll_y > 0

    asyncio.run(drive())


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (WorkflowStatus.PENDING, "QUEUED"),
        (WorkflowStatus.WAITING, "WAITING FOR AGENT"),
        (WorkflowStatus.RUNNING, "RUNNING"),
        (WorkflowStatus.NEEDS_HUMAN, "AWAITING HUMAN"),
        (WorkflowStatus.APPROVED, "APPROVED"),
        (WorkflowStatus.CANCELLED, "CANCELLED"),
    ],
)
def test_execution_status_label_distinguishes_queued_from_provider_waiting(
    status: WorkflowStatus, expected: str
) -> None:
    execution = WorkflowExecution(id="execution-1", status=status)

    assert execution_status_label(execution) == expected


@pytest.mark.parametrize(
    "status", [WorkflowStatus.PENDING, WorkflowStatus.WAITING, WorkflowStatus.RUNNING]
)
def test_every_active_status_reports_a_pending_cancellation(status: WorkflowStatus) -> None:
    execution = WorkflowExecution(
        id="execution-1",
        status=status,
        cancellation_requested_at=datetime.now(UTC),
    )

    assert execution_status_label(execution) == "CANCELLATION REQUESTED"


def test_terminal_status_ignores_a_historical_cancellation_marker() -> None:
    execution = WorkflowExecution(
        id="execution-1",
        status=WorkflowStatus.CANCELLED,
        cancellation_requested_at=datetime.now(UTC),
    )

    assert execution_status_label(execution) == "CANCELLED"


def test_live_status_message_names_the_provider_a_run_is_waiting_for() -> None:
    waiting_for_provider = WorkflowRunnerScreen._status_message(
        WorkflowStatus.WAITING, {"provider": "ollama"}
    )

    assert waiting_for_provider == "Waiting for ollama capacity…"
    # Without a provider in the payload the message stays generic but distinct.
    assert (
        WorkflowRunnerScreen._status_message(WorkflowStatus.WAITING)
        == "Waiting for agent capacity…"
    )
    assert (
        WorkflowRunnerScreen._status_message(WorkflowStatus.PENDING)
        == "Queued for an execution slot…"
    )
    assert WorkflowRunnerScreen._status_message(WorkflowStatus.RUNNING) == "Workflow running…"


@pytest.mark.parametrize(
    "status", [WorkflowStatus.PENDING, WorkflowStatus.WAITING, WorkflowStatus.RUNNING]
)
def test_stop_control_is_offered_for_every_active_status(
    tmp_path: Path, status: WorkflowStatus
) -> None:
    controller, _, _ = _controller(tmp_path)
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test(size=(120, 50)) as pilot:
            application.push_screen(WorkflowRunnerScreen())
            await settle_tui(pilot)
            screen = application.screen
            screen._render_workflow_event(
                WorkflowEvent(
                    execution_id="execution-1",
                    kind=WorkflowEventKind.STATUS_CHANGED,
                    status=status,
                )
            )
            await settle_tui(pilot)

            assert screen.query_one("#workflow-stop-actions", Horizontal).display is True
            assert screen.query_one("#stop-workflow", Button).disabled is False

    asyncio.run(drive())


def test_history_filters_and_renders_provider_waiting_executions(tmp_path: Path) -> None:
    controller, _, repository = _controller(tmp_path)
    repository.execution.status = WorkflowStatus.WAITING
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test(size=(120, 50)) as pilot:
            application.push_screen(ExecutionHistoryScreen())
            await settle_tui(pilot)
            screen = application.screen

            # A WAITING execution is still listed and searchable by its status.
            screen.query_one("#history-filter", Input).value = "waiting"
            await settle_tui(pilot)
            picker = screen.query_one("#execution-picker", OrchestratorSelect)
            assert "1 persisted workflow execution(s)." in str(
                screen.query_one("#history-summary", Static).renderable
            )
            assert any(
                "WAITING FOR AGENT" in str(label) for label, _ in picker._options
            )

            screen.query_one("#history-filter", Input).value = "approved"
            await settle_tui(pilot)
            assert "0 persisted workflow execution(s)." in str(
                screen.query_one("#history-summary", Static).renderable
            )

            application.push_screen(ExecutionDetailScreen(repository.execution.id))
            await settle_tui(pilot)
            detail_status = application.screen.query_one("#execution-status", Static)
            assert str(detail_status.renderable) == "WAITING FOR AGENT"

    asyncio.run(drive())


def test_controller_routes_cancellation_to_the_live_service_then_the_repository(
    tmp_path: Path,
) -> None:
    controller, service, repository = _controller(tmp_path)
    repository.execution.status = WorkflowStatus.WAITING

    # No live service yet: the durable repository request keeps a restarted TUI
    # able to stop work it no longer owns in memory.
    assert controller.cancel_workflow("execution-1") is True
    assert repository.cancellation_requests == ["execution-1"]
    assert service.cancellations == []

    controller._active_services["execution-1"] = service  # type: ignore[assignment]

    assert controller.cancel_workflow("execution-1") is True
    assert service.cancellations == ["execution-1"]
