"""Reusable multi-task picker for the workflow runner."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from rich.text import Text
from textual.app import ComposeResult, RenderResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.geometry import Size
from textual.message import Message
from textual.widgets import Button, Checkbox, SelectionList, Static

from prchef.models.development_task import DevelopmentTask
from prchef.models.workflow_execution import WorkflowExecution
from prchef.models.workflow_status import WorkflowStatus, can_start_fresh_run
from prchef.tui.widgets import PrchefSelect

if TYPE_CHECKING:
    from prchef.tui.app import PrchefApp

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


class _PaginationButton(Button):
    """Navigation accepts Space as well as Textual's standard Enter binding."""

    BINDINGS = [("space", "press", "")]


class TaskSelector(Vertical):
    """Display open tasks with repository-scoped execution availability."""

    class SelectionChanged(Message):
        def __init__(self, task_ids: tuple[str, ...]) -> None:
            self.task_ids = task_ids
            super().__init__()

    class PageChanged(Message):
        def __init__(self, generation: int, tasks: tuple[DevelopmentTask, ...]) -> None:
            self.generation = generation
            self.tasks = tasks
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
        self._positions: dict[str, int] = {}
        self._filtered: list[DevelopmentTask] = []
        self._filtered_ids: set[str] = set()
        self._never_run: set[str] = set()
        self._selected_never_count = 0
        self._selected_filtered_count = 0
        self._history: set[str] | None = set()
        self._page = 0
        self._page_size = 25
        self._page_generation = 0
        self._render_generation = 0
        self._load_page_status = False
        self._status_pending = False
        self._incomplete = False
        self._executions: dict[str, WorkflowExecution] = {}
        self._checkbox_tasks: dict[str, str] = {}
        self._selected: set[str] = set()
        self._provider = ""
        self._updating = False
        self._rows: list[tuple[DevelopmentTask, TaskRow]] = []
        self._table_header: TaskTableHeader | None = None
        # ``None`` means the provider has no label catalog, so no filter is offered.
        self._label_catalog: tuple[str, ...] | None = None
        self._label_filter: tuple[str, ...] = ()
        self._label_list: SelectionList[str] | None = None

    @property
    def selected_task_ids(self) -> tuple[str, ...]:
        """Every selected task in list order, including rows hidden by the filter."""

        return tuple(task.id for task in self._tasks if task.id in self._selected)

    @property
    def visible_task_ids(self) -> tuple[str, ...]:
        return tuple(task.id for task in self.page_tasks)

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

    @property
    def page_tasks(self) -> tuple[DevelopmentTask, ...]:
        start = self._page * self._page_size
        return tuple(self._filtered[start:start + self._page_size])

    @property
    def page_count(self) -> int:
        return max(1, (len(self._filtered) + self._page_size - 1) // self._page_size)

    def on_mount(self) -> None:
        self._page_size = getattr(self.app, "task_page_size", 25)
        self.query_one("#task-page-size", PrchefSelect).value = str(self._page_size)
        self._sync_pagination()

    def compose(self) -> ComposeResult:
        yield Static("Tasks", classes="field-label")
        with Horizontal(id="task-pagination"):
            yield Button("Refresh tasks", id="refresh-tasks")
            yield _PaginationButton("Previous", id="task-previous", disabled=True)
            yield Static("Page 1 of 1", id="task-page-indicator")
            yield _PaginationButton("Next", id="task-next", disabled=True)
            yield PrchefSelect(
                [(f"{size} / page", str(size)) for size in (10, 25, 50)],
                value="25", allow_blank=False, id="task-page-size",
            )
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
            yield VerticalScroll(id="available-tasks")
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
        history: set[str] | None = None,
        history_error: str = "",
        incomplete: str = "",
        load_page_status: bool = False,
    ) -> None:
        """Show loaded tasks and, for a label-capable provider, its label filter.

        ``labels`` is ``None`` when the provider has no label catalog; an empty
        sequence is a supported catalog with no labels. ``label_error`` reports
        a failed catalog lookup for a provider that does support labels.
        """

        previous = set(self._selected)
        previous_filter = self._label_filter
        loaded = list(tasks)
        if incomplete:
            loaded_ids = {task.id for task in loaded}
            loaded.extend(task for task in self._tasks if task.id not in loaded_ids)
        self._tasks = loaded
        self._executions = {
            task_id: execution for task_id, execution in self._executions.items()
            if task_id in previous
        } | dict(executions or {})
        self._positions = {task.id: index for index, task in enumerate(self._tasks)}
        self._history = set(history) if history is not None else (
            None if history_error else set(self._executions)
        )
        self._load_page_status = load_page_status
        self.query_one("#task-error", Static).update(
            (f"Incomplete task list: {len(self._tasks)} tasks loaded. {incomplete}. "
             "Refresh tasks to retry." if incomplete else "")
            + (f" Execution history unavailable: {history_error}. Select all is disabled."
               if history_error else "")
        )
        self._provider = provider
        self._incomplete = bool(incomplete)
        selectable = {
            task.id for task in self._tasks
            if can_start_fresh_run(
                self._executions[task.id].status if task.id in self._executions else None
            )
        }
        self._selected = previous & selectable
        self._checkbox_tasks.clear()
        self._set_label_catalog(labels, label_error=label_error, retained=previous_filter)

        self.query_one("#refresh-tasks", Button).disabled = False
        self._rebuild_filter()
        self._present_page()
        self.post_message(self.SelectionChanged(self.selected_task_ids))

    def _sync_pagination(self) -> None:
        self.query_one("#task-page-indicator", Static).update(
            f"Page {self._page + 1} of {self.page_count}"
        )
        self.query_one("#task-previous", Button).disabled = self._page == 0
        self.query_one("#task-next", Button).disabled = self._page + 1 >= self.page_count

    def _present_page(self) -> None:
        self._page_generation += 1
        self._status_pending = self._load_page_status and bool(self.page_tasks)
        self._render_page()
        self._sync_pagination()
        if self._status_pending:
            self.post_message(self.PageChanged(self._page_generation, self.page_tasks))

    def set_page_executions(
        self, generation: int, executions: Mapping[str, WorkflowExecution]
    ) -> None:
        if generation != self._page_generation:
            return
        self._status_pending = False
        ids = {task.id for task in self.page_tasks}
        for task_id in ids:
            self._executions.pop(task_id, None)
        self._executions.update(executions)
        newly_seen = self._never_run.intersection(executions)
        history_changed = bool(newly_seen)
        self._selected_never_count -= sum(task_id in self._selected for task_id in newly_seen)
        if self._history is not None:
            self._history.update(executions)
            self._never_run.difference_update(executions)
        blocked = {task_id for task_id, execution in executions.items()
                   if not can_start_fresh_run(execution.status)}
        removed = self._selected & blocked
        changed = bool(removed)
        self._selected_filtered_count -= len(removed & self._filtered_ids)
        self._selected.difference_update(blocked)
        metrics = self._column_metrics()
        if self._table_header is not None:
            self._table_header._metrics = metrics
            self._table_header.refresh()
        for task, row in self._rows:
            execution = self._executions.get(task.id)
            row._status = _execution_status(execution) if execution else ""
            row._metrics = metrics
            row.disabled = not can_start_fresh_run(execution.status if execution else None)
            row.label = self._row_label(task, execution)
            row.tooltip = row.label.plain
            self._set_checkbox_value_silently(row, task.id in self._selected)
            row.refresh()
        # Page status changes affect select-all eligibility; do not rescan the catalog.
        if changed or history_changed:
            self._sync_header_and_hint()
        if changed:
            self.post_message(self.SelectionChanged(self.selected_task_ids))

    @staticmethod
    def _row_label(task: DevelopmentTask, execution: WorkflowExecution | None) -> Text:
        labels = _labels_of(task)
        suffix = f" [{labels}]" if labels else ""
        if execution is not None:
            status = _execution_status(execution)
            suffix += (
                f" \u2014 Latest execution: {status}. Selecting starts a new run; "
                "earlier runs remain in Execution history."
                if can_start_fresh_run(execution.status)
                else f" \u2014 Latest execution: {status} (open in Execution history)"
            )
        return Text(f"{task.id}: {task.title or '(untitled)'}{suffix}")

    def _render_page(self) -> None:
        self._rows = []
        self._checkbox_tasks.clear()
        self._table_header = None
        metrics = self._column_metrics()
        rows: list[Static] = []
        if self.page_tasks:
            self._table_header = TaskTableHeader(metrics=metrics, id="task-table-header")
            rows.append(self._table_header)
        for task in self.page_tasks:
            position = self._positions[task.id]
            checkbox_id = f"task-checkbox-{position}"
            self._checkbox_tasks[checkbox_id] = task.id
            execution = self._executions.get(task.id)
            label_text = _labels_of(task)
            title = task.title or "(untitled)"
            status = _execution_status(execution) if execution else ""
            row = TaskRow(
                self._row_label(task, execution),
                number=task.id,
                title=title,
                labels=label_text,
                status=status,
                metrics=metrics,
                value=task.id in self._selected,
                id=checkbox_id,
                disabled=self._status_pending or not can_start_fresh_run(
                    execution.status if execution else None
                ),
                classes="task-row",
            )
            self._rows.append((task, row))
            rows.append(row)
        self._render_generation += 1
        self.call_next(self._mount_page, rows, self._render_generation)

    async def _mount_page(self, rows: list[Static], generation: int) -> None:
        if generation != self._render_generation:
            return
        task_list = self.query_one("#available-tasks", VerticalScroll)
        await task_list.remove_children()
        if generation == self._render_generation:
            await task_list.mount(*rows)
            task_list.scroll_home(animate=False)

    def _column_metrics(self) -> _ColumnMetrics:
        """Measure only the visible page."""

        number = max(
            (len(task.id) for task in self.page_tasks),
            default=0,
        )
        labels = max(
            (len(_labels_of(task)) for task in self.page_tasks),
            default=0,
        )
        status = max(
            (
                len(_execution_status(execution))
                for task in self.page_tasks
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
            self.call_next(self._mount_labels, self._label_list)

    async def _mount_labels(self, label_list: SelectionList[str]) -> None:
        if label_list is not self._label_list:
            return
        options = self.query_one("#task-label-options", Vertical)
        await options.remove_children()
        if label_list is self._label_list:
            await options.mount(label_list)

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

    def on_select_changed(self, event: PrchefSelect.Changed) -> None:
        if event.select.id != "task-page-size":
            return
        event.stop()
        size = int(str(event.value))
        if size not in (10, 25, 50) or size == self._page_size:
            return
        self._page_size = size
        cast("PrchefApp", self.app).task_page_size = size
        self._page = 0
        self._present_page()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id in {"task-previous", "task-next"}:
            event.stop()
            self._page = max(0, min(self.page_count - 1, self._page + (
                1 if event.button.id == "task-next" else -1
            )))
            self._present_page()
            if event.button.disabled:
                self.query_one("#task-page-size", PrchefSelect).focus()
        if event.button.id == "clear-task-label-filter":
            event.stop()
            self.clear_label_filter()

    def _matches_label_filter(self, task: DevelopmentTask) -> bool:
        # Exact, case-sensitive names; every selected label must be present.
        names = set(_label_names_of(task))
        return all(label in names for label in self._label_filter)

    def _rebuild_filter(self) -> None:
        self._filtered = [task for task in self._tasks if self._matches_label_filter(task)]
        self._filtered_ids = {task.id for task in self._filtered}
        self._never_run = (self._filtered_ids - self._history
                           if self._history is not None else set())
        self._page = min(self._page, self.page_count - 1)
        self._sync_filter_controls()
        self._recount_selection()
        self._sync_header_and_hint()

    def _apply_label_filter(self) -> None:
        self._page = 0
        self._rebuild_filter()
        self._present_page()

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
        self._tasks = []
        self._positions.clear()
        self._history = set()
        self._incomplete = False
        self._provider = ""
        self._filtered = []
        self._filtered_ids.clear()
        self._never_run.clear()
        self._selected_never_count = 0
        self._selected_filtered_count = 0
        self._page = 0
        self._page_generation += 1
        self._render_generation += 1
        self._sync_pagination()
        self._executions.clear()
        self._selected.clear()
        self._checkbox_tasks.clear()
        self._rows = []
        self._table_header = None
        self.query_one("#available-tasks", VerticalScroll).remove_children()
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

        if not preserve_selection:
            self.clear_tasks()
        self._page_generation += 1
        self._provider = provider
        self.query_one("#refresh-tasks", Button).disabled = True
        self.query_one("#task-hint", Static).update(f"Loading available {provider} tasks...")

    def set_error(self, message: str) -> None:
        """Display a task-loading failure without interrupting the launcher."""

        self._incomplete = True
        self._sync_header_and_hint()
        self.query_one("#refresh-tasks", Button).disabled = False
        self.query_one("#task-error", Static).update(
            f"Incomplete task list: could not load tasks: {message}. Refresh tasks to retry."
        )

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        if self._updating:
            return
        if event.checkbox.id == "select-all-tasks":
            # Only shown, available rows change; hidden selections are kept.
            if self._history is None:
                return
            shown = self._never_run
            if event.value:
                self._selected |= shown
            else:
                self._selected -= shown
            self._updating = True
            try:
                for task, checkbox in self._rows:
                    task_id = task.id
                    if not checkbox.disabled:
                        self._set_checkbox_value_silently(
                            checkbox, task_id in self._selected
                        )
            finally:
                self._updating = False
        else:
            selected_task_id = self._checkbox_tasks.get(event.checkbox.id or "")
            if (selected_task_id is None or event.checkbox.disabled
                    or not any(row is event.checkbox for _, row in self._rows)):
                return
            if event.value:
                self._selected.add(selected_task_id)
            else:
                self._selected.discard(selected_task_id)
        self._recount_selection()
        self._sync_header_and_hint()
        self.post_message(self.SelectionChanged(self.selected_task_ids))

    def _recount_selection(self) -> None:
        # Catalog/filter changes and explicit selection actions may scan their scope.
        # Routine page presentation uses these cached counts.
        self._selected_never_count = len(self._selected & self._never_run)
        self._selected_filtered_count = len(self._selected & self._filtered_ids)

    def _sync_header_and_hint(self) -> None:
        visible = self._filtered
        shown = self._never_run
        header = self.query_one("#select-all-tasks", Checkbox)
        header.disabled = not shown
        self._updating = True
        try:
            self._set_checkbox_value_silently(
                header, bool(shown) and self._selected_never_count == len(shown)
            )
        finally:
            self._updating = False
        selected = (
            f"Selected {len(self._selected)} of {len(self._tasks)} {self._provider} tasks. "
            "Select all includes only tasks without earlier runs."
        )
        if not self._tasks and not self._incomplete:
            selected += f" No open {self._provider} tasks were returned."
        if not self._label_filter:
            self.query_one("#task-hint", Static).update(selected)
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
        hidden_count = len(self._selected) - self._selected_filtered_count
        hidden = (
            f" {hidden_count} selected task(s) hidden by the filter will also start."
            if hidden_count
            else ""
        )
        self.query_one("#task-hint", Static).update(
            Text(f"{showing} {selected}{hidden}")
        )

    @staticmethod
    def _set_checkbox_value_silently(checkbox: Checkbox, value: bool) -> None:
        """Update derived checkbox state without turning it into a user action."""

        checkbox.set_reactive(Checkbox.value, value)
        checkbox.set_class(value, "-on")
