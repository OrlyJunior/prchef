"""Detailed persisted workflow inspection screen."""

from __future__ import annotations

import json
import webbrowser
from dataclasses import asdict
from typing import TYPE_CHECKING, cast

from textual.app import ComposeResult
from textual.containers import Container, Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Static

from orchestrator.models.workflow_execution import WorkflowExecution
from orchestrator.models.workflow_step import WorkflowStep
from orchestrator.tui.bridge import WorkflowController

if TYPE_CHECKING:
    from orchestrator.tui.app import OrchestratorApp


class ExecutionDetailScreen(Screen[None]):
    """Render all persisted execution snapshots without re-running workflow logic."""

    BINDINGS = [("escape", "app.pop_screen", "Back")]

    def __init__(self, execution_id: str) -> None:
        super().__init__()
        self.execution_id = execution_id
        self._links: dict[str, str] = {}

    def compose(self) -> ComposeResult:
        with Container(id="execution-detail"):
            yield Static("Execution detail", classes="screen-title")
            with Horizontal(classes="screen-actions"):
                yield Button("Open task", id="open-task")
                yield Button("Open PR", id="open-pr")
                yield Button("Open planner session", id="open-spec-session")
                yield Button("Open review session", id="open-review-session")
            with VerticalScroll():
                yield Static("Loading execution…", id="execution-content")

    @property
    def controller(self) -> WorkflowController:
        return cast("OrchestratorApp", self.app).controller

    def on_mount(self) -> None:
        try:
            execution, steps = self.controller.execution_detail(self.execution_id)
        except Exception as exc:
            self.query_one("#execution-content", Static).update(
                f"Could not load execution {self.execution_id}: {exc}"
            )
            return
        self._render_execution(execution, steps)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        url = self._links.get(event.button.id or "")
        if not url:
            self.notify("No related link was persisted for this execution.", severity="warning")
            return
        webbrowser.open(url)
        self.notify("Opened related resource in the default browser.")

    def _render_execution(self, execution: WorkflowExecution, steps: list[WorkflowStep]) -> None:
        self._links = {
            "open-task": execution.task_url,
            "open-pr": execution.pr_url or "",
            "open-spec-session": execution.spec_jules_session_url or "",
            "open-review-session": execution.review_jules_session_url or "",
        }
        for button_id, url in self._links.items():
            self.query_one(f"#{button_id}", Button).disabled = not bool(url)

        details = {
            "execution": asdict(execution),
            "steps": [asdict(step) for step in steps],
            "links": {
                key.removeprefix("open-"): value for key, value in self._links.items() if value
            },
        }
        rendered = json.dumps(details, ensure_ascii=False, indent=2, default=str)
        self.query_one("#execution-content", Static).update(rendered)
