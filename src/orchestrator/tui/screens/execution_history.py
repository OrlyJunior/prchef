"""Persisted workflow execution browser."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.events import DescendantFocus
from textual.screen import Screen
from textual.widgets import Button, Input, Static

from orchestrator.models.workflow_execution import WorkflowExecution
from orchestrator.tui.bridge import WorkflowController
from orchestrator.tui.widgets import OrchestratorSelect, is_orchestrator_select_widget

if TYPE_CHECKING:
    from orchestrator.tui.app import OrchestratorApp


class ExecutionHistoryScreen(Screen[None]):
    """Browse and filter executions read from the workflow repository."""

    BINDINGS = [("escape", "app.pop_screen", "Back"), ("r", "refresh", "Refresh")]

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="execution-history"):
            yield Static("Execution history", classes="screen-title")
            yield Input(
                placeholder="Filter by task, repository, stage, or status",
                id="history-filter",
            )
            yield OrchestratorSelect([], prompt="Select an execution", id="execution-picker")
            with Horizontal(classes="screen-actions"):
                yield Button("Inspect", variant="primary", id="inspect-execution")
                yield Button("Refresh", id="refresh-history")
            yield Static("", id="history-summary")

    @property
    def controller(self) -> WorkflowController:
        return cast("OrchestratorApp", self.app).controller

    def on_mount(self) -> None:
        self.refresh_history()

    def on_descendant_focus(self, event: DescendantFocus) -> None:
        if is_orchestrator_select_widget(event.widget):
            return
        self.query_one("#execution-history", VerticalScroll).scroll_to_widget(
            event.widget, animate=False
        )

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "history-filter":
            self.refresh_history(event.value)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "refresh-history":
            self.refresh_history()
        elif event.button.id == "inspect-execution":
            self._inspect_selected()

    def action_refresh(self) -> None:
        self.refresh_history()

    def refresh_history(self, query: str | None = None) -> None:
        filter_value = self.query_one("#history-filter", Input).value if query is None else query
        try:
            executions = self.controller.executions(filter_value)
        except Exception as exc:
            self.query_one("#history-summary", Static).update(
                f"Could not load execution history: {exc}"
            )
            return
        self._set_executions(executions)

    def _set_executions(self, executions: list[WorkflowExecution]) -> None:
        options = [
            (
                " | ".join(
                    [
                        execution.status.value,
                        execution.current_stage.value,
                        f"{execution.task_source}:{execution.task_id}",
                        execution.repository or "repository pending",
                        execution.id,
                    ]
                ),
                execution.id,
            )
            for execution in executions
        ]
        self.query_one("#execution-picker", OrchestratorSelect).set_options(options)
        self.query_one("#history-summary", Static).update(
            f"{len(executions)} persisted workflow execution(s)."
        )

    def _inspect_selected(self) -> None:
        execution_id = self.query_one("#execution-picker", OrchestratorSelect).value
        if not isinstance(execution_id, str):
            self.notify("Select an execution to inspect.", severity="warning")
            return
        from orchestrator.tui.screens.execution_detail import ExecutionDetailScreen

        self.app.push_screen(ExecutionDetailScreen(execution_id))
