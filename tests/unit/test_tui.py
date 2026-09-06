from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Button, Input, Select, Static

from orchestrator.config.project_config import (
    ConfiguredRepository,
    GitHubAppSettings,
    ProjectConfig,
)
from orchestrator.models.agent import AgentRole
from orchestrator.models.development_task import DevelopmentTask
from orchestrator.models.workflow_event import WorkflowEvent, WorkflowEventKind
from orchestrator.models.workflow_execution import WorkflowExecution
from orchestrator.models.workflow_status import StepStatus, WorkflowStage, WorkflowStatus
from orchestrator.tui.app import OrchestratorApp
from orchestrator.tui.bridge import WorkflowController, WorkflowLaunch
from orchestrator.tui.screens.execution_detail import ExecutionDetailScreen
from orchestrator.tui.screens.execution_history import ExecutionHistoryScreen
from orchestrator.tui.screens.github_app_settings import GitHubAppSettingsScreen
from orchestrator.tui.screens.task_selector import TaskSelector
from orchestrator.tui.screens.workflow_runner import WorkflowRunnerScreen
from orchestrator.tui.widgets import OrchestratorSelect


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
        self.saved_github_apps: tuple[str, GitHubAppSettings, GitHubAppSettings] | None = None
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
                    }
                },
            }
        )

    def list_repositories(self) -> list[ConfiguredRepository]:
        return [ConfiguredRepository("example", self._path)]

    def load(self, task: DevelopmentTask, repository: str | None = None) -> ProjectConfig:
        assert task.repository == repository == "example"
        return self.config

    def save_github_app_settings(
        self,
        repository_key: str,
        *,
        implementer_app: GitHubAppSettings,
        reviewer_app: GitHubAppSettings,
    ) -> Path:
        assert repository_key == "example"
        self.saved_github_apps = (repository_key, implementer_app, reviewer_app)
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


class _WorkflowService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None, dict[AgentRole, str], dict[AgentRole, Any]]] = []

    def run(
        self,
        task_id: str,
        repository: str | None,
        *,
        agent_providers: dict[AgentRole, str],
        agent_runtime_options: dict[AgentRole, Any],
        event_listener: Callable[..., None] | None,
    ) -> WorkflowExecution:
        self.calls.append((task_id, repository, agent_providers, agent_runtime_options))
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
            {},
        )
    ]


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
                agent_runtime_options={AgentRole.PLANNER: {"model": "not-supported"}},
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


def test_tui_buttons_activate_on_initial_click(tmp_path: Path) -> None:
    """A pointer click should activate an action without a focus-setting click first."""

    controller, _, _ = _controller(tmp_path)
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test(size=(120, 50)) as pilot:
            await pilot.click("#new-workflow")
            assert isinstance(application.screen, WorkflowRunnerScreen)
            await pilot.pause()
            await pilot.click("#workflow-history")
            assert isinstance(application.screen, ExecutionHistoryScreen)

    asyncio.run(drive())


def test_select_opens_on_first_click_and_restores_focus_when_closed(tmp_path: Path) -> None:
    controller, _, _ = _controller(tmp_path)
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test(size=(120, 50)) as pilot:
            await pilot.press("n")
            await pilot.pause()
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
            await pilot.pause()
            assert content.max_scroll_y > 0

            before_open = content.scroll_y
            await pilot.click("#viewport-select")
            await pilot.pause(delay=0.1)
            overlay = select.query_one("SelectOverlay")
            assert select.expanded is True
            assert content.scroll_y >= before_open
            assert overlay.region.y >= content.content_region.y
            assert overlay.region.bottom <= content.content_region.bottom

            opened_scroll = content.scroll_y
            await pilot.press("escape")
            await pilot.pause()
            assert select.expanded is False
            assert application.screen.focused is select
            assert content.scroll_y == opened_scroll

            await pilot.press("enter", "down", "enter")
            await pilot.pause()
            assert select.value == "one"
            assert application.screen.focused is select

    asyncio.run(drive())


def test_select_focus_scrolls_the_control_into_view_on_small_terminals(tmp_path: Path) -> None:
    controller, _, _ = _controller(tmp_path)
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test(size=(80, 20)) as pilot:
            await pilot.press("n")
            await pilot.pause()
            screen = application.screen
            screen.query_one("#planner-configure", Button).press()
            await pilot.pause()
            select = screen.query_one("#planner-provider", Select)
            select.focus()
            await pilot.pause()
            content = screen.query_one("#workflow-runner", VerticalScroll)
            await pilot.press("enter")
            await pilot.pause()
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

    assert WorkflowRunnerScreen.github_app_summary(configured, configured) == (
        "GitHub Apps: Implementer ✓ · Reviewer ✓"
    )
    assert WorkflowRunnerScreen.github_app_summary(configured, unconfigured) == (
        "GitHub Apps: Implementer ✓ · Reviewer —"
    )
    assert "12" not in WorkflowRunnerScreen.github_app_summary(configured, unconfigured)


def test_workflow_runner_maps_live_events_to_timeline_and_keeps_raw_logs_optional(
    tmp_path: Path,
) -> None:
    controller, _, _ = _controller(tmp_path)
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test() as pilot:
            application.push_screen(WorkflowRunnerScreen())
            await pilot.pause()
            screen = application.screen
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
            screen._render_workflow_event(finished)

            timeline = str(screen.query_one("#workflow-events", Static).renderable)
            assert "✓ Changes implemented" in timeline
            assert "STAGE_STARTED" not in timeline
            assert "STAGE_FINISHED" not in timeline

            screen.query_one("#toggle-workflow-logs", Button).press()
            await pilot.pause()
            raw_events = screen.query_one("#workflow-raw-events", Static)
            assert raw_events.display is True
            assert "STAGE_STARTED" in str(raw_events.renderable)
            assert "STAGE_FINISHED" in str(raw_events.renderable)

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
            await pilot.pause()
            assert screen.query_one("#planner-config").display is False
            assert screen.query_one("#planner-advanced").display is False
            assert "planner (Provider/YAML default)" in str(
                screen.query_one("#planner-summary").renderable
            )
            assert screen.query_one("#planner-provider").value == "planner"
            assert screen.query_one("#planner-model").display is False
            assert screen.query_one("#implementer-model").display is True
            assert screen.query_one("#implementer-reasoning").display is True
            assert screen.query_one("#implementer-model").value == ""

            screen.query_one("#planner-provider").value = "implementer"
            await pilot.pause()
            assert screen.query_one("#planner-model").display is True
            assert screen.query_one("#planner-reasoning").display is True

    asyncio.run(drive())


def test_workflow_runner_expands_advanced_controls_and_preserves_overrides(
    tmp_path: Path,
) -> None:
    controller, service, _ = _controller(tmp_path)
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test(size=(120, 50)) as pilot:
            await pilot.press("n")
            await pilot.pause()
            screen = application.screen
            configure = screen.query_one("#planner-configure", Button)
            configure.focus()
            await pilot.press("enter")
            assert screen.query_one("#planner-config").display is True

            screen.query_one("#planner-provider", Select).value = "vertex"
            await pilot.pause()
            assert "vertex (this-run provider override)" in str(
                screen.query_one("#planner-summary").renderable
            )
            assert screen.query_one("#planner-advanced-toggle", Button).display is True
            assert screen.query_one("#planner-advanced").display is False

            advanced = screen.query_one("#planner-advanced-toggle", Button)
            advanced.press()
            await pilot.pause()
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
            await pilot.pause()
            assert "This-run override" in str(
                screen.query_one("#planner-temperature-source").renderable
            )

            advanced.press()
            await pilot.pause()
            assert screen.query_one("#planner-advanced").display is False
            assert screen.query_one("#planner-temperature", Input).value == "0.8"

            screen.query_one("#available-tasks", Select).value = "12"
            await pilot.pause()
            screen.query_one("#start-workflow", Button).focus()
            await pilot.press("enter")
            await pilot.pause(delay=0.1)
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
            await pilot.pause()
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
            await pilot.pause()
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
            await pilot.pause(delay=0.1)
            screen = application.screen
            assert task_loads == [("example", "github")]
            assert screen.query_one("#available-tasks", Select).value == Select.BLANK
            assert "1 open github tasks available." in str(
                screen.query_one("#task-hint").renderable
            )
            assert len(screen.query("#task-id")) == 0

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
    controller.configured_agent_providers = (  # type: ignore[method-assign]
        lambda repository: list(capabilities)
    )
    controller.agent_providers = lambda repository: agent_providers  # type: ignore[method-assign]
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
            await pilot.pause(delay=0.1)
            screen = application.screen
            screen.query_one("#task-provider", Select).value = "trello"
            await pilot.pause(delay=0.1)
            screen.query_one("#repository", Select).value = "other"
            await pilot.pause(delay=0.1)
            assert task_loads == [
                ("example", "github"),
                ("example", "trello"),
                ("other", "github"),
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
            await pilot.pause(delay=0.1)
            screen = application.screen
            screen.query_one("#refresh-tasks", Button).focus()
            await pilot.press("enter")
            await pilot.pause(delay=0.1)
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
            await pilot.pause(delay=0.1)
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
        async with application.run_test() as pilot:
            await pilot.press("c")
            assert isinstance(application.screen, GitHubAppSettingsScreen)
            repository = application.screen.query_one("#github-app-repository")
            repository.value = "example"
            await pilot.pause()
            assert application.screen.query_one("#implementer-app-id").value == "56"
            assert application.screen.query_one("#reviewer-app-id").value == "12"
            application.screen.query_one("#implementer-app-id").value = "123"
            application.screen.query_one("#implementer-installation-id").value = "456"
            application.screen.query_one("#implementer-key-env").value = "IMPLEMENTER_KEY"
            application.screen.query_one("#save-github-apps", Button).focus()
            await pilot.pause()
            await pilot.press("enter")
            assert "Saved App references" in str(
                application.screen.query_one("#github-app-status").renderable
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

            await pilot.pause(delay=0.1)
            screen.query_one("#available-tasks", Select).value = "12"
            await pilot.pause()
            assert screen.query_one("#task-selector", TaskSelector).selected_task_id == "12"
            screen.query_one("#start-workflow", Button).focus()
            await pilot.pause(delay=0.1)
            assert content.scroll_y > 0
            await pilot.press("enter")
            await pilot.pause(delay=0.1)
            assert service.calls[0][0:2] == ("12", "example")

    asyncio.run(drive())


def test_workflow_runner_does_not_need_to_scroll_in_a_tall_terminal(tmp_path: Path) -> None:
    controller, _, _ = _controller(tmp_path)
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test(size=(120, 200)) as pilot:
            application.push_screen(WorkflowRunnerScreen())
            await pilot.pause()
            content = application.screen.query_one("#workflow-runner", VerticalScroll)
            assert content.max_scroll_y == 0

    asyncio.run(drive())


def test_secondary_screens_keep_content_in_scrollable_regions_in_small_terminals(
    tmp_path: Path,
) -> None:
    controller, _, repository = _controller(tmp_path)
    repository.execution.validation_snapshot = {
        f"check_{number}": "passed" for number in range(40)
    }
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test(size=(80, 20)) as pilot:
            application.push_screen(ExecutionHistoryScreen())
            await pilot.pause()
            assert isinstance(application.screen.query_one("#execution-history"), VerticalScroll)

            application.push_screen(ExecutionDetailScreen(repository.execution.id))
            await pilot.pause()
            detail = application.screen.query_one("#execution-detail", VerticalScroll)
            assert detail.max_scroll_y > 0
            detail.scroll_end(animate=False)
            await pilot.pause()
            assert detail.scroll_y > 0

            await pilot.press("escape")
            application.push_screen(GitHubAppSettingsScreen())
            await pilot.pause()
            settings = application.screen.query_one("#github-app-settings", VerticalScroll)
            assert settings.max_scroll_y > 0
            application.screen.query_one("#save-github-apps", Button).focus()
            await pilot.pause(delay=0.1)
            assert settings.scroll_y > 0

    asyncio.run(drive())
