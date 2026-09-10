"""Top-level Textual application for interactive workflow control."""

from __future__ import annotations

from typing import Any

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal
from textual.events import Resize
from textual.widgets import Button, Footer, Header, Static

from orchestrator.tui.bridge import WorkflowController
from orchestrator.tui.screens.agent_instructions import AgentInstructionsScreen
from orchestrator.tui.screens.agent_presets import AgentPresetsScreen
from orchestrator.tui.screens.execution_history import ExecutionHistoryScreen
from orchestrator.tui.screens.github_app_settings import GitHubAppSettingsScreen
from orchestrator.tui.screens.repository_settings import RepositorySettingsScreen
from orchestrator.tui.screens.workflow_runner import WorkflowRunnerScreen


class OrchestratorApp(App[None]):
    """First-class terminal presentation over the existing orchestration core."""

    TITLE = "Orchestrator"
    SUB_TITLE = "Interactive workflow control"
    CSS = """
    #home, #workflow-runner, #execution-history, #execution-detail,
    #github-app-settings, #agent-instructions, #repository-settings {
        width: 100%;
        height: 1fr;
        padding: 1 2;
    }
    #workflow-runner, #execution-history, #execution-detail,
    #github-app-settings, #agent-instructions, #repository-settings {
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
    #execution-content {
        width: 100%;
        height: auto;
        overflow-x: auto;
        overflow-y: hidden;
    }
    """
    BINDINGS = [
        Binding("n", "new_workflow", "New workflow"),
        Binding("h", "history", "History"),
        Binding("c", "github_apps", "GitHub Apps"),
        Binding("i", "agent_instructions", "Agent instructions"),
        Binding("p", "agent_presets", "Agent presets"),
        Binding("r", "repositories", "Repositories"),
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
        yield Footer()

    def on_mount(self) -> None:
        self._set_home_layout_mode(self.size.width)

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

    def action_agent_instructions(self) -> None:
        self.push_screen(AgentInstructionsScreen())
