"""Top-level Textual application for interactive workflow control."""

from __future__ import annotations

from typing import Any

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import Resize
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Header, Static

from orchestrator.models.workflow_execution import WorkflowExecution
from orchestrator.tui.bridge import WorkflowController
from orchestrator.tui.screens.agent_instructions import AgentInstructionsScreen
from orchestrator.tui.screens.agent_presets import AgentPresetsScreen
from orchestrator.tui.screens.execution_history import ExecutionHistoryScreen
from orchestrator.tui.screens.github_app_settings import GitHubAppSettingsScreen
from orchestrator.tui.screens.provider_settings import ProviderSettingsScreen
from orchestrator.tui.screens.repository_settings import RepositorySettingsScreen
from orchestrator.tui.screens.workflow_runner import WorkflowRunnerScreen


class QuitWithWorkConfirmation(ModalScreen[bool]):
    CSS = """
    QuitWithWorkConfirmation { align: center middle; }
    #quit-work-dialog { width: 100%; max-width: 72; height: auto;
                        max-height: 80%; padding: 1 2; border: round $warning; }
    #quit-work-actions { height: auto; }
    """

    def __init__(self, count: int) -> None:
        super().__init__()
        self.count = count

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="quit-work-dialog"):
            yield Static(f"Quit with {self.count} active execution(s)?", classes="screen-title")
            yield Static(
                "They will be interrupted and can be resumed when the app is reopened."
            )
            with Horizontal(id="quit-work-actions"):
                yield Button("Quit and interrupt", variant="warning", id="quit-confirm")
                yield Button("Keep running", id="quit-dismiss")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "quit-confirm")


class InterruptedRecoveryChoice(ModalScreen[tuple[str, ...] | None]):
    CSS = """
    InterruptedRecoveryChoice { align: center middle; }
    #recovery-dialog { width: 100%; max-width: 82; height: 80%;
                       padding: 1 2; border: round $warning; }
    #recovery-items { height: 1fr; overflow-y: auto; }
    #recovery-actions { height: auto; }
    """

    def __init__(self, executions: list[WorkflowExecution]) -> None:
        super().__init__()
        self.executions = executions
        self.selected: set[str] = set()

    def compose(self) -> ComposeResult:
        with Vertical(id="recovery-dialog"):
            yield Static(
                f"{len(self.executions)} execution(s) were interrupted", classes="screen-title"
            )
            yield Static("Resume all, select executions, or leave them for later.")
            with VerticalScroll(id="recovery-items"):
                for index, execution in enumerate(self.executions):
                    yield Button(
                        f"[ ] {execution.task_source}:{execution.task_id} | "
                        f"resume { (execution.resume_stage or execution.current_stage).value}",
                        id=f"recovery-item-{index}",
                    )
            with Horizontal(id="recovery-actions"):
                yield Button("Resume all", variant="primary", id="recovery-all")
                yield Button("Resume selected", id="recovery-selected")
                yield Button("Later", id="recovery-later")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id == "recovery-all":
            self.dismiss(tuple(item.id for item in self.executions))
        elif button_id == "recovery-selected":
            self.dismiss(tuple(item.id for item in self.executions if item.id in self.selected))
        elif button_id == "recovery-later":
            self.dismiss(None)
        elif button_id.startswith("recovery-item-"):
            item = self.executions[int(button_id.removeprefix("recovery-item-"))]
            if item.id in self.selected:
                self.selected.remove(item.id)
                mark = "[ ]"
            else:
                self.selected.add(item.id)
                mark = "[x]"
            event.button.label = f"{mark} {item.task_source}:{item.task_id} | " \
                                 f"resume {(item.resume_stage or item.current_stage).value}"


class OrchestratorApp(App[None]):
    """First-class terminal presentation over the existing orchestration core."""

    TITLE = "Orchestrator"
    SUB_TITLE = "Interactive workflow control"
    CSS = """
    #home, #workflow-runner, #execution-history, #execution-detail, #batch-detail,
    #github-app-settings, #agent-instructions, #repository-settings {
        width: 100%;
        height: 1fr;
        padding: 1 2;
    }
    #workflow-runner, #execution-history, #execution-detail, #batch-detail,
    #github-app-settings, #agent-instructions, #repository-settings {
        overflow-y: auto;
    }
    .screen-title { text-style: bold; margin-bottom: 1; }
    .field-label { margin-top: 1; }
    .workflow-form { max-width: 100; height: auto; }
    .agent-instructions-editor { height: 7; }
    #runtime-settings-note { margin-top: 1; color: $text-muted; }
    .role-section { height: auto; margin-top: 1; padding: 0 1 1 1; border: round $primary; }
    .role-title { text-style: bold; }
    .role-summary-row { height: auto; align: center middle; }
    .role-summary-row Static { width: 1fr; color: $text-muted; }
    .role-summary-row Button { width: auto; }
    .role-preset-select { width: 32; margin-right: 1; }
    .role-config { height: auto; display: none; margin-top: 1; }
    .role-config-row { height: auto; width: 100%; }
    .role-field { width: 1fr; height: auto; margin-right: 1; }
    .role-field:last-of-type { margin-right: 0; }
    .role-advanced-action { align: left bottom; }
    .role-advanced-action Button { width: auto; }
    .role-advanced { height: auto; display: none; margin-top: 1; }
    .default-indicator { color: $text-muted; }
    WorkflowRunnerScreen.-narrow .role-summary-row { layout: vertical; }
    WorkflowRunnerScreen.-narrow .role-summary-row Static,
    WorkflowRunnerScreen.-narrow .role-preset-select { width: 100%; margin-right: 0; }
    WorkflowRunnerScreen.-narrow .role-config-row { layout: vertical; }
    WorkflowRunnerScreen.-narrow .role-field { width: 100%; margin-right: 0; }
    #task-selector { height: auto; }
    /* Label filter sits directly above the task table. Its list stays short
       so the table remains reachable on a constrained terminal. */
    #task-label-filter { height: auto; margin-top: 1; display: none; }
    #task-label-filter-bar { width: 100%; height: auto; }
    #task-label-filter-summary { width: 1fr; height: auto; padding: 1 1 0 0; }
    #task-label-options { width: 100%; height: auto; }
    #task-label-list { width: 100%; height: auto; max-height: 8; }
    #task-label-status { height: auto; color: $text-muted; }
    #task-label-status.-error { color: $error; }
    /* The task list reads as a table: a distinct select-all band, a column
       header, then one single-line row per task that clips instead of
       spilling past the terminal width. */
    #task-table {
        width: 100%;
        height: auto;
        margin-top: 1;
        padding: 0 1;
        border: round $primary;
    }
    #available-tasks { width: 100%; height: auto; }
    .task-select-all {
        width: 100%;
        /* Auto height leaves room for the separator the bottom border draws
           between this band and the task rows. */
        height: auto;
        border: none;
        border-bottom: hkey $primary;
        padding: 0;
        background: $boost;
        text-style: bold;
    }
    .task-select-all:focus { background: $accent 40%; }
    #task-table-header {
        width: 100%;
        height: 1;
        color: $text-muted;
        text-style: bold;
    }
    .task-row {
        width: 100%;
        height: 1;
        border: none;
        padding: 0;
        margin-top: 0;
        background: transparent;
    }
    .task-row:focus { background: $accent 40%; }
    .task-row:disabled { color: $text-muted; }
    #task-hint { margin-top: 1; }
    .github-app-roles { width: 100%; height: auto; }
    .github-app-role { width: 100%; height: auto; margin-bottom: 1; }
    .screen-actions { margin-top: 1; height: auto; }
    .screen-actions Button { margin-right: 1; }
    #execution-detail { min-width: 0; overflow-x: hidden; scrollbar-gutter: stable; }
    #execution-status { width: 100%; height: auto; }
    #execution-detail-actions {
        width: 100%;
        min-width: 0;
        height: auto;
        grid-size: 2;
        grid-columns: 1fr 1fr;
        grid-rows: auto;
        grid-gutter: 0 1;
        margin-top: 1;
    }
    #execution-detail-actions Button { width: 100%; min-width: 0; max-width: 100%; }
    #execution-detail-actions.-without-cancel {
        grid-size: 3;
        grid-columns: 1fr 1fr 1fr;
    }
    ExecutionDetailScreen.-medium-actions #execution-detail-actions {
        grid-size: 2;
        grid-columns: 1fr 1fr;
    }
    ExecutionDetailScreen.-medium-actions #execution-detail-actions.-without-cancel
    #copy-detailed-logs-button { column-span: 2; }
    ExecutionDetailScreen.-narrow-actions #execution-detail-actions {
        grid-size: 1;
        grid-columns: 1fr;
    }
    ExecutionDetailScreen.-narrow-actions #execution-detail-actions.-without-cancel
    #copy-detailed-logs-button { column-span: 1; }
    #execution-detail .screen-actions { width: 100%; min-width: 0; }
    #execution-detail .screen-actions Button {
        min-width: 0;
        max-width: 100%;
        margin-right: 0;
    }
    #execution-summary { width: 100%; height: auto; margin-top: 1; }
    #execution-timeline { width: 100%; height: auto; margin-top: 1; }
    #execution-stage { width: 100%; height: auto; }
    #execution-toggle-logs { width: auto; margin-top: 1; }
    #execution-detailed-logs {
        display: none;
        width: 100%;
        height: auto;
        overflow-x: auto;
        overflow-y: hidden;
    }
    .navigation-actions {
        width: 100%;
        height: auto;
        layout: horizontal;
        margin-top: 1;
    }
    .navigation-actions .navigation-control {
        width: auto;
        max-width: 100%;
        min-width: 16;
        height: 3;
        margin: 0 1 0 0;
    }
    .navigation-actions .navigation-control:last-of-type { margin-right: 0; }
    #home.-compact-navigation .navigation-actions,
    WorkflowRunnerScreen.-compact-navigation .navigation-actions {
        layout: vertical;
    }
    #home.-compact-navigation .navigation-actions .navigation-control,
    WorkflowRunnerScreen.-compact-navigation .navigation-actions .navigation-control {
        margin: 0 0 1 0;
    }
    #workflow-progress { margin-top: 2; text-style: bold; }
    #workflow-progress.cancelled, #execution-status.cancelled { color: $warning; }
    #workflow-events { margin-top: 1; color: $text-muted; }
    #workflow-outcome {
        display: none;
        width: 100%;
        height: auto;
        margin-top: 1;
        padding: 1;
        border: round $primary;
    }
    #workflow-outcome.awaiting-user { border: round $accent; }
    #workflow-outcome.completed { border: round $success; }
    #workflow-outcome.failed { border: round $error; }
    #workflow-outcome.cancelled { border: round $warning; }
    #workflow-outcome-title { text-style: bold; }
    #workflow-outcome-message { height: auto; margin-top: 1; }
    #workflow-outcome-actions { height: auto; }
    #workflow-outcome-actions Button { width: auto; max-width: 100%; }
    #batch-progress { margin-top: 1; height: auto; }
    .batch-progress-summary { text-style: bold; }
    #batch-provider-availability { height: auto; margin-top: 1; }
    .batch-provider-line { height: auto; width: 100%; margin-top: 1; }
    .batch-provider-line Static { width: 100%; height: auto; }
    .batch-provider-line Button { width: auto; max-width: 100%; }
    .batch-progress-note { margin-top: 1; color: $text-muted; }
    #batch-progress-rows { height: auto; margin-top: 1; }
    .batch-item-row {
        height: auto;
        margin-top: 1;
        padding: 0 1;
        border: round $primary;
    }
    .batch-item-header {
        width: 100%;
        content-align: left middle;
        text-align: left;
    }
    .batch-item-body { height: auto; margin-top: 1; }
    .batch-item-body Static { color: $text-muted; }
    .batch-item-log-actions { height: auto; margin-top: 1; }
    .batch-item-log-actions Button { width: auto; margin-right: 1; }
    .batch-item-actions { height: auto; }
    .batch-item-body > Button { width: auto; max-width: 100%; margin-top: 1; }
    .workflow-log-actions { height: auto; margin-top: 1; }
    .workflow-log-actions Button { width: auto; }
    #workflow-raw-events-title {
        display: none;
        margin-top: 1;
        text-style: bold;
    }
    #workflow-raw-events {
        display: none;
        width: 100%;
        height: auto;
        margin-top: 1;
        overflow-x: auto;
        overflow-y: hidden;
    }
    #human-input-panel {
        display: none;
        height: auto;
        margin-top: 1;
    }
    #human-input-prompt { margin-bottom: 1; }
    #human-response { height: 7; }
    WorkflowRunnerScreen.-narrow #human-input-panel .screen-actions { layout: vertical; }
    WorkflowRunnerScreen.-narrow #human-input-panel .screen-actions Button {
        width: auto;
        max-width: 100%;
        margin-right: 0;
    }
    /* TextArea defaults to height 1fr, which resolves to zero rows beside the
       auto-height detail content and hides the feedback this action edits. */
    #execution-continuation-feedback { height: 7; }
    #execution-human-title,
    #execution-human-prompt,
    #execution-human-response,
    #execution-human-actions {
        display: none;
    }
    #execution-human-response { height: 7; }
    """
    BINDINGS = [
        Binding("n", "new_workflow", "New workflow"),
        Binding("h", "history", "History"),
        Binding("c", "github_apps", "GitHub Apps"),
        Binding("i", "agent_instructions", "Agent instructions"),
        Binding("p", "agent_presets", "Agent presets"),
        Binding("r", "repositories", "Repositories"),
        Binding("o", "providers", "Providers"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, controller: WorkflowController, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.controller = controller

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with VerticalScroll(id="home"):
            yield Static("Orchestrator workflow control", classes="screen-title")
            yield Static(
                "Browse configured repositories and tasks, select role providers, "
                "start a workflow, or inspect persisted executions."
            )
            with Horizontal(classes="navigation-actions", id="home-actions"):
                yield Button(
                    "Start workflow",
                    variant="success",
                    id="new-workflow",
                    classes="navigation-control",
                )
                yield Button(
                    "Execution history", id="history", classes="navigation-control"
                )
                yield Button(
                    "Agent presets", id="agent-presets", classes="navigation-control"
                )
                yield Button(
                    "Repositories", id="repositories", classes="navigation-control"
                )
                yield Button(
                    "GitHub Apps", id="github-apps", classes="navigation-control"
                )
                yield Button(
                    "Agent instructions",
                    id="agent-instructions-button",
                    classes="navigation-control",
                )
            with Horizontal(classes="navigation-actions"):
                yield Button("Providers", id="providers", classes="navigation-control")
        yield Footer()

    def on_mount(self) -> None:
        self._set_home_layout_mode(self.size.width)
        interrupted = self.controller.interrupted_executions()
        if interrupted:
            self.push_screen(InterruptedRecoveryChoice(interrupted), self._resume_choice)

    def _resume_choice(self, execution_ids: tuple[str, ...] | None) -> None:
        for execution_id in execution_ids or ():
            try:
                self.controller.submit_interrupted(execution_id)
            except Exception as exc:
                self.notify(f"Could not resume {execution_id}: {exc}", severity="error")

    async def action_quit(self) -> None:
        count = self.controller.active_execution_count()
        if count:
            self.push_screen(QuitWithWorkConfirmation(count), self._confirm_quit)
        else:
            self.exit()

    def _confirm_quit(self, confirmed: bool | None) -> None:
        if confirmed:
            self.controller.shutdown_for_exit()
            self.exit()

    def on_resize(self, event: Resize) -> None:
        self._set_home_layout_mode(event.size.width)

    def _set_home_layout_mode(self, width: int) -> None:
        # The home container is unmounted while a destination screen is active.
        # App-level resize events still fire then, so only update it when present.
        for home in self.query("#home"):
            home.set_class(width <= 96, "-compact-navigation")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "new-workflow":
            self.action_new_workflow()
        elif event.button.id == "history":
            self.action_history()
        elif event.button.id == "agent-presets":
            self.action_agent_presets()
        elif event.button.id == "repositories":
            self.action_repositories()
        elif event.button.id == "providers":
            self.action_providers()
        elif event.button.id == "github-apps":
            self.action_github_apps()
        elif event.button.id == "agent-instructions-button":
            self.action_agent_instructions()

    def action_new_workflow(self) -> None:
        self.push_screen(WorkflowRunnerScreen())

    def action_history(self) -> None:
        self.push_screen(ExecutionHistoryScreen())

    def action_github_apps(self) -> None:
        self.push_screen(GitHubAppSettingsScreen())

    def action_agent_presets(self) -> None:
        self.push_screen(AgentPresetsScreen())

    def action_repositories(self) -> None:
        self.push_screen(RepositorySettingsScreen())

    def action_providers(self) -> None:
        self.push_screen(ProviderSettingsScreen())

    def action_agent_instructions(self) -> None:
        self.push_screen(AgentInstructionsScreen())
