"""Workflow launch and live-progress screen."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from textual import work
from textual.app import ComposeResult
from textual.containers import Container, Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Button, Input, Select, Static

from orchestrator.models.agent import AgentRole
from orchestrator.models.development_task import DevelopmentTask
from orchestrator.models.workflow_event import WorkflowEvent
from orchestrator.models.workflow_execution import WorkflowExecution
from orchestrator.tui.bridge import WorkflowController, WorkflowLaunch
from orchestrator.tui.screens.task_selector import TaskSelector

if TYPE_CHECKING:
    from orchestrator.tui.app import OrchestratorApp


class WorkflowRunnerScreen(Screen[None]):
    """Collect launch selections and observe the existing workflow service."""

    BINDINGS = [("escape", "app.pop_screen", "Back")]

    def __init__(self) -> None:
        super().__init__()
        self._event_lines: list[str] = []

    def compose(self) -> ComposeResult:
        with Container(id="workflow-runner"):
            yield Static("Start workflow", classes="screen-title")
            with Vertical(classes="workflow-form"):
                yield Static("Repository", classes="field-label")
                yield Select([], prompt="Configured repository", id="repository")
                yield Static("Task provider", classes="field-label")
                yield Select([], prompt="Task provider", id="task-provider")
                yield TaskSelector(id="task-selector")
                yield Static("Planner", classes="field-label")
                yield Select([], prompt="Planner provider", id="planner-provider")
                yield Static("Implementer", classes="field-label")
                yield Select([], prompt="Implementer provider", id="implementer-provider")
                yield Static("Reviewer", classes="field-label")
                yield Select([], prompt="Reviewer provider", id="reviewer-provider")
                with Horizontal(classes="screen-actions"):
                    yield Button("Start workflow", variant="success", id="start-workflow")
                    yield Button("Execution history", id="workflow-history")
            yield Static("No workflow running.", id="workflow-progress")
            yield Static("", id="workflow-events")

    @property
    def controller(self) -> WorkflowController:
        return cast("OrchestratorApp", self.app).controller

    def on_mount(self) -> None:
        try:
            repositories = self.controller.repositories()
        except Exception as exc:
            self.query_one("#workflow-progress", Static).update(
                f"Cannot load configured repositories: {exc}"
            )
            return
        options = [(f"{item.key} — {item.path}", item.key) for item in repositories]
        self.query_one("#repository", Select).set_options(options)
        if not repositories:
            self.query_one("#workflow-progress", Static).update(
                "No repositories are configured. Add entries under repositories "
                "in orchestrator.yml."
            )

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "repository" and isinstance(event.value, str):
            self._load_repository_options(event.value)

    def _load_repository_options(self, repository: str) -> None:
        try:
            providers = self.controller.task_providers(repository)
            configured_agents = self.controller.configured_agent_providers(repository)
            selected_agents = self.controller.agent_providers(repository)
        except Exception as exc:
            self.notify(f"Could not resolve repository configuration: {exc}", severity="error")
            return

        self.query_one("#task-provider", Select).set_options(
            [(provider, provider) for provider in providers]
        )
        self.query_one("#task-provider", Select).value = providers[0]
        self.query_one("#task-selector", TaskSelector).clear_tasks()
        for role, widget_id in (
            (AgentRole.PLANNER, "#planner-provider"),
            (AgentRole.IMPLEMENTER, "#implementer-provider"),
            (AgentRole.REVIEWER, "#reviewer-provider"),
        ):
            select = self.query_one(widget_id, Select)
            select.set_options([(provider, provider) for provider in configured_agents])
            select.value = selected_agents[role]

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "load-tasks":
            repository = self._selected("#repository")
            provider = self._selected("#task-provider")
            if repository is None or provider is None:
                self.notify("Choose a repository and task provider first.", severity="warning")
                return
            self._load_tasks(repository, provider)
        elif event.button.id == "start-workflow":
            self._start_selected_workflow()
        elif event.button.id == "workflow-history":
            self._show_history()

    def _show_history(self) -> None:
        from orchestrator.tui.screens.execution_history import ExecutionHistoryScreen

        self.app.push_screen(ExecutionHistoryScreen())

    @work(thread=True, exclusive=True)
    def _load_tasks(self, repository: str, provider: str) -> None:
        try:
            tasks = self.controller.list_tasks(repository=repository, task_provider=provider)
        except Exception as exc:
            self.app.call_from_thread(self._task_loading_failed, str(exc))
            return
        self.app.call_from_thread(self._set_available_tasks, tasks, provider)

    def _set_available_tasks(self, tasks: list[DevelopmentTask], provider: str) -> None:
        self.query_one("#task-selector", TaskSelector).set_tasks(tasks, provider=provider)

    def _task_loading_failed(self, message: str) -> None:
        self.notify(f"Could not load tasks: {message}", severity="error")

    def _start_selected_workflow(self) -> None:
        repository = self._selected("#repository")
        task_provider = self._selected("#task-provider")
        task_id = self.query_one("#task-id", Input).value
        provider_selections = {
            AgentRole.PLANNER: self._selected("#planner-provider"),
            AgentRole.IMPLEMENTER: self._selected("#implementer-provider"),
            AgentRole.REVIEWER: self._selected("#reviewer-provider"),
        }
        if repository is None or task_provider is None or not task_id.strip():
            self.notify(
                "Select a repository, provider, and task before starting.", severity="warning"
            )
            return
        if any(value is None for value in provider_selections.values()):
            self.notify("Select an agent provider for every workflow role.", severity="warning")
            return
        launch = WorkflowLaunch(
            repository=repository,
            task_provider=task_provider,
            task_id=task_id,
            agent_providers={
                role: value for role, value in provider_selections.items() if value is not None
            },
        )
        self.query_one("#start-workflow", Button).disabled = True
        self.query_one("#workflow-progress", Static).update("Starting workflow…")
        self._event_lines.clear()
        self.query_one("#workflow-events", Static).update("")
        self._run_workflow(launch)

    @work(thread=True, exclusive=True)
    def _run_workflow(self, launch: WorkflowLaunch) -> None:
        try:
            execution = self.controller.start_workflow(
                launch, event_listener=self._on_workflow_event
            )
        except Exception as exc:
            self.app.call_from_thread(self._workflow_failed, str(exc))
            return
        self.app.call_from_thread(self._workflow_finished, execution)

    def _on_workflow_event(self, event: WorkflowEvent) -> None:
        self.app.call_from_thread(self._render_workflow_event, event)

    def _render_workflow_event(self, event: WorkflowEvent) -> None:
        stage = event.stage.value.replace("_", " ").title() if event.stage else ""
        message = event.message or event.status or event.step_status or ""
        self.query_one("#workflow-progress", Static).update(
            f"Execution {event.execution_id}: {stage or event.kind.value} {message}".strip()
        )
        self._event_lines.append(f"{event.kind.value}: {stage or message}")
        self.query_one("#workflow-events", Static).update("\n".join(self._event_lines[-8:]))

    def _workflow_finished(self, execution: WorkflowExecution) -> None:
        self.query_one("#start-workflow", Button).disabled = False
        self.query_one("#workflow-progress", Static).update(
            f"Finished {execution.id}: {execution.status.value} ({execution.current_stage.value})"
        )
        self.notify(f"Workflow finished: {execution.status.value}")

    def _workflow_failed(self, message: str) -> None:
        self.query_one("#start-workflow", Button).disabled = False
        self.query_one("#workflow-progress", Static).update(f"Could not start workflow: {message}")
        self.notify(f"Could not start workflow: {message}", severity="error")

    def _selected(self, selector: str) -> str | None:
        value = self.query_one(selector, Select).value
        return value if isinstance(value, str) else None
