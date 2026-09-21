"""Read-only view of an ordered, persisted workflow batch."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Static

from orchestrator.tui.bridge import WorkflowController
from orchestrator.tui.screens.execution_history import execution_status_label
from orchestrator.tui.widgets import OrchestratorSelect

if TYPE_CHECKING:
    from orchestrator.tui.app import OrchestratorApp


class BatchDetailScreen(Screen[None]):
    BINDINGS = [("escape", "app.pop_screen", "Back")]

    def __init__(self, batch_id: str) -> None:
        super().__init__()
        self.batch_id = batch_id

    @property
    def controller(self) -> WorkflowController:
        return cast("OrchestratorApp", self.app).controller

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="batch-detail"):
            yield Static("Workflow batch", classes="screen-title")
            yield Static("", id="batch-summary")
            yield OrchestratorSelect([], prompt="Select a batch task", id="batch-item-picker")
            with Horizontal(classes="screen-actions"):
                yield Button("Inspect execution", variant="primary", id="inspect-batch-item")

    def on_mount(self) -> None:
        try:
            batch = self.controller.batch_detail(self.batch_id)
        except Exception as exc:
            self.query_one("#batch-summary", Static).update(f"Could not load batch: {exc}")
            return
        options: list[tuple[str, str]] = []
        for item in batch.items:
            status = "NOT STARTED"
            if item.execution_id is not None:
                try:
                    execution, _ = self.controller.execution_detail(item.execution_id)
                    status = execution_status_label(execution)
                except Exception:
                    status = "EXECUTION UNAVAILABLE"
                options.append(
                    (
                        f"{item.position + 1}. {status} | {item.task_id}: {item.task_title}",
                        item.execution_id,
                    )
                )
            else:
                options.append(
                    (f"{item.position + 1}. {status} | {item.task_id}: {item.task_title}", "")
                )
        self.query_one("#batch-item-picker", OrchestratorSelect).set_options(options)
        self.query_one("#batch-summary", Static).update(
            f"{batch.created_at.isoformat()} | {batch.repository} | "
            f"{batch.task_source} | {len(batch.items)} task(s)"
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "inspect-batch-item":
            return
        execution_id = self.query_one("#batch-item-picker", OrchestratorSelect).value
        if not isinstance(execution_id, str) or not execution_id:
            self.notify("That batch task has no execution to inspect.", severity="warning")
            return
        from orchestrator.tui.screens.execution_detail import ExecutionDetailScreen

        self.app.push_screen(ExecutionDetailScreen(execution_id))
