"""Detailed persisted workflow inspection screen."""

from __future__ import annotations

import json
import threading
import webbrowser
from dataclasses import asdict
from typing import TYPE_CHECKING, cast

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.containers import Grid, Horizontal, ScrollableContainer, VerticalScroll
from textual.events import DescendantFocus, Resize
from textual.message import Message
from textual.screen import Screen
from textual.timer import Timer
from textual.widgets import Button, Log, Static, TextArea

from orchestrator.models.execution_diagnostic import ExecutionDiagnostic
from orchestrator.models.workflow_event import WorkflowEvent
from orchestrator.models.workflow_execution import WorkflowExecution
from orchestrator.models.workflow_status import (
    ACTIVE_WORKFLOW_STATUSES,
    WorkflowStage,
    WorkflowStatus,
)
from orchestrator.models.workflow_step import WorkflowStep
from orchestrator.runtime_logging import redact_payload
from orchestrator.tui.bridge import WorkflowController
from orchestrator.tui.diagnostics import diagnostic_for_display, format_diagnostics_for_export
from orchestrator.tui.screens.execution_history import execution_status_label
from orchestrator.tui.workflow_presentation import status_message, step_timeline_lines

if TYPE_CHECKING:
    from orchestrator.tui.app import OrchestratorApp


class ExecutionDetailRefreshRequested(Message):
    """Queue a workflow event read on Textual's UI thread."""


class ExecutionDetailScreen(Screen[None]):
    """Render persisted execution details and resume a pending human response."""

    BINDINGS = [("escape", "app.pop_screen", "Back")]

    def __init__(self, execution_id: str) -> None:
        super().__init__()
        self.execution_id = execution_id
        self._links: dict[str, str] = {}
        self._diagnostics: list[ExecutionDiagnostic] = []
        self._last_steps: list[WorkflowStep] = []
        self._reconcile_timer: Timer | None = None
        self._diagnostics_loaded = False
        self._diagnostic_cursor = 0
        self._displayed_details: str | None = None
        self._displayed_timeline: str | None = None
        self._interaction_id: int | None = None
        self._continuation_seeded = False
        self._event_refresh_lock = threading.Lock()
        self._event_refresh_queued = False

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="execution-detail"):
            yield Static("Execution detail", classes="screen-title")
            yield Static("", id="execution-status")
            yield Static("", id="execution-stage")
            yield Static("", id="execution-timeline", markup=False)
            with Grid(id="execution-detail-actions"):
                yield Button("Cancel", variant="error", id="execution-cancel")
                yield Button("Open task", id="open-task")
                yield Button("Open PR", id="open-pr")
                yield Button("Open planner session", id="open-spec-session")
                yield Button("Open review session", id="open-review-session")
                yield Button("Copy detailed logs", id="copy-detailed-logs-button", disabled=True)
            yield Static("", id="execution-continuation-title")
            yield TextArea("", id="execution-continuation-feedback")
            with Horizontal(classes="screen-actions", id="execution-continuation-actions"):
                yield Button(
                    "Continue Execution",
                    variant="primary",
                    id="execution-continue",
                    disabled=True,
                )
            yield Static("", id="execution-human-title")
            yield Static("", id="execution-human-prompt")
            yield TextArea("", id="execution-human-response")
            with Horizontal(classes="screen-actions", id="execution-human-actions"):
                yield Button(
                    "Resume workflow",
                    variant="primary",
                    id="execution-resume",
                    disabled=True,
                )
            with ScrollableContainer(id="execution-content-scroll"):
                yield Static("Loading execution…", id="execution-content", markup=False)
            yield Button("Hide detailed logs", id="execution-toggle-logs")
            yield Log(id="execution-detailed-logs", auto_scroll=False)

    @property
    def controller(self) -> WorkflowController:
        return cast("OrchestratorApp", self.app).controller

    def on_mount(self) -> None:
        self._set_action_layout(self.size.width)
        self.controller.subscribe_execution_events(self._observe_status)
        self._refresh_execution()

    def on_resize(self, event: Resize) -> None:
        self._set_action_layout(event.size.width)

    def _set_action_layout(self, width: int) -> None:
        self.set_class(width < 80, "-medium-actions")
        self.set_class(width < 55, "-narrow-actions")

    def on_unmount(self) -> None:
        self.controller.unsubscribe_execution_events(self._observe_status)
        if self._reconcile_timer is not None:
            self._reconcile_timer.stop()
            self._reconcile_timer = None

    def on_screen_resume(self) -> None:
        self._refresh_execution()

    def on_screen_suspend(self) -> None:
        if self._reconcile_timer is not None:
            self._reconcile_timer.stop()
            self._reconcile_timer = None

    def _refresh_execution(self) -> None:
        # Read on the Textual thread when a callback is handled, rather than
        # rendering a snapshot captured before a later workflow transition.
        if self.app.screen is not self:
            return
        page = self.query_one("#execution-detail", VerticalScroll)
        details_scroll = self.query_one("#execution-content-scroll", ScrollableContainer)
        log = self.query_one("#execution-detailed-logs", Log)
        offsets = (
            (page.scroll_x, page.scroll_y),
            (details_scroll.scroll_x, details_scroll.scroll_y),
            (log.scroll_x, log.scroll_y),
        )
        try:
            execution, steps = self.controller.execution_detail(self.execution_id)
        except Exception as exc:
            try:
                execution = self.controller.execution_snapshot(self.execution_id)
            except Exception:
                self.query_one("#execution-content", Static).update(
                    f"Could not load execution {self.execution_id}: {exc}"
                )
                return
            steps = self._last_steps
        self._render_execution(execution, steps)
        try:
            diagnostics = (
                self.controller.execution_diagnostics(self.execution_id)
                if not self._diagnostics_loaded
                else self.controller.execution_diagnostics_after(
                    self.execution_id, self._diagnostic_cursor
                )
            )
            self._append_diagnostics(diagnostics)
            self._diagnostics_loaded = True
        except Exception:
            pass  # A later tick retries the same cursor.
        self.call_after_refresh(self._restore_scroll, offsets)

    def _restore_scroll(
        self, offsets: tuple[tuple[float, float], tuple[float, float], tuple[float, float]]
    ) -> None:
        if self.app.screen is not self:
            return
        for widget_id, (x, y) in zip(
            ("execution-detail", "execution-content-scroll", "execution-detailed-logs"),
            offsets,
            strict=True,
        ):
            self.query_one(f"#{widget_id}").scroll_to(x=x, y=y, animate=False)

    def _observe_status(self, event: WorkflowEvent) -> None:
        if event.execution_id != self.execution_id:
            return
        with self._event_refresh_lock:
            if self._event_refresh_queued:
                return
            self._event_refresh_queued = True
        try:
            if self.post_message(ExecutionDetailRefreshRequested()):
                return
        except Exception:
            pass
        with self._event_refresh_lock:
            self._event_refresh_queued = False

    def on_execution_detail_refresh_requested(
        self, _event: ExecutionDetailRefreshRequested
    ) -> None:
        with self._event_refresh_lock:
            self._event_refresh_queued = False
        self._refresh_execution()

    def _append_diagnostics(self, diagnostics: list[ExecutionDiagnostic]) -> None:
        log = self.query_one("#execution-detailed-logs", Log)
        for diagnostic in diagnostics:
            if diagnostic.id is not None and diagnostic.id <= self._diagnostic_cursor:
                continue
            rendered = json.dumps(
                redact_payload(diagnostic_for_display(diagnostic)),
                ensure_ascii=False,
                indent=2,
                default=str,
            )
            log.write_line(rendered)
            self._diagnostics.append(diagnostic)
            if diagnostic.id is not None:
                self._diagnostic_cursor = diagnostic.id
        self.query_one("#copy-detailed-logs-button", Button).disabled = not self._diagnostics

    def on_descendant_focus(self, event: DescendantFocus) -> None:
        self.query_one("#execution-detail", VerticalScroll).scroll_to_widget(
            event.widget, animate=False
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "execution-cancel":
            from orchestrator.tui.screens.workflow_runner import StopWorkflowConfirmation

            self.app.push_screen(StopWorkflowConfirmation(), self._confirm_cancellation)
            return
        if event.button.id == "execution-resume":
            response = self.query_one("#execution-human-response", TextArea).text.strip()
            if response:
                event.button.disabled = True
                self._resume_execution(response)
            return
        if event.button.id == "execution-continue":
            feedback = self.query_one("#execution-continuation-feedback", TextArea).text.strip()
            if feedback:
                event.button.disabled = True
                self._continue_execution(feedback)
            return
        if event.button.id == "copy-detailed-logs-button":
            self._copy_detailed_logs()
            return
        if event.button.id == "execution-toggle-logs":
            log = self.query_one("#execution-detailed-logs", Log)
            log.display = not log.display
            event.button.label = "Hide detailed logs" if log.display else "Show detailed logs"
            return
        url = self._links.get(event.button.id or "")
        if not url:
            self.notify("No related link was persisted for this execution.", severity="warning")
            return
        webbrowser.open(url)
        self.notify("Opened related resource in the default browser.")

    def _confirm_cancellation(self, confirmed: bool | None) -> None:
        if not confirmed:
            self._refresh_execution()
            return
        cancel = self.query_one("#execution-cancel", Button)
        cancel.disabled = True
        try:
            accepted = self.controller.cancel_workflow(self.execution_id)
        except Exception as exc:
            self.notify(f"Could not request cancellation: {exc}", severity="error")
        else:
            if not accepted:
                self.notify("This workflow has already finished.", severity="warning")
        finally:
            self._refresh_execution()

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if event.text_area.id == "execution-human-response":
            self.query_one("#execution-resume", Button).disabled = not event.text_area.text.strip()
        if event.text_area.id == "execution-continuation-feedback":
            self.query_one("#execution-continue", Button).disabled = (
                not event.text_area.text.strip()
            )

    @work(thread=True, exclusive=True)
    def _resume_execution(self, response: str) -> None:
        try:
            in_batch = self.controller.execution_in_live_batch(self.execution_id)
            if in_batch:
                self.controller.submit_response(self.execution_id, response)
            else:
                self.controller.respond_and_resume(self.execution_id, response)
        except Exception as exc:
            self.app.call_from_thread(
                self.notify, f"Could not resume workflow: {exc}", severity="error"
            )
            return
        self.app.call_from_thread(self._refresh_execution)
        if in_batch:
            self.app.call_from_thread(
                self.notify, "Response submitted; the task rejoined the batch queue."
            )

    @work(thread=True, exclusive=True)
    def _continue_execution(self, feedback: str) -> None:
        try:
            in_batch = self.controller.execution_in_live_batch(self.execution_id)
            if in_batch:
                self.controller.submit_continuation(self.execution_id, feedback)
            else:
                self.controller.continue_execution(self.execution_id, feedback)
        except Exception as exc:
            self.app.call_from_thread(
                self.notify, f"Could not continue execution: {exc}", severity="error"
            )
            self.app.call_from_thread(self._reenable_continue_button)
            return
        self.app.call_from_thread(self._refresh_execution)
        if in_batch:
            self.app.call_from_thread(
                self.notify, "Continuation submitted; the task rejoined the batch queue."
            )

    def _reenable_continue_button(self) -> None:
        self.query_one("#execution-continue", Button).disabled = False

    def _render_execution(
        self,
        execution: WorkflowExecution,
        steps: list[WorkflowStep],
    ) -> None:
        self._last_steps = steps
        status = self.query_one("#execution-status", Static)
        cancellation_pending = (
            execution.status in ACTIVE_WORKFLOW_STATUSES
            or execution.status is WorkflowStatus.NEEDS_HUMAN
        ) and execution.cancellation_requested_at is not None
        label = execution_status_label(execution)
        if str(status.renderable) != label:
            status.update(label)
        stage_label = (
            f"Stage: {execution.current_stage.value}"
            if execution.current_stage is not None else "Stage: Not started"
        )
        stage_widget = self.query_one("#execution-stage", Static)
        if str(stage_widget.renderable) != stage_label:
            stage_widget.update(stage_label)
        lines = step_timeline_lines(steps, execution.human_interactions)
        if execution.status in {
            WorkflowStatus.PENDING, WorkflowStatus.WAITING, WorkflowStatus.NEEDS_HUMAN
        } or (execution.status is WorkflowStatus.RUNNING and not lines):
            lines.append(status_message(execution.status))
        timeline = "\n".join(lines)
        if timeline != self._displayed_timeline:
            self.query_one("#execution-timeline", Static).update(timeline)
            self._displayed_timeline = timeline
        status.set_class(
            cancellation_pending or execution.status is WorkflowStatus.CANCELLED, "cancelled"
        )
        active = (
            execution.status in ACTIVE_WORKFLOW_STATUSES
            or execution.status is WorkflowStatus.NEEDS_HUMAN
        )
        if active and self._reconcile_timer is None:
            self._reconcile_timer = self.set_interval(2.0, self._refresh_execution)
        elif not active and self._reconcile_timer is not None:
            self._reconcile_timer.stop()
            self._reconcile_timer = None
        cancel = self.query_one("#execution-cancel", Button)
        cancel.display = (
            execution.status is WorkflowStatus.NEEDS_HUMAN
            or (execution.status in ACTIVE_WORKFLOW_STATUSES and not cancellation_pending)
        )
        cancel.disabled = False
        self._links = {
            "open-task": execution.task_url,
            "open-pr": execution.pr_url or "",
            "open-spec-session": execution.spec_jules_session_url or "",
            "open-review-session": execution.review_jules_session_url or "",
        }
        for button_id, url in self._links.items():
            self.query_one(f"#{button_id}", Button).disabled = not bool(url)

        interaction = (
            next(
                (item for item in reversed(execution.human_interactions) if item.awaiting_response),
                None,
            )
            if execution.status is WorkflowStatus.NEEDS_HUMAN
            and execution.cancellation_requested_at is None
            else None
        )
        for widget_id in (
            "execution-human-title",
            "execution-human-prompt",
            "execution-human-response",
            "execution-human-actions",
        ):
            self.query_one(f"#{widget_id}").display = interaction is not None
        if interaction is not None:
            role = {
                "GENERATE_SPEC": "Planner",
                "REVIEW": "Reviewer",
            }.get(
                interaction.requesting_stage.value, interaction.requesting_stage.value.title()
            )
            if self._interaction_id != interaction.id:
                self.query_one("#execution-human-title", Static).update(
                    f"{role} needs clarification"
                )
                self.query_one("#execution-human-prompt", Static).update(interaction.prompt)
                self.query_one("#execution-human-response", TextArea).text = ""
                self.query_one("#execution-resume", Button).disabled = True
            self._interaction_id = interaction.id
        else:
            self._interaction_id = None

        continuation_stage = self.controller.continuation_stage(execution)
        continuation_eligible = continuation_stage is not None
        for widget_id in (
            "execution-continuation-title",
            "execution-continuation-feedback",
            "execution-continuation-actions",
        ):
            self.query_one(f"#{widget_id}").display = continuation_eligible
        if continuation_eligible and not self._continuation_seeded:
            feedback = self.query_one("#execution-continuation-feedback", TextArea)
            role = "planner" if continuation_stage is WorkflowStage.GENERATE_SPEC else "implementer"
            self.query_one("#execution-continuation-title", Static).update(
                f"Edit the feedback sent to a fresh {role} invocation:"
            )
            try:
                feedback.text = self.controller.continuation_feedback(execution.id)
                self.query_one("#execution-continue", Button).disabled = not feedback.text.strip()
            except Exception as exc:
                feedback.text = ""
                self.query_one("#execution-continue", Button).disabled = True
                self.notify(f"Could not prepare continuation feedback: {exc}", severity="error")
            self._continuation_seeded = True

        details = {
            "execution": asdict(execution),
            "steps": [asdict(step) for step in steps],
            "links": {
                key.removeprefix("open-"): value for key, value in self._links.items() if value
            },
        }
        rendered = json.dumps(redact_payload(details), ensure_ascii=False, indent=2, default=str)
        if rendered != self._displayed_details:
            self.query_one("#execution-content", Static).update(
                Text(rendered, no_wrap=True, overflow="ignore")
            )
            self._displayed_details = rendered

    def _copy_detailed_logs(self) -> None:
        try:
            content = format_diagnostics_for_export(
                self.controller.execution_diagnostics(self.execution_id)
            )
        except Exception as exc:
            self.notify(f"Could not read detailed logs: {exc}", severity="error")
            return
        if not content:
            self.notify("No detailed logs are available to copy.", severity="warning")
            return
        try:
            self.app.copy_to_clipboard(content)
        except Exception as exc:
            self.notify(f"Could not copy detailed logs: {exc}", severity="error")
            return
        self.notify("Detailed logs copied to clipboard.")
