"""Reusable task-picker widget for the workflow runner."""

from __future__ import annotations

from collections.abc import Iterable

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Button, Select, Static

from orchestrator.models.development_task import DevelopmentTask


class TaskSelector(Vertical):
    """Browse available tasks while retaining their provider identifiers internally."""

    selected_task_id: str | None = None

    def compose(self) -> ComposeResult:
        yield Static("Task", classes="field-label")
        yield Button("Refresh tasks", id="refresh-tasks")
        yield Select([], prompt="Available tasks", id="available-tasks")
        yield Static("Choose a provider and load tasks to browse it.", id="task-hint")
        yield Static("", id="task-error")

    def set_tasks(self, tasks: Iterable[DevelopmentTask], *, provider: str) -> None:
        task_list = list(tasks)
        self.selected_task_id = None
        options = [(f"{task.id}: {task.title or '(untitled)'}", task.id) for task in task_list]
        select = self.query_one("#available-tasks", Select)
        select.disabled = False
        select.set_options(options)
        self.query_one("#refresh-tasks", Button).disabled = False
        self.query_one("#task-error", Static).update("")
        hint = self.query_one("#task-hint", Static)
        if task_list:
            hint.update(f"{len(task_list)} open {provider} tasks available.")
        else:
            hint.update(f"No open {provider} tasks were returned.")

    def clear_tasks(self) -> None:
        self.selected_task_id = None
        self.query_one("#available-tasks", Select).set_options([])
        self.query_one("#available-tasks", Select).disabled = False
        self.query_one("#refresh-tasks", Button).disabled = False
        self.query_one("#task-error", Static).update("")
        self.query_one("#task-hint", Static).update(
            "Choose a provider and load tasks to browse it."
        )

    def set_tasks_loading(self, *, provider: str) -> None:
        """Render an inline busy state while the runner fetches tasks."""

        self.selected_task_id = None
        select = self.query_one("#available-tasks", Select)
        select.set_options([])
        select.disabled = True
        self.query_one("#refresh-tasks", Button).disabled = True
        self.query_one("#task-error", Static).update("")
        self.query_one("#task-hint", Static).update(f"Loading available {provider} tasks...")

    def set_error(self, message: str) -> None:
        """Display a task-loading failure without interrupting the launcher."""

        self.selected_task_id = None
        self.query_one("#available-tasks", Select).disabled = False
        self.query_one("#refresh-tasks", Button).disabled = False
        self.query_one("#task-hint", Static).update("Tasks could not be loaded. Try refreshing.")
        self.query_one("#task-error", Static).update(f"Could not load tasks: {message}")

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id != "available-tasks" or not isinstance(event.value, str):
            return
        self.selected_task_id = event.value
