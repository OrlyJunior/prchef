"""Reusable multi-task picker for the workflow runner."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import Button, Checkbox, Static

from orchestrator.models.development_task import DevelopmentTask
from orchestrator.models.workflow_execution import WorkflowExecution


class TaskSelector(Vertical):
    """Display open tasks with repository-scoped execution availability."""

    class SelectionChanged(Message):
        def __init__(self, task_ids: tuple[str, ...]) -> None:
            self.task_ids = task_ids
            super().__init__()

    def __init__(
        self,
        *,
        id: str | None = None,
        classes: str | None = None,
        disabled: bool = False,
    ) -> None:
        super().__init__(id=id, classes=classes, disabled=disabled)
        self._tasks: list[DevelopmentTask] = []
        self._executions: dict[str, WorkflowExecution] = {}
        self._checkbox_tasks: dict[str, str] = {}
        self._selected: set[str] = set()
        self._provider = ""
        self._updating = False

    @property
    def selected_task_ids(self) -> tuple[str, ...]:
        return tuple(task.id for task in self._tasks if task.id in self._selected)

    @property
    def selected_task_id(self) -> str | None:
        selected = self.selected_task_ids
        return selected[0] if len(selected) == 1 else None

    @property
    def selected_tasks(self) -> tuple[DevelopmentTask, ...]:
        return tuple(task for task in self._tasks if task.id in self._selected)

    def compose(self) -> ComposeResult:
        yield Static("Tasks", classes="field-label")
        yield Button("Refresh tasks", id="refresh-tasks")
        yield Checkbox("Select all available tasks", id="select-all-tasks", disabled=True)
        # Keep the legacy id while replacing the single-value Select.
        yield Vertical(id="available-tasks")
        yield Static("Choose a provider and load tasks to browse it.", id="task-hint")
        yield Static("", id="task-error")

    def set_tasks(
        self,
        tasks: Iterable[DevelopmentTask],
        *,
        provider: str,
        executions: Mapping[str, WorkflowExecution] | None = None,
    ) -> None:
        previous = set(self._selected)
        self._tasks = list(tasks)
        self._executions = dict(executions or {})
        self._provider = provider
        selectable = {task.id for task in self._tasks if task.id not in self._executions}
        self._selected = previous & selectable
        self._checkbox_tasks.clear()

        task_list = self.query_one("#available-tasks", Vertical)
        task_list.remove_children()
        rows: list[Checkbox] = []
        for position, task in enumerate(self._tasks):
            checkbox_id = f"task-checkbox-{position}"
            self._checkbox_tasks[checkbox_id] = task.id
            execution = self._executions.get(task.id)
            labels = task.metadata.get("labels", [])
            if not isinstance(labels, list):
                labels = []
            label_text = ", ".join(str(label) for label in labels if str(label).strip())
            suffix = f" [{label_text}]" if label_text else ""
            if execution is not None:
                suffix += f" — {execution.status.value} (open in Execution history)"
            rows.append(
                Checkbox(
                    Text(f"{task.id}: {task.title or '(untitled)'}{suffix}"),
                    value=task.id in self._selected,
                    id=checkbox_id,
                    disabled=execution is not None,
                    classes="task-row",
                )
            )
        if rows:
            task_list.mount(*rows)

        header = self.query_one("#select-all-tasks", Checkbox)
        header.disabled = not selectable
        self.query_one("#refresh-tasks", Button).disabled = False
        self.query_one("#task-error", Static).update("")
        self._sync_header_and_hint()
        if not self._tasks:
            self.query_one("#task-hint", Static).update(
                f"No open {provider} tasks were returned."
            )

    def clear_tasks(self) -> None:
        self._tasks = []
        self._executions.clear()
        self._selected.clear()
        self._checkbox_tasks.clear()
        self.query_one("#available-tasks", Vertical).remove_children()
        header = self.query_one("#select-all-tasks", Checkbox)
        self._set_checkbox_value_silently(header, False)
        header.disabled = True
        self.query_one("#refresh-tasks", Button).disabled = False
        self.query_one("#task-error", Static).update("")
        self.query_one("#task-hint", Static).update(
            "Choose a provider and load tasks to browse it."
        )
        self.post_message(self.SelectionChanged(()))

    def set_tasks_loading(self, *, provider: str) -> None:
        """Render an inline busy state while the runner fetches tasks."""

        self.clear_tasks()
        self.query_one("#refresh-tasks", Button).disabled = True
        self.query_one("#task-hint", Static).update(f"Loading available {provider} tasks...")

    def set_error(self, message: str) -> None:
        """Display a task-loading failure without interrupting the launcher."""

        self.clear_tasks()
        self.query_one("#task-hint", Static).update("Tasks could not be loaded. Try refreshing.")
        self.query_one("#task-error", Static).update(f"Could not load tasks: {message}")

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        if self._updating:
            return
        if event.checkbox.id == "select-all-tasks":
            selectable = {task.id for task in self._tasks if task.id not in self._executions}
            self._selected = selectable if event.value else set()
            self._updating = True
            try:
                for checkbox_id, task_id in self._checkbox_tasks.items():
                    checkbox = self.query_one(f"#{checkbox_id}", Checkbox)
                    if not checkbox.disabled:
                        self._set_checkbox_value_silently(
                            checkbox, task_id in self._selected
                        )
            finally:
                self._updating = False
        else:
            selected_task_id = self._checkbox_tasks.get(event.checkbox.id or "")
            if selected_task_id is None or event.checkbox.disabled:
                return
            if event.value:
                self._selected.add(selected_task_id)
            else:
                self._selected.discard(selected_task_id)
        self._sync_header_and_hint()
        self.post_message(self.SelectionChanged(self.selected_task_ids))

    def _sync_header_and_hint(self) -> None:
        selectable_count = sum(task.id not in self._executions for task in self._tasks)
        self._updating = True
        try:
            self._set_checkbox_value_silently(
                self.query_one("#select-all-tasks", Checkbox),
                bool(selectable_count and len(self._selected) == selectable_count),
            )
        finally:
            self._updating = False
        blocked_count = len(self._tasks) - selectable_count
        blocked = f" {blocked_count} already have executions." if blocked_count else ""
        self.query_one("#task-hint", Static).update(
            f"Selected {len(self._selected)} of {selectable_count} available "
            f"{self._provider} tasks."
            f"{blocked}"
        )

    @staticmethod
    def _set_checkbox_value_silently(checkbox: Checkbox, value: bool) -> None:
        """Update derived checkbox state without turning it into a user action."""

        checkbox.set_reactive(Checkbox.value, value)
        checkbox.set_class(value, "-on")
