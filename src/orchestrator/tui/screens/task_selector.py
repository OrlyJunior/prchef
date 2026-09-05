"""Reusable task-picker widget for the workflow runner."""

from __future__ import annotations

from collections.abc import Iterable

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Button, Input, Select, Static

from orchestrator.models.development_task import DevelopmentTask


class TaskSelector(Vertical):
    """Allow either browsing available tasks or entering a task identifier."""

    def compose(self) -> ComposeResult:
        yield Static("Task", classes="field-label")
        yield Input(placeholder="Task ID (or choose an available task below)", id="task-id")
        yield Button("Load available tasks", id="load-tasks")
        yield Select([], prompt="Available tasks", id="available-tasks")
        yield Static("Choose a provider and load tasks to browse it.", id="task-hint")

    def set_tasks(self, tasks: Iterable[DevelopmentTask], *, provider: str) -> None:
        task_list = list(tasks)
        options = [(f"{task.id}: {task.title or '(untitled)'}", task.id) for task in task_list]
        self.query_one("#available-tasks", Select).set_options(options)
        hint = self.query_one("#task-hint", Static)
        if task_list:
            hint.update(f"{len(task_list)} open {provider} tasks available.")
        elif provider == "trello":
            hint.update("Enter a Trello card ID. No board/list is configured for browsing.")
        else:
            hint.update(f"No open {provider} tasks were returned.")

    def clear_tasks(self) -> None:
        self.query_one("#available-tasks", Select).set_options([])
        self.query_one("#task-hint", Static).update(
            "Choose a provider and load tasks to browse it."
        )

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id != "available-tasks" or not isinstance(event.value, str):
            return
        self.query_one("#task-id", Input).value = event.value
