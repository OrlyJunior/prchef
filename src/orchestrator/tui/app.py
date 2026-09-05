"""Top-level Textual application for interactive workflow control."""

from __future__ import annotations

from typing import Any

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal
from textual.widgets import Button, Footer, Header, Static

from orchestrator.tui.bridge import WorkflowController
from orchestrator.tui.screens.execution_history import ExecutionHistoryScreen
from orchestrator.tui.screens.workflow_runner import WorkflowRunnerScreen


class OrchestratorApp(App[None]):
    """First-class terminal presentation over the existing orchestration core."""

    TITLE = "Orchestrator"
    SUB_TITLE = "Interactive workflow control"
    CSS = """
    #home, #workflow-runner, #execution-history, #execution-detail {
        width: 100%;
        height: 100%;
        padding: 1 2;
    }
    .screen-title { text-style: bold; margin-bottom: 1; }
    .field-label { margin-top: 1; }
    .workflow-form { max-width: 100; }
    .screen-actions { margin-top: 1; height: auto; }
    .screen-actions Button { margin-right: 1; }
    #workflow-progress { margin-top: 2; text-style: bold; }
    #workflow-events { margin-top: 1; color: $text-muted; }
    #execution-content { width: 100%; }
    """
    BINDINGS = [
        Binding("n", "new_workflow", "New workflow"),
        Binding("h", "history", "History"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, controller: WorkflowController, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.controller = controller

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Container(id="home"):
            yield Static("Orchestrator workflow control", classes="screen-title")
            yield Static(
                "Browse configured repositories and tasks, select role providers, "
                "start a workflow, or inspect persisted executions."
            )
            with Horizontal(classes="screen-actions"):
                yield Button("Start workflow", variant="success", id="new-workflow")
                yield Button("Execution history", id="history")
        yield Footer()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "new-workflow":
            self.action_new_workflow()
        elif event.button.id == "history":
            self.action_history()

    def action_new_workflow(self) -> None:
        self.push_screen(WorkflowRunnerScreen())

    def action_history(self) -> None:
        self.push_screen(ExecutionHistoryScreen())
