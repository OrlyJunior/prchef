"""Reusable multi-task picker for the workflow runner."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from rich.text import Text
from textual.app import ComposeResult, RenderResult
from textual.containers import Vertical
from textual.geometry import Size
from textual.message import Message
from textual.widgets import Button, Checkbox, Static

from orchestrator.models.development_task import DevelopmentTask
from orchestrator.models.workflow_execution import WorkflowExecution

# Checked/unchecked/blocked glyphs. ASCII keeps every column aligned on
# terminals that render the Unicode ballot boxes as double-width cells.
_CHECKED = "[x]"
_UNCHECKED = "[ ]"
_BLOCKED = "[-]"
_CHECKBOX_WIDTH = 3
_GAP = 1
_MAX_NUMBER_WIDTH = 12
_MAX_LABELS_WIDTH = 28
_MAX_STATUS_WIDTH = 16
_MIN_TITLE_WIDTH = 8
_MIN_LABELS_WIDTH = 8


@dataclass(frozen=True)
class _ColumnMetrics:
    """Widest content each optional column holds for the current task list."""

    number: int = 0
    labels: int = 0
    status: int = 0


@dataclass(frozen=True)
class _ColumnWidths:
    """Resolved cell widths for one row of the task table."""

    number: int
    title: int
    labels: int
    status: int


def _resolve_columns(width: int, metrics: _ColumnMetrics) -> _ColumnWidths:
    """Split the row width into aligned columns, favouring the title."""

    available = width - _CHECKBOX_WIDTH - _GAP
    if available < _MIN_TITLE_WIDTH:
        return _ColumnWidths(number=0, title=max(available, 0), labels=0, status=0)

    number = min(metrics.number, _MAX_NUMBER_WIDTH)
    if number:
        available -= number + _GAP

    status = min(metrics.status, _MAX_STATUS_WIDTH)
    if status and available - status - _GAP >= _MIN_TITLE_WIDTH:
        available -= status + _GAP
    else:
        status = 0

    # Labels never take more than a third of what is left, and the column is
    # dropped entirely when the remainder is too narrow to be worth reading.
    wanted = min(metrics.labels, _MAX_LABELS_WIDTH)
    labels = min(wanted, max(available // 3, 0))
    if (
        labels >= min(wanted, _MIN_LABELS_WIDTH)
        and available - labels - _GAP >= _MIN_TITLE_WIDTH
    ):
        available -= labels + _GAP
    else:
        labels = 0

    return _ColumnWidths(number=number, title=max(available, 0), labels=labels, status=status)


def _cell(text: Text, width: int) -> Text:
    """Truncate with an ellipsis and pad so the next column stays aligned."""

    cell = text.copy()
    cell.truncate(width, overflow="ellipsis", pad=True)
    return cell


def _labels_of(task: DevelopmentTask) -> str:
    labels = task.metadata.get("labels", [])
    if not isinstance(labels, list):
        labels = []
    return ", ".join(str(label) for label in labels if str(label).strip())


class _GlyphCheckbox(Checkbox):
    """Checkbox that swaps the static ``X`` for a checked/unchecked glyph pair."""

    BUTTON_LEFT = ""
    BUTTON_INNER = _UNCHECKED
    BUTTON_RIGHT = ""

    @property
    def _button(self) -> Text:
        if self.disabled:
            glyph = _BLOCKED
        else:
            glyph = _CHECKED if self.value else _UNCHECKED
        return Text(glyph, style=self.get_component_rich_style("toggle--button"))


class TaskRow(_GlyphCheckbox):
    """One task rendered as an aligned checkbox/number/title/labels row."""

    def __init__(
        self,
        label: Text,
        *,
        number: str,
        title: str,
        labels: str,
        status: str,
        metrics: _ColumnMetrics,
        value: bool = False,
        id: str | None = None,
        disabled: bool = False,
        classes: str | None = None,
    ) -> None:
        super().__init__(
            label,
            value=value,
            id=id,
            disabled=disabled,
            classes=classes,
            # Columns clip; the tooltip keeps the full text reachable.
            tooltip=label.plain,
        )
        self._number = number
        self._title = title
        self._labels = labels
        self._status = status
        self._metrics = metrics

    def render(self) -> RenderResult:
        width = self.content_size.width or self.size.width
        if width <= 0:
            return super().render()
        columns = _resolve_columns(width, self._metrics)
        row = Text(no_wrap=True, overflow="ellipsis", end="")
        row.append_text(self._button)
        row.append(" ")
        if columns.number:
            row.append_text(_cell(Text(self._number, style="bold"), columns.number))
            row.append(" ")
        row.append_text(_cell(Text(self._title), columns.title))
        if columns.labels:
            row.append(" ")
            row.append_text(_cell(Text(self._labels, style="italic"), columns.labels))
        if columns.status:
            row.append(" ")
            row.append_text(_cell(Text(self._status), columns.status))
        return row

    def get_content_width(self, container: Size, viewport: Size) -> int:
        return container.width

    def on_resize(self) -> None:
        self.refresh()


class TaskTableHeader(Static):
    """Column headings kept in step with the task rows underneath."""

    def __init__(self, *, metrics: _ColumnMetrics, id: str | None = None) -> None:
        super().__init__(id=id)
        self._metrics = metrics

    def render(self) -> RenderResult:
        width = self.content_size.width or self.size.width
        if width <= 0:
            return Text("")
        columns = _resolve_columns(width, self._metrics)
        row = Text(no_wrap=True, overflow="ellipsis", end="")
        row.append(" " * (_CHECKBOX_WIDTH + _GAP))
        if columns.number:
            row.append_text(_cell(Text("Task"), columns.number))
            row.append(" ")
        row.append_text(_cell(Text("Title"), columns.title))
        if columns.labels:
            row.append(" ")
            row.append_text(_cell(Text("Labels"), columns.labels))
        if columns.status:
            row.append(" ")
            row.append_text(_cell(Text("Status"), columns.status))
        return row

    def on_resize(self) -> None:
        self.refresh()


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
        self._pending_selection: set[str] | None = None
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
        with Vertical(id="task-table"):
            yield _GlyphCheckbox(
                "Select all available tasks",
                id="select-all-tasks",
                disabled=True,
                classes="task-select-all",
            )
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
        previous = (
            set(self._pending_selection)
            if self._pending_selection is not None
            else set(self._selected)
        )
        self._pending_selection = None
        self._tasks = list(tasks)
        self._executions = dict(executions or {})
        self._provider = provider
        selectable = {task.id for task in self._tasks if task.id not in self._executions}
        self._selected = previous & selectable
        self._checkbox_tasks.clear()

        task_list = self.query_one("#available-tasks", Vertical)
        task_list.remove_children()
        metrics = self._column_metrics()
        rows: list[Static] = []
        if self._tasks:
            rows.append(TaskTableHeader(metrics=metrics, id="task-table-header"))
        for position, task in enumerate(self._tasks):
            checkbox_id = f"task-checkbox-{position}"
            self._checkbox_tasks[checkbox_id] = task.id
            execution = self._executions.get(task.id)
            label_text = _labels_of(task)
            title = task.title or "(untitled)"
            suffix = f" [{label_text}]" if label_text else ""
            status = ""
            if execution is not None:
                status = execution.status.value
                suffix += f" — {status} (open in Execution history)"
            rows.append(
                TaskRow(
                    Text(f"{task.id}: {title}{suffix}"),
                    number=task.id,
                    title=title,
                    labels=label_text,
                    status=status,
                    metrics=metrics,
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
        self.post_message(self.SelectionChanged(self.selected_task_ids))

    def _column_metrics(self) -> _ColumnMetrics:
        """Measure the natural width of every column for the loaded tasks."""

        number = max(
            (len(task.id) for task in self._tasks),
            default=0,
        )
        labels = max(
            (len(_labels_of(task)) for task in self._tasks),
            default=0,
        )
        status = max(
            (
                len(execution.status.value)
                for task in self._tasks
                if (execution := self._executions.get(task.id)) is not None
            ),
            default=0,
        )
        return _ColumnMetrics(
            number=max(number, len("Task")),
            labels=max(labels, len("Labels")) if labels else 0,
            status=max(status, len("Status")) if status else 0,
        )

    def clear_tasks(self) -> None:
        self._pending_selection = None
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

    def set_tasks_loading(
        self, *, provider: str, preserve_selection: bool = False
    ) -> None:
        """Render an inline busy state while the runner fetches tasks."""

        retained = (
            set(self._pending_selection or self._selected)
            if preserve_selection
            else set()
        )
        self.clear_tasks()
        self._pending_selection = retained
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
