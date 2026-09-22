"""Expandable, per-task batch progress rows shared by live and persisted views."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.message import Message
from textual.widgets import Button, Static

from orchestrator.tui.batch_progress import (
    SUMMARY_CATEGORIES,
    SUMMARY_LABELS,
    BatchRowView,
    summarize,
)


class BatchItemRow(Vertical):
    """One batch task, collapsed to a summary line until the user expands it.

    Expanded content grows this row's own height inside the owning screen's
    main ``VerticalScroll`` rather than opening a nested scroll viewport, so
    long timelines and detailed logs stay reachable on small terminals.
    """

    class ShowLogsRequested(Message):
        """Detailed logs became visible and should be filled in."""

        def __init__(self, position: int) -> None:
            self.position = position
            super().__init__()

    class CopyLogsRequested(Message):
        def __init__(self, position: int) -> None:
            self.position = position
            super().__init__()

    class OpenExecutionRequested(Message):
        """The full execution detail (resume/continue, links) is wanted."""

        def __init__(self, position: int) -> None:
            self.position = position
            super().__init__()

    def __init__(self, row: BatchRowView) -> None:
        super().__init__(id=f"batch-row-{row.position}", classes="batch-item-row")
        self.position = row.position
        self._expanded = False
        self._showing_detailed_logs = False
        self._row = row
        self._composed = False

    def compose(self) -> ComposeResult:
        prefix = self.id
        yield Button("", id=f"{prefix}-header", classes="batch-item-header")
        with Vertical(id=f"{prefix}-body", classes="batch-item-body"):
            yield Static("No activity yet.", id=f"{prefix}-timeline", markup=False)
            with Horizontal(classes="batch-item-log-actions"):
                yield Button(
                    "Show detailed logs", id=f"{prefix}-toggle-logs", disabled=True
                )
                yield Button("Copy detailed logs", id=f"{prefix}-copy-logs", disabled=True)
                yield Button("Open execution", id=f"{prefix}-open-execution", disabled=True)
            yield Static("Detailed logs", id=f"{prefix}-detailed-title")
            yield Static("", id=f"{prefix}-detailed", markup=False, expand=True)

    def on_mount(self) -> None:
        # This widget's own children only exist once mounting completes, so
        # the initial row content is applied here rather than right after
        # ``BatchProgressView`` schedules the mount.
        prefix = self.id
        self.query_one(f"#{prefix}-body", Vertical).display = False
        self.query_one(f"#{prefix}-detailed-title", Static).display = False
        self.query_one(f"#{prefix}-detailed", Static).display = False
        self._composed = True
        self.update_row(self._row)

    def update_row(self, row: BatchRowView) -> None:
        self._row = row
        if not self._composed:
            # Live updates can arrive between mount being scheduled and it
            # completing; ``on_mount`` applies the latest ``_row`` then.
            return
        prefix = self.id
        glyph = "▾" if self._expanded else "▸"
        stage_suffix = f" — {row.stage_label}" if row.stage_label else ""
        self.query_one(f"#{prefix}-header", Button).label = (
            f"{glyph} {row.position + 1}. {row.status_label} | "
            f"{row.task_id}: {row.task_title}{stage_suffix}"
        )
        self.query_one(f"#{prefix}-timeline", Static).update(
            "\n".join(row.timeline) if row.timeline else "No activity yet."
        )
        has_execution = row.execution_id is not None
        self.query_one(f"#{prefix}-toggle-logs", Button).disabled = not has_execution
        self.query_one(f"#{prefix}-copy-logs", Button).disabled = not has_execution
        self.query_one(f"#{prefix}-open-execution", Button).disabled = not has_execution

    def on_button_pressed(self, event: Button.Pressed) -> None:
        prefix = self.id
        if event.button.id == f"{prefix}-header":
            event.stop()
            self._expanded = not self._expanded
            self.query_one(f"#{prefix}-body", Vertical).display = self._expanded
            if self._row is not None:
                self.update_row(self._row)
            if self._expanded:
                self.scroll_visible(animate=False)
            return
        if event.button.id == f"{prefix}-toggle-logs":
            event.stop()
            self._showing_detailed_logs = not self._showing_detailed_logs
            title = self.query_one(f"#{prefix}-detailed-title", Static)
            detailed = self.query_one(f"#{prefix}-detailed", Static)
            title.display = self._showing_detailed_logs
            detailed.display = self._showing_detailed_logs
            event.button.label = (
                "Hide detailed logs" if self._showing_detailed_logs else "Show detailed logs"
            )
            if self._showing_detailed_logs:
                self.post_message(self.ShowLogsRequested(self.position))
                self.scroll_visible(animate=False)
            return
        if event.button.id == f"{prefix}-copy-logs":
            event.stop()
            self.post_message(self.CopyLogsRequested(self.position))
            return
        if event.button.id == f"{prefix}-open-execution":
            event.stop()
            self.post_message(self.OpenExecutionRequested(self.position))
            return

    def set_detailed_logs(self, text: str) -> None:
        self.query_one(f"#{self.id}-detailed", Static).update(
            text or "No detailed logs are available yet."
        )


class BatchProgressView(Vertical):
    """Ordered, expandable batch rows plus a status summary that stays visible."""

    def compose(self) -> ComposeResult:
        yield Static("", id="batch-progress-summary", classes="batch-progress-summary")
        yield Static("", id="batch-progress-note", classes="batch-progress-note")
        yield Vertical(id="batch-progress-rows")

    def __init__(self, *, id: str | None = None, classes: str | None = None) -> None:
        super().__init__(id=id, classes=classes)
        # Latest content per position. Rows are (re)built from this rather
        # than from the ``set_rows`` argument, so live updates that arrive
        # while a rebuild is still in flight are not lost.
        self._rows: dict[int, BatchRowView] = {}

    def set_rows(self, rows: list[BatchRowView]) -> None:
        """(Re)build every row. Only used for a fresh batch load or reload."""

        self._rows = {row.position: row for row in rows}
        self.update_summary(summarize(rows))
        # Textual removes widgets asynchronously, so the previous rows must be
        # fully gone before rows reusing their ids are mounted. The rebuild is
        # owned by the screen so it shares the screen's worker lifetime.
        self.screen.run_worker(self._rebuild_rows(), group="batch-progress-rows")

    async def _rebuild_rows(self) -> None:
        container = self.query_one("#batch-progress-rows", Vertical)
        # The container lock serializes back-to-back rebuilds (e.g. repeated
        # refreshes), so each one removes whatever the previous one mounted.
        async with container.batch():
            await container.remove_children()
            if self._rows and container.is_attached:
                # Each row applies its own initial content once mounted (see
                # ``BatchItemRow.on_mount``).
                await container.mount(
                    *(BatchItemRow(self._rows[position]) for position in sorted(self._rows))
                )

    def update_row(self, row: BatchRowView) -> None:
        """Update one row in place; used for event-driven live refreshes."""

        self._rows[row.position] = row
        try:
            widget = self.query_one(f"#batch-row-{row.position}", BatchItemRow)
        except NoMatches:
            # Not mounted yet: the pending rebuild picks up ``self._rows``.
            return
        widget.update_row(row)

    def row(self, position: int) -> BatchItemRow | None:
        try:
            return self.query_one(f"#batch-row-{position}", BatchItemRow)
        except NoMatches:
            return None

    def update_summary(self, counts: dict[str, int]) -> None:
        self.query_one("#batch-progress-summary", Static).update(
            " · ".join(
                f"{SUMMARY_LABELS[category]}: {counts.get(category, 0)}"
                for category in SUMMARY_CATEGORIES
            )
        )

    def set_note(self, message: str) -> None:
        self.query_one("#batch-progress-note", Static).update(message)
