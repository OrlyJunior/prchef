"""Top-level Textual application for interactive workflow control."""

from __future__ import annotations

from typing import Any

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Grid
from textual.widgets import Button, Footer, Header, Static

from orchestrator.tui.bridge import WorkflowController
from orchestrator.tui.screens.agent_instructions import AgentInstructionsScreen
from orchestrator.tui.screens.agent_presets import AgentPresetsScreen
from orchestrator.tui.screens.execution_history import ExecutionHistoryScreen
from orchestrator.tui.screens.github_app_settings import GitHubAppSettingsScreen
from orchestrator.tui.screens.workflow_runner import WorkflowRunnerScreen


class OrchestratorApp(App[None]):
    """First-class terminal presentation over the existing orchestration core."""

    TITLE = "Orchestrator"
    SUB_TITLE = "Interactive workflow control"
    CSS = """
    #home, #workflow-runner, #execution-history, #execution-detail,
    #github-app-settings, #agent-instructions, #agent-presets {
        width: 100%;
        height: 1fr;
        padding: 1 2;
    }
    #workflow-runner, #execution-history, #execution-detail,
    #github-app-settings, #agent-instructions, #agent-presets {
        overflow-y: auto;
    }
    .screen-title { text-style: bold; margin-bottom: 1; }
    .field-label { margin-top: 1; }
    .workflow-form { max-width: 100; height: auto; }
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
    .github-app-roles { width: 100%; }
    .github-app-role { width: 1fr; height: auto; margin-right: 1; }
    .screen-actions { margin-top: 1; height: auto; }
    .screen-actions Button { margin-right: 1; }
    #home-actions {
        grid-size: 3;
        grid-gutter: 1 0;
    }
    #home-actions Button { width: 1fr; margin-right: 0; }
    #workflow-progress { margin-top: 2; text-style: bold; }
    #workflow-events { margin-top: 1; color: $text-muted; }
    .workflow-log-actions { height: auto; margin-top: 1; }
    .workflow-log-actions Button { width: auto; }
    #workflow-raw-events { display: none; margin-top: 1; color: $text-muted; }
    #human-input-panel {
        display: none;
        height: auto;
        margin-top: 1;
        padding: 1;
        border: round $warning;
    }
    #human-input-prompt { margin-bottom: 1; }
    #human-response { height: 7; }
    #execution-human-title,
    #execution-human-prompt,
    #execution-human-response,
    #execution-human-actions {
        display: none;
    }
    #execution-human-response { height: 7; }
    #execution-content { width: 100%; }
    """
    BINDINGS = [
        Binding("n", "new_workflow", "New workflow"),
        Binding("h", "history", "History"),
        Binding("c", "github_apps", "GitHub Apps"),
        Binding("i", "agent_instructions", "Agent instructions"),
        Binding("p", "agent_presets", "Agent presets"),
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
            with Grid(classes="screen-actions", id="home-actions"):
                yield Button("Start workflow", variant="success", id="new-workflow")
                yield Button("Execution history", id="history")
                yield Button("Agent presets", id="agent-presets")
                yield Button("GitHub Apps", id="github-apps")
                # This is a navigation control, not the editor's content
                # container. Keeping their IDs distinct prevents the editor
                # layout rules from making this button fill the home screen.
                yield Button("Agent instructions", id="agent-instructions-button")
        yield Footer()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "new-workflow":
            self.action_new_workflow()
        elif event.button.id == "history":
            self.action_history()
        elif event.button.id == "agent-presets":
            self.action_agent_presets()
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

    def action_agent_instructions(self) -> None:
        self.push_screen(AgentInstructionsScreen())
