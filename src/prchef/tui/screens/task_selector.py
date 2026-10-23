"""Reusable multi-task picker for the workflow runner."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from rich.text import Text
from textual.app import ComposeResult, RenderResult
from textual.containers import Horizontal, Vertical
from textual.geometry import Size
from textual.message import Message
from textual.widgets import Button, Checkbox, SelectionList, Static

from prchef.models.development_task import DevelopmentTask
from prchef.models.workflow_execution import WorkflowExecution
from prchef.models.workflow_status import WorkflowStatus, can_start_fresh_run

# Checked/unchecked/blocked glyphs. ASCII keeps every column aligned on
# terminals that render the Unicode ballot boxes as double-width cells.
_CHECKED = "[x]"
_UNCHECKED = "[ ]"
_BLOCKED = "[-]"
_CHECKBOX_WIDTH = 3
_GAP = 1
_MAX_NUMBER_WIDTH = 12
_MAX_LABELS_WIDTH = 28
_MAX_STATUS_WIDTH = 32


def _execution_status(execution: WorkflowExecution) -> str:
    if execution.status is WorkflowStatus.INTERRUPTED:
        return f"INTERRUPTED: {(execution.resume_stage or execution.current_stage).value}"
    return execution.status.value
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


def _label_names_of(task: DevelopmentTask) -> list[str]:
    labels = task.metadata.get("labels", [])
    if not isinstance(labels, list):
        return []
    return [str(label) for label in labels]


def _labels_of(task: DevelopmentTask) -> str:
    return ", ".join(label for label in _label_names_of(task) if label.strip())


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
        self._rows: list[tuple[DevelopmentTask, TaskRow]] = []
        self._table_header: TaskTableHeader | None = None
        # ``None`` means the provider has no label catalog, so no filter is offered.
        self._label_catalog: tuple[str, ...] | None = None
        self._label_filter: tuple[str, ...] = ()
        self._pending_label_filter: tuple[str, ...] | None = None
        self._label_list: SelectionList[str] | None = None

    @property
    def selected_task_ids(self) -> tuple[str, ...]:
        """Every selected task in list order, including rows hidden by the filter."""

        return tuple(task.id for task in self._tasks if task.id in self._selected)

    @property
    def visible_task_ids(self) -> tuple[str, ...]:
        return tuple(task.id for task in self._visible_tasks())

    @property
    def label_filter(self) -> tuple[str, ...]:
        return self._label_filter

    @property
    def label_catalog(self) -> tuple[str, ...] | None:
        return self._label_catalog

    @property
    def selected_task_id(self) -> str | None:
        selected = self.selected_task_ids
        return selected[0] if len(selected) == 1 else None

    @property
    def selected_tasks(self) -> tuple[DevelopmentTask, ...]:
        return tuple(task for task in self._tasks if task.id in self._selected)

    @property
    def selected_previous_executions(self) -> dict[str, WorkflowExecution]:
        return {
            task.id: self._executions[task.id]
            for task in self._tasks
            if task.id in self._selected and task.id in self._executions
        }

    def compose(self) -> ComposeResult:
        yield Static("Tasks", classes="field-label")
        yield Button("Refresh tasks", id="refresh-tasks")
        # Shown only for providers with a label catalog.
        with Vertical(id="task-label-filter"):
            with Horizontal(id="task-label-filter-bar"):
                yield Static(
                    "Label filter: off. Select labels to narrow the task list.",
                    id="task-label-filter-summary",
                )
                yield Button("Clear filter", id="clear-task-label-filter", disabled=True)
            yield Vertical(id="task-label-options")
            yield Static("", id="task-label-status")
        with Vertical(id="task-table"):
            yield _GlyphCheckbox(
                "Select all tasks without earlier runs",
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
        labels: Sequence[str] | None = None,
        label_error: str = "",
    ) -> None:
        """Show loaded tasks and, for a label-capable provider, its label filter.

        ``labels`` is ``None`` when the provider has no label catalog; an empty
        sequence is a supported catalog with no labels. ``label_error`` reports
        a failed catalog lookup for a provider that does support labels.
        """

        previous = (
            set(self._pending_selection)
            if self._pending_selection is not None
            else set(self._selected)
        )
        previous_filter = (
            self._pending_label_filter
            if self._pending_label_filter is not None
            else self._label_filter
        )
        self._pending_selection = None
        self._pending_label_filter = None
        self._tasks = list(tasks)
        self._executions = dict(executions or {})
        self._provider = provider
        selectable = {
            task.id for task in self._tasks
            if can_start_fresh_run(
                self._executions[task.id].status if task.id in self._executions else None
            )
        }
        self._selected = previous & selectable
        self._checkbox_tasks.clear()
        self._set_label_catalog(labels, label_error=label_error, retained=previous_filter)

        task_list = self.query_one("#available-tasks", Vertical)
        task_list.remove_children()
        self._rows = []
        self._table_header = None
        metrics = self._column_metrics()
        rows: list[Static] = []
        if self._tasks:
            self._table_header = TaskTableHeader(metrics=metrics, id="task-table-header")
            rows.append(self._table_header)
        for position, task in enumerate(self._tasks):
            checkbox_id = f"task-checkbox-{position}"
            self._checkbox_tasks[checkbox_id] = task.id
            execution = self._executions.get(task.id)
            label_text = _labels_of(task)
            title = task.title or "(untitled)"
            suffix = f" [{label_text}]" if label_text else ""
            status = ""
            if execution is not None:
                status = _execution_status(execution)
                suffix += (
                    f" — Latest execution: {status}. Selecting starts a new run; "
                    "earlier runs remain in Execution history."
                    if can_start_fresh_run(execution.status)
                    else f" — Latest execution: {status} (open in Execution history)"
                )
            row = TaskRow(
                Text(f"{task.id}: {title}{suffix}"),
                number=task.id,
                title=title,
                labels=label_text,
                status=status,
                metrics=metrics,
                value=task.id in self._selected,
                id=checkbox_id,
                disabled=not can_start_fresh_run(execution.status if execution else None),
                classes="task-row",
            )
            self._rows.append((task, row))
            rows.append(row)
        if rows:
            task_list.mount(*rows)

        self.query_one("#refresh-tasks", Button).disabled = False
        self.query_one("#task-error", Static).update("")
        self._apply_label_filter()
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
                len(_execution_status(execution))
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

    def _set_label_catalog(
        self,
        labels: Sequence[str] | None,
        *,
        label_error: str,
        retained: tuple[str, ...],
    ) -> None:
        """Rebuild the label control, keeping retained labels that still exist."""

        options = self.query_one("#task-label-options", Vertical)
        options.remove_children()
        self._label_list = None
        status = self.query_one("#task-label-status", Static)
        status.remove_class("-error")
        supported = labels is not None or bool(label_error)
        self._label_catalog = tuple(dict.fromkeys(labels or ())) if supported else None
        catalog = self._label_catalog or ()
        self._label_filter = tuple(label for label in catalog if label in retained)
        self.query_one("#task-label-filter", Vertical).display = supported
        if label_error:
            status.update(
                Text(
                    f"Could not load labels: {label_error}. Showing every loaded task; "
                    "refresh to retry the label filter."
                )
            )
            status.add_class("-error")
        elif supported and not catalog:
            status.update("This repository has no labels to filter by.")
        else:
            status.update("")
        if catalog:
            self._label_list = SelectionList[str](
                *[(Text(label), label, label in self._label_filter) for label in catalog],
                id="task-label-list",
            )
            options.mount(self._label_list)

    def _reset_label_filter(self) -> None:
        self._label_catalog = None
        self._label_filter = ()
        self._label_list = None
        self.query_one("#task-label-options", Vertical).remove_children()
        self.query_one("#task-label-status", Static).update("")
        self.query_one("#task-label-filter", Vertical).display = False
        self._sync_filter_controls()

    def clear_label_filter(self) -> None:
        """Show the full loaded list again, in its original order."""

        self._label_filter = ()
        if self._label_list is not None:
            self._label_list.deselect_all()
        self._apply_label_filter()

    def on_selection_list_selected_changed(
        self, event: SelectionList.SelectedChanged[str]
    ) -> None:
        event.stop()
        # A list replaced by a newer load can still deliver a queued message.
        if event.selection_list is not self._label_list:
            return
        chosen = set(event.selection_list.selected)
        self._label_filter = tuple(
            label for label in self._label_catalog or () if label in chosen
        )
        self._apply_label_filter()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "clear-task-label-filter":
            event.stop()
            self.clear_label_filter()

    def _matches_label_filter(self, task: DevelopmentTask) -> bool:
        # Exact, case-sensitive names; every selected label must be present.
        names = set(_label_names_of(task))
        return all(label in names for label in self._label_filter)

    def _visible_tasks(self) -> list[DevelopmentTask]:
        return [task for task in self._tasks if self._matches_label_filter(task)]

    def _apply_label_filter(self) -> None:
        any_visible = False
        for task, row in self._rows:
            row.display = self._matches_label_filter(task)
            any_visible = any_visible or row.display
        if self._table_header is not None:
            self._table_header.display = any_visible
        self._sync_filter_controls()
        self._sync_header_and_hint()

    def _sync_filter_controls(self) -> None:
        summary = self.query_one("#task-label-filter-summary", Static)
        if self._label_filter:
            summary.update(
                Text(f"Label filter: {' + '.join(self._label_filter)} (tasks must have all)")
            )
        else:
            summary.update("Label filter: off. Select labels to narrow the task list.")
        self.query_one("#clear-task-label-filter", Button).disabled = not self._label_filter
        self.query_one("#select-all-tasks", Checkbox).label = (
            "Select all shown tasks without earlier runs"
            if self._label_filter else "Select all tasks without earlier runs"
        )

    def clear_tasks(self) -> None:
        self._pending_selection = None
        self._pending_label_filter = None
        self._tasks = []
        self._executions.clear()
        self._selected.clear()
        self._checkbox_tasks.clear()
        self._rows = []
        self._table_header = None
        self.query_one("#available-tasks", Vertical).remove_children()
        self._reset_label_filter()
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
        # A refresh reconciles the filter against the reloaded catalog; a new
        # repository or provider starts unfiltered.
        retained_filter = (
            (
                self._pending_label_filter
                if self._pending_label_filter is not None
                else self._label_filter
            )
            if preserve_selection
            else ()
        )
        self.clear_tasks()
        self._pending_selection = retained
        self._pending_label_filter = retained_filter
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
            # Only shown, available rows change; hidden selections are kept.
            shown = {
                task.id for task in self._visible_tasks() if task.id not in self._executions
            }
            if event.value:
                self._selected |= shown
            else:
                self._selected -= shown
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
        visible = self._visible_tasks()
        shown = {task.id for task in visible if task.id not in self._executions}
        selectable_count = sum(
            can_start_fresh_run(
                self._executions[task.id].status if task.id in self._executions else None
            ) for task in self._tasks
        )
        header = self.query_one("#select-all-tasks", Checkbox)
        header.disabled = not shown
        self._updating = True
        try:
            self._set_checkbox_value_silently(
                header, bool(shown) and shown <= self._selected
            )
        finally:
            self._updating = False
        blocked_count = sum(
            not can_start_fresh_run(self._executions[task.id].status)
            for task in visible if task.id in self._executions
        )
        blocked = f" {blocked_count} have active or approved executions." if blocked_count else ""
        selected = (
            f"Selected {len(self._selected)} of {selectable_count} available "
            f"{self._provider} tasks. Select all includes only tasks without earlier runs."
        )
        if not self._label_filter:
            self.query_one("#task-hint", Static).update(f"{selected}{blocked}")
            return
        labels = " + ".join(self._label_filter)
        if visible:
            showing = (
                f"Showing {len(visible)} of {len(self._tasks)} loaded tasks labelled {labels}."
            )
        else:
            showing = (
                f"No loaded {self._provider} tasks have every selected label ({labels}). "
                "Clear the filter to show all tasks."
            )
        hidden_count = len(self._selected - {task.id for task in visible})
        hidden = (
            f" {hidden_count} selected task(s) hidden by the filter will also start."
            if hidden_count
            else ""
        )
        self.query_one("#task-hint", Static).update(
            Text(f"{showing} {selected}{hidden}{blocked}")
        )

    @staticmethod
    def _set_checkbox_value_silently(checkbox: Checkbox, value: bool) -> None:
        """Update derived checkbox state without turning it into a user action."""

        checkbox.set_reactive(Checkbox.value, value)
        checkbox.set_class(value, "-on")
