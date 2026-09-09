from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from textual.app import App, ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Button, Input, Select, Static, TextArea

from orchestrator.config.project_config import (
    AgentPresetConfig,
    AgentProviderSettings,
    ConfiguredRepository,
    GitHubAppSettings,
    ProjectConfig,
)
from orchestrator.models.agent import AgentRole
from orchestrator.models.development_task import DevelopmentTask
from orchestrator.models.execution_diagnostic import DiagnosticCategory, ExecutionDiagnostic
from orchestrator.models.workflow_event import WorkflowEvent, WorkflowEventKind
from orchestrator.models.workflow_execution import WorkflowExecution
from orchestrator.models.workflow_status import StepStatus, WorkflowStage, WorkflowStatus
from orchestrator.tui.app import OrchestratorApp
from orchestrator.tui.bridge import WorkflowController, WorkflowLaunch
from orchestrator.tui.diagnostics import format_diagnostics_for_export
from orchestrator.tui.screens.agent_instructions import AgentInstructionsScreen
from orchestrator.tui.screens.agent_presets import AgentPresetsScreen
from orchestrator.tui.screens.execution_detail import ExecutionDetailScreen
from orchestrator.tui.screens.execution_history import ExecutionHistoryScreen
from orchestrator.tui.screens.github_app_settings import GitHubAppSettingsScreen
from orchestrator.tui.screens.task_selector import TaskSelector
from orchestrator.tui.screens.workflow_runner import StopWorkflowConfirmation, WorkflowRunnerScreen
from orchestrator.tui.widgets import OrchestratorSelect, RepositorySelect


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
        self.diagnostics: list[ExecutionDiagnostic] = []
        self.cancellation_requests: list[str] = []

    def list_executions(self) -> list[WorkflowExecution]:
        return self.executions

    def get_execution(self, execution_id: str) -> WorkflowExecution:
        assert execution_id == self.execution.id
        return self.execution

    def list_steps(self, execution_id: str) -> list[Any]:
        assert execution_id == self.execution.id
        return []

    def list_diagnostics(self, execution_id: str) -> list[ExecutionDiagnostic]:
        assert execution_id == self.execution.id
        return self.diagnostics

    def request_cancellation(self, execution_id: str) -> bool:
        assert execution_id == self.execution.id
        if self.execution.status is not WorkflowStatus.RUNNING:
            return False
        self.cancellation_requests.append(execution_id)
        return True


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


class _WorkflowService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None, dict[AgentRole, str], dict[AgentRole, Any]]] = []
        self.emit_terminal_event = False

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
        if self.emit_terminal_event and event_listener is not None:
            event_listener(
                WorkflowEvent(
                    execution_id="execution-1",
                    kind=WorkflowEventKind.EXECUTION_CREATED,
                    status=WorkflowStatus.PENDING,
                )
            )
            event_listener(
                WorkflowEvent(
                    execution_id="execution-1",
                    kind=WorkflowEventKind.WORKFLOW_FINISHED,
                    status=WorkflowStatus.APPROVED,
                )
            )
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
            assert isinstance(application.screen, AgentInstructionsScreen)
            assert isinstance(
                application.screen.query_one("#agent-instructions"), VerticalScroll
            )

    asyncio.run(drive())


def test_main_navigation_reflows_compact_controls_without_content_sizing(tmp_path: Path) -> None:
    controller, _, _ = _controller(tmp_path)
    application = OrchestratorApp(controller)
    navigation_ids = (
        "new-workflow",
        "history",
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
            await pilot.pause()

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
        "workflow-github-apps",
        "workflow-agent-instructions",
        "workflow-agent-presets",
    )

    async def drive() -> None:
        async with application.run_test(size=(120, 50)) as pilot:
            application.push_screen(WorkflowRunnerScreen())
            await pilot.pause()
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
            await pilot.pause()

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
        ("#github-apps", GitHubAppSettingsScreen),
        ("#agent-instructions-button", AgentInstructionsScreen),
        ("#agent-presets", AgentPresetsScreen),
    )

    async def drive() -> None:
        async with application.run_test(size=(120, 50)) as pilot:
            for selector, expected_screen in destinations:
                await pilot.click(selector)
                await pilot.pause()
                assert isinstance(application.screen, expected_screen)
                await pilot.press("escape")
                await pilot.pause()

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
            await pilot.pause()
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
            await pilot.pause()

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
            await pilot.pause()
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
            await pilot.pause()
            assert raw_events.display is True
            assert raw_events_title.display is True
            assert (
                screen.query_one("#toggle-workflow-logs", Button).label.plain
                == "Hide detailed logs"
            )
            assert raw_events.size.height > 200
            assert content.max_scroll_y > 0
            content.scroll_end(animate=False)
            await pilot.pause()
            assert content.scroll_y > 0
            raw_log = str(raw_events.renderable)
            assert "STAGE_STARTED" in raw_log
            assert "STAGE_FINISHED" in raw_log
            assert "AGENT_FALLBACK" in raw_log
            assert "REQUEST" in raw_log

            screen.query_one("#toggle-workflow-logs", Button).press()
            await pilot.pause()
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
            await pilot.pause()
            summary = application.screen.query_one("#implementer-summary", Static)
            assert "fallback: vertex/backup" in str(summary.renderable)
            assert "repository setting; unchanged" not in str(summary.renderable)

            application.screen.query_one("#implementer-model", Input).value = "temporary-model"
            await pilot.pause()

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
            await pilot.pause()
            screen = application.screen
            screen.query_one("#implementer-preset", Select).value = "deep"
            await pilot.pause()
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
            await pilot.pause()
            assert screen.query_one("#implementer-preset", Select).value != "deep"
            assert "Manual configuration; temporary this-run overrides" in str(
                screen.query_one("#implementer-preset-source", Static).renderable
            )
            screen.query_one("#available-tasks", Select).value = "12"
            screen.query_one("#start-workflow", Button).press()
            await pilot.pause(delay=0.1)
            assert service.calls[-1][3][AgentRole.IMPLEMENTER] == {
                "model": "temporary-model",
                "reasoning_effort": "xhigh",
            }
            assert config.agent_presets["implementer"]["deep"].model == "preset-model"

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
            await pilot.pause()
            screen = application.screen
            reviewer_preset = screen.query_one("#reviewer-preset", Select)
            assert {option[1] for option in reviewer_preset._options} == {
                reviewer_preset._options[0][1],
                "thorough",
            }
            assert "deep" not in {option[1] for option in reviewer_preset._options}

            reviewer_preset.value = "thorough"
            await pilot.pause()
            assert screen.query_one("#reviewer-provider", Select).value == "vertex"
            assert screen.query_one("#reviewer-model", Input).value == "review-model"
            assert screen.query_one("#reviewer-temperature", Input).value == "0.4"

            reviewer_preset.value = reviewer_preset._options[0][1]
            await pilot.pause()
            assert screen.query_one("#reviewer-provider", Select).value == "reviewer"
            assert screen.query_one("#reviewer-model", Input).value == ""
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
            await pilot.pause()
            assert isinstance(application.screen, AgentPresetsScreen)
            screen = application.screen
            repository = screen.query_one("#agent-presets-repository", Select)
            repository.value = "example"
            await pilot.pause()
            role = screen.query_one("#agent-preset-role", Select)
            assert {option[1] for option in role._options} == {
                "planner", "implementer", "reviewer"
            }

            role.value = "implementer"
            await pilot.pause()
            screen.query_one("#agent-preset-select", Select).value = "deep"
            await pilot.pause()
            fallback = screen.query_one("#agent-fallback-preset", Select)
            fallback.value = "deep"
            screen.query_one("#save-agent-fallback", Button).press()
            await pilot.pause()
            assert config.agents.implementer.fallback_preset == "deep"
            screen.query_one("#agent-preset-model", Input).value = "updated"
            screen.query_one("#update-agent-preset", Button).press()
            await pilot.pause()
            assert config.agent_presets["implementer"]["deep"].model == "updated"

            screen.query_one("#agent-preset-name", Input).value = "renamed"
            screen.query_one("#rename-agent-preset", Button).press()
            await pilot.pause()
            assert "renamed" in config.agent_presets["implementer"]
            fallback.value = fallback._options[0][1]
            screen.query_one("#save-agent-fallback", Button).press()
            await pilot.pause()
            assert config.agents.implementer.fallback_preset is None
            screen.query_one("#delete-agent-preset", Button).press()
            await pilot.pause()
            assert "implementer" not in config.agent_presets

            role.value = "planner"
            await pilot.pause()
            screen.query_one("#agent-preset-name", Input).value = "fast"
            screen.query_one("#agent-preset-provider", Select).value = "planner"
            screen.query_one("#create-agent-preset", Button).press()
            await pilot.pause()
            assert config.agent_presets["planner"]["fast"].provider == "planner"

            role.value = "reviewer"
            await pilot.pause()
            preset_selector = screen.query_one("#agent-preset-select", Select)
            assert "fast" not in {option[1] for option in preset_selector._options}
            screen.query_one("#agent-preset-name", Input).value = "focused"
            screen.query_one("#agent-preset-provider", Select).value = "reviewer"
            screen.query_one("#create-agent-preset", Button).press()
            await pilot.pause()
            assert config.agent_presets["reviewer"]["focused"].provider == "reviewer"
            screen.query_one("#agent-preset-name", Input).value = "focused-review"
            screen.query_one("#rename-agent-preset", Button).press()
            await pilot.pause()
            assert "focused-review" in config.agent_presets["reviewer"]
            screen.query_one("#delete-agent-preset", Button).press()
            await pilot.pause()
            assert "reviewer" not in config.agent_presets
            assert "fast" in config.agent_presets["planner"]

    asyncio.run(drive())


def test_agent_presets_role_selector_shows_all_roles_and_supports_keyboard_and_mouse(
    tmp_path: Path,
) -> None:
    controller, _, _ = _controller(tmp_path)
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test(size=(80, 24)) as pilot:
            await pilot.press("p")
            await pilot.pause()
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
            await pilot.pause()
            assert role.value == "implementer"
            assert "Implementer presets loaded" in str(
                screen.query_one("#agent-presets-status", Static).renderable
            )

            await pilot.click("#agent-preset-role")
            await pilot.pause()
            overlay = role.query_one("SelectOverlay")
            assert role.expanded is True
            # The menu includes a top and bottom border plus all three rows.
            assert overlay.region.height >= len(role._options) + 2

            # The third option is visible and selectable directly by mouse.
            await pilot.click(overlay, offset=(2, 3))
            await pilot.pause()
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
            await pilot.pause()
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
            await pilot.pause()
            screen = application.screen
            screen.query_one("#planner-configure", Button).press()
            screen.query_one("#planner-preset", Select).value = "retired"
            await pilot.pause()
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
            await pilot.pause()
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
            await pilot.pause()
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
    controller.configured_agent_providers = (  # type: ignore[method-assign]
        lambda repository: list(capabilities)
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
            await pilot.pause(delay=0.1)
            screen = application.screen
            screen.query_one("#task-provider", Select).value = "trello"
            await pilot.pause(delay=0.1)
            screen.query_one("#repository", Select).value = "other"
            await pilot.pause(delay=0.1)
            assert controller.selected_repository == "other"
            assert task_loads == [
                ("example", "github"),
                ("example", "trello"),
                ("other", "github"),
            ]
            screen.query_one("#workflow-agent-instructions", Button).press()
            # The pushed screen's nested Select mounts asynchronously. Wait
            # for its retained repository selection to finish restoring.
            await pilot.pause(delay=0.1)
            assert isinstance(application.screen, AgentInstructionsScreen)
            assert (
                application.screen.query_one(
                    "#agent-instructions-repository", OrchestratorSelect
                ).value
                == "other"
            )

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
        async with application.run_test() as pilot:
            await pilot.press("i")
            assert isinstance(application.screen, AgentInstructionsScreen)
            screen = application.screen
            repository = screen.query_one("#agent-instructions-repository", OrchestratorSelect)
            repository.value = "example"
            await pilot.pause()
            assert "No IMPLEMENTER.md yet" in str(
                screen.query_one("#implementer-instructions-status", Static).renderable
            )
            assert not (tmp_path / "IMPLEMENTER.md").exists()
            assert not (tmp_path / "REVIEWER.md").exists()

            screen.query_one("#implementer-instructions", TextArea).text = "Use focused tests."
            screen.query_one("#save-implementer-instructions", Button).press()
            await pilot.pause()
            assert (tmp_path / "IMPLEMENTER.md").read_text(encoding="utf-8") == "Use focused tests."
            assert not (tmp_path / "REVIEWER.md").exists()

            screen.query_one("#reviewer-instructions", TextArea).text = "Check retries."
            screen.query_one("#save-reviewer-instructions", Button).press()
            await pilot.pause()
            assert (tmp_path / "REVIEWER.md").read_text(encoding="utf-8") == "Check retries."
            assert "Git was not changed" in str(
                screen.query_one("#agent-instructions-status", Static).renderable
            )

            repository.value = "other"
            await pilot.pause()
            assert screen.query_one("#implementer-instructions", TextArea).text == ""
            assert "No REVIEWER.md yet" in str(
                screen.query_one("#reviewer-instructions-status", Static).renderable
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
            payload={"instructions": "implement the requested change"},
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
            await pilot.pause()
            screen = application.screen
            copy_button = screen.query_one("#copy-detailed-logs-button", Button)
            assert not copy_button.disabled
            copy_button.press()
            await pilot.pause()

    asyncio.run(drive())

    assert copied == [format_diagnostics_for_export(repository.diagnostics)]
    assert "=== REQUEST ===" in copied[0]
    assert "=== RESPONSE ===" in copied[0]
    assert "response line 399" in copied[0]


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
            await pilot.pause()
            application.screen.query_one("#copy-detailed-logs-button", Button).press()
            await pilot.pause()

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


def test_execution_history_and_detail_display_cancelled_status(tmp_path: Path) -> None:
    controller, _, repository = _controller(tmp_path)
    repository.execution.status = WorkflowStatus.CANCELLED
    application = OrchestratorApp(controller)

    async def drive() -> None:
        async with application.run_test() as pilot:
            application.push_screen(ExecutionHistoryScreen())
            await pilot.pause()
            picker = application.screen.query_one("#execution-picker", OrchestratorSelect)
            assert "CANCELLED" in str(picker._options)

            application.push_screen(ExecutionDetailScreen(repository.execution.id))
            await pilot.pause()
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
            await pilot.pause()
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
            await pilot.pause()
            assert isinstance(application.screen, StopWorkflowConfirmation)
            application.screen.query_one("#confirm-stop-workflow", Button).press()
            await pilot.pause()
            assert repository.cancellation_requests == [repository.execution.id]
            assert "Cancellation requested" in str(
                screen.query_one("#workflow-progress", Static).renderable
            )

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
            detail_content = application.screen.query_one("#execution-content", Static)
            assert detail.max_scroll_y > 0
            assert detail_content.size.height > 20
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
