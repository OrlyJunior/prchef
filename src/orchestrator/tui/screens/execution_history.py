"""Persisted workflow execution browser."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.events import DescendantFocus
from textual.screen import Screen
from textual.widgets import Button, Input, Static

from orchestrator.models.workflow_batch import WorkflowBatch
from orchestrator.models.workflow_execution import WorkflowExecution
from orchestrator.models.workflow_status import ACTIVE_WORKFLOW_STATUSES, WorkflowStatus
from orchestrator.tui.bridge import WorkflowController
from orchestrator.tui.widgets import OrchestratorSelect, is_orchestrator_select_widget

if TYPE_CHECKING:
    from orchestrator.tui.app import OrchestratorApp


def execution_status_label(execution: WorkflowExecution) -> str:
    """Render the state an operator actually needs to distinguish.

    Queued work, work blocked on shared provider capacity, and genuinely
    running work are all active but mean different things when deciding
    whether to wait or to stop an execution.
    """

    if execution.status is WorkflowStatus.NEEDS_HUMAN:
        if execution.cancellation_requested_at is not None:
            return "CANCELLATION REQUESTED"
        return "AWAITING HUMAN"
    if (
        execution.status in ACTIVE_WORKFLOW_STATUSES
        and execution.cancellation_requested_at is not None
    ):
        return "CANCELLATION REQUESTED"
    if execution.status is WorkflowStatus.PENDING:
        return "QUEUED"
    if execution.status is WorkflowStatus.WAITING:
        return "WAITING FOR AGENT"
    if execution.status is WorkflowStatus.INTERRUPTED:
        return f"INTERRUPTED (resume {(execution.resume_stage or execution.current_stage).value})"
    return execution.status.value


class ExecutionHistoryScreen(Screen[None]):
    """Browse and filter executions read from the workflow repository."""

    BINDINGS = [("escape", "app.pop_screen", "Back"), ("r", "refresh", "Refresh")]

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="execution-history"):
            yield Static("Execution history", classes="screen-title")
            yield Static("Stored batches", classes="field-label")
            yield OrchestratorSelect([], prompt="Select a batch", id="batch-picker")
            with Horizontal(classes="screen-actions"):
                yield Button("Inspect batch", id="inspect-batch")
            yield Static("", id="batch-history-summary")
            yield Static("Individual executions", classes="field-label")
            yield Input(
                placeholder="Filter by task, repository, stage, or status",
                id="history-filter",
            )
            yield OrchestratorSelect([], prompt="Select an execution", id="execution-picker")
            with Horizontal(classes="screen-actions"):
                yield Button("Inspect", variant="primary", id="inspect-execution")
                yield Button("Resume interrupted", id="resume-interrupted")
                yield Button("Cancel interrupted", variant="error", id="cancel-interrupted")
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
            self._refresh_executions(event.value)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "refresh-history":
            self.refresh_history()
        elif event.button.id == "inspect-execution":
            self._inspect_selected()
        elif event.button.id == "resume-interrupted":
            self._act_on_interrupted(resume=True)
        elif event.button.id == "cancel-interrupted":
            self._act_on_interrupted(resume=False)
        elif event.button.id == "inspect-batch":
            self._inspect_batch()

    def action_refresh(self) -> None:
        self.refresh_history()

    def refresh_history(self, query: str | None = None) -> None:
        filter_value = (
            self.query_one("#history-filter", Input).value if query is None else query
        )
        try:
            all_executions = self.controller.executions()
            executions = (
                all_executions
                if not filter_value.strip()
                else self.controller.executions(filter_value)
            )
            batches = self.controller.batches()
        except Exception as exc:
            self.query_one("#history-summary", Static).update(
                f"Could not load execution history: {exc}"
            )
            return
        executions_by_id = {execution.id: execution for execution in all_executions}
        self.query_one("#batch-picker", OrchestratorSelect).set_options(
            [
                (
                    f"{batch.created_at.isoformat()} | {batch.repository} | "
                    f"{len(batch.items)} task(s) | "
                    f"{self._batch_statuses(batch, executions_by_id)}",
                    batch.id,
                )
                for batch in batches
            ]
        )
        self.query_one("#batch-history-summary", Static).update(
            f"{len(batches)} persisted workflow batch(es)."
        )
        self._set_executions(executions)

    def _refresh_executions(self, query: str) -> None:
        """Filter executions without reloading every persisted batch."""

        try:
            executions = self.controller.executions(query)
        except Exception as exc:
            self.query_one("#history-summary", Static).update(
                f"Could not load execution history: {exc}"
            )
            return
        self._set_executions(executions)

    def _batch_statuses(
        self,
        batch: WorkflowBatch,
        executions_by_id: dict[str, WorkflowExecution],
    ) -> str:
        statuses: list[str] = []
        for item in batch.items:
            execution_id = item.execution_id
            if not execution_id:
                statuses.append("NOT STARTED")
                continue
            execution = executions_by_id.get(execution_id)
            statuses.append(
                execution_status_label(execution)
                if execution is not None
                else "UNAVAILABLE"
            )
        return ", ".join(statuses) if statuses else "empty"

    def _set_executions(self, executions: list[WorkflowExecution]) -> None:
        options = [
            (
                " | ".join(
                    [
                        execution_status_label(execution),
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

    def _act_on_interrupted(self, *, resume: bool) -> None:
        execution_id = self.query_one("#execution-picker", OrchestratorSelect).value
        if not isinstance(execution_id, str):
            self.notify("Select an interrupted execution.", severity="warning")
            return
        try:
            execution = next(
                (item for item in self.controller.executions() if item.id == execution_id), None
            )
            if execution is None or execution.status is not WorkflowStatus.INTERRUPTED:
                self.notify("Select an interrupted execution.", severity="warning")
                self.refresh_history()
                return
            if resume:
                self.controller.submit_interrupted(execution_id)
            else:
                if not self.controller.cancel_workflow(execution_id):
                    raise ValueError("Execution is no longer cancellable")
        except Exception as exc:
            self.notify(f"Could not update execution: {exc}", severity="error")
        self.refresh_history()

    def _inspect_batch(self) -> None:
        batch_id = self.query_one("#batch-picker", OrchestratorSelect).value
        if not isinstance(batch_id, str):
            self.notify("Select a batch to inspect.", severity="warning")
            return
        from orchestrator.tui.screens.batch_detail import BatchDetailScreen

        self.app.push_screen(BatchDetailScreen(batch_id))
