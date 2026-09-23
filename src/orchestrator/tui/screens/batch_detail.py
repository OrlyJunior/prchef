"""View of an ordered, persisted workflow batch with per-item actions.

Every batch item renders as an expandable row using the same
``BatchProgressView`` projection the active launcher uses, so a persisted or
restarted batch reads identically to one still running. Unlike the live
view, statuses here are read once (or on an explicit refresh): a persisted
``PENDING``/``WAITING`` item does not imply the original in-memory provider
queue resumed, since provider concurrency state is process-local and does not
survive a restart.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Static

from orchestrator.models.execution_diagnostic import ExecutionDiagnostic
from orchestrator.models.workflow_batch import WorkflowBatch
from orchestrator.tui.batch_progress import build_persisted_rows
from orchestrator.tui.bridge import WorkflowController
from orchestrator.tui.diagnostics import format_diagnostics_for_export
from orchestrator.tui.widgets import BatchItemRow, BatchProgressView

if TYPE_CHECKING:
    from orchestrator.tui.app import OrchestratorApp


class BatchDetailScreen(Screen[None]):
    BINDINGS = [("escape", "app.pop_screen", "Back"), ("r", "refresh", "Refresh")]

    def __init__(self, batch_id: str) -> None:
        super().__init__()
        self.batch_id = batch_id
        self._batch: WorkflowBatch | None = None
        self._diagnostics_cache: dict[int, list[ExecutionDiagnostic]] = {}

    @property
    def controller(self) -> WorkflowController:
        return cast("OrchestratorApp", self.app).controller

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="batch-detail"):
            yield Static("Workflow batch", classes="screen-title")
            yield Static("", id="batch-summary")
            with Horizontal(classes="screen-actions"):
                yield Button("Refresh", id="refresh-batch")
            yield BatchProgressView(id="batch-progress")

    def on_mount(self) -> None:
        self.load_batch()

    def action_refresh(self) -> None:
        self.load_batch()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "refresh-batch":
            self.load_batch()

    def load_batch(self) -> None:
        self._diagnostics_cache.clear()
        try:
            batch = self.controller.batch_detail(self.batch_id)
        except Exception as exc:
            self.query_one("#batch-summary", Static).update(f"Could not load batch: {exc}")
            return
        self._batch = batch
        self.query_one("#batch-summary", Static).update(
            f"{batch.created_at.isoformat()} | {batch.repository} | "
            f"{batch.task_source} | {len(batch.items)} task(s)"
        )
        view = self.query_one("#batch-progress", BatchProgressView)
        if not batch.items:
            view.set_rows([])
            view.set_note("This batch has no tasks.")
            return
        view.set_note("")
        view.set_rows(build_persisted_rows(batch, self.controller))

    def on_batch_item_row_show_logs_requested(
        self, message: BatchItemRow.ShowLogsRequested
    ) -> None:
        view = self.query_one("#batch-progress", BatchProgressView)
        row = view.row(message.position)
        if row is None:
            return
        content = format_diagnostics_for_export(self._diagnostics(message.position))
        row.set_detailed_logs(content or "No detailed logs are available.")

    def on_batch_item_row_copy_logs_requested(
        self, message: BatchItemRow.CopyLogsRequested
    ) -> None:
        content = format_diagnostics_for_export(self._diagnostics(message.position))
        if not content:
            self.notify("No detailed logs are available to copy.", severity="warning")
            return
        try:
            self.app.copy_to_clipboard(content)
        except Exception as exc:
            self.notify(f"Could not copy detailed logs: {exc}", severity="error")
            return
        self.notify("Detailed logs copied to clipboard.")

    def _diagnostics(self, position: int) -> list[ExecutionDiagnostic]:
        if position in self._diagnostics_cache:
            return self._diagnostics_cache[position]
        execution_id = self._execution_id(position)
        if execution_id is None:
            return []
        try:
            diagnostics = self.controller.execution_diagnostics(execution_id)
        except Exception as exc:
            self.notify(f"Could not load detailed logs: {exc}", severity="error")
            return []
        self._diagnostics_cache[position] = diagnostics
        return diagnostics

    def on_batch_item_row_open_execution_requested(
        self, message: BatchItemRow.OpenExecutionRequested
    ) -> None:
        execution_id = self._execution_id(message.position)
        if execution_id is None:
            self.notify("That batch task has no execution to inspect.", severity="warning")
            return
        from orchestrator.tui.screens.execution_detail import ExecutionDetailScreen

        self.app.push_screen(ExecutionDetailScreen(execution_id))

    def on_batch_item_row_action_requested(self, message: BatchItemRow.ActionRequested) -> None:
        self.on_batch_item_row_open_execution_requested(
            BatchItemRow.OpenExecutionRequested(message.position)
        )

    def on_batch_item_row_cancel_requested(self, message: BatchItemRow.CancelRequested) -> None:
        execution_id = self._execution_id(message.position)
        if execution_id is None:
            self.notify("That batch task has no execution to cancel.", severity="warning")
            return
        try:
            accepted = self.controller.cancel_workflow(execution_id)
        except Exception as exc:
            self.notify(f"Could not cancel batch task: {exc}", severity="error")
            return
        if not accepted:
            self.notify("This batch task has already finished.", severity="warning")
        self.load_batch()

    def _execution_id(self, position: int) -> str | None:
        if self._batch is None:
            return None
        for item in self._batch.items:
            if item.position == position:
                return item.execution_id
        return None
