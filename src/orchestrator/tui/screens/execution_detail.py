"""Detailed persisted workflow inspection screen."""

from __future__ import annotations

import json
import webbrowser
from dataclasses import asdict
from typing import TYPE_CHECKING, cast

from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.events import DescendantFocus
from textual.screen import Screen
from textual.widgets import Button, Static, TextArea

from orchestrator.models.execution_diagnostic import ExecutionDiagnostic
from orchestrator.models.workflow_execution import WorkflowExecution
from orchestrator.models.workflow_status import ACTIVE_WORKFLOW_STATUSES, WorkflowStatus
from orchestrator.models.workflow_step import WorkflowStep
from orchestrator.runtime_logging import redact_payload
from orchestrator.tui.bridge import WorkflowController
from orchestrator.tui.diagnostics import diagnostic_for_display, format_diagnostics_for_export
from orchestrator.tui.screens.execution_history import execution_status_label

if TYPE_CHECKING:
    from orchestrator.tui.app import OrchestratorApp


class ExecutionDetailScreen(Screen[None]):
    """Render persisted execution details and resume a pending human response."""

    BINDINGS = [("escape", "app.pop_screen", "Back")]

    def __init__(self, execution_id: str) -> None:
        super().__init__()
        self.execution_id = execution_id
        self._links: dict[str, str] = {}
        self._diagnostics: list[ExecutionDiagnostic] = []

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="execution-detail"):
            yield Static("Execution detail", classes="screen-title")
            yield Static("", id="execution-status")
            with Horizontal(classes="screen-actions"):
                yield Button("Open task", id="open-task")
                yield Button("Open PR", id="open-pr")
                yield Button("Open planner session", id="open-spec-session")
                yield Button("Open review session", id="open-review-session")
                yield Button(
                    "Copy detailed logs", id="copy-detailed-logs-button", disabled=True
                )
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
            yield Static("Loading execution…", id="execution-content", markup=False)

    @property
    def controller(self) -> WorkflowController:
        return cast("OrchestratorApp", self.app).controller

    def on_mount(self) -> None:
        try:
            execution, steps = self.controller.execution_detail(self.execution_id)
            diagnostics = self.controller.execution_diagnostics(self.execution_id)
        except Exception as exc:
            self.query_one("#execution-content", Static).update(
                f"Could not load execution {self.execution_id}: {exc}"
            )
            return
        self._render_execution(execution, steps, diagnostics)

    def on_descendant_focus(self, event: DescendantFocus) -> None:
        self.query_one("#execution-detail", VerticalScroll).scroll_to_widget(
            event.widget, animate=False
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
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
        url = self._links.get(event.button.id or "")
        if not url:
            self.notify("No related link was persisted for this execution.", severity="warning")
            return
        webbrowser.open(url)
        self.notify("Opened related resource in the default browser.")

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
                execution, _ = self.controller.execution_detail(self.execution_id)
            else:
                execution = self.controller.respond_and_resume(self.execution_id, response)
            _, steps = self.controller.execution_detail(self.execution_id)
            diagnostics = self.controller.execution_diagnostics(self.execution_id)
        except Exception as exc:
            self.app.call_from_thread(
                self.notify, f"Could not resume workflow: {exc}", severity="error"
            )
            return
        self.app.call_from_thread(self._render_execution, execution, steps, diagnostics)
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
                execution, _ = self.controller.execution_detail(self.execution_id)
            else:
                execution = self.controller.continue_execution(self.execution_id, feedback)
            _, steps = self.controller.execution_detail(self.execution_id)
            diagnostics = self.controller.execution_diagnostics(self.execution_id)
        except Exception as exc:
            self.app.call_from_thread(
                self.notify, f"Could not continue execution: {exc}", severity="error"
            )
            self.app.call_from_thread(self._reenable_continue_button)
            return
        self.app.call_from_thread(self._render_execution, execution, steps, diagnostics)
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
        diagnostics: list[ExecutionDiagnostic],
    ) -> None:
        self._diagnostics = diagnostics
        self.query_one("#copy-detailed-logs-button", Button).disabled = not diagnostics
        status = self.query_one("#execution-status", Static)
        cancellation_pending = (
            execution.status in ACTIVE_WORKFLOW_STATUSES
            and execution.cancellation_requested_at is not None
        )
        status.update(execution_status_label(execution))
        status.set_class(
            cancellation_pending or execution.status is WorkflowStatus.CANCELLED, "cancelled"
        )
        self._links = {
            "open-task": execution.task_url,
            "open-pr": execution.pr_url or "",
            "open-spec-session": execution.spec_jules_session_url or "",
            "open-review-session": execution.review_jules_session_url or "",
        }
        for button_id, url in self._links.items():
            self.query_one(f"#{button_id}", Button).disabled = not bool(url)

        interaction = next(
            (item for item in reversed(execution.human_interactions) if item.awaiting_response),
            None,
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
            self.query_one("#execution-human-title", Static).update(
                f"{role} needs clarification"
            )
            self.query_one("#execution-human-prompt", Static).update(interaction.prompt)
            response = self.query_one("#execution-human-response", TextArea)
            response.text = ""
            self.query_one("#execution-resume", Button).disabled = True

        continuation_eligible = execution.status.value == "CHANGES_REQUESTED" or (
            execution.status.value == "FAILED"
            and execution.workspace_path is not None
            and execution.working_branch is not None
        )
        for widget_id in (
            "execution-continuation-title",
            "execution-continuation-feedback",
            "execution-continuation-actions",
        ):
            self.query_one(f"#{widget_id}").display = continuation_eligible
        if continuation_eligible:
            feedback = self.query_one("#execution-continuation-feedback", TextArea)
            self.query_one("#execution-continuation-title", Static).update(
                "Edit the feedback sent to a fresh implementer invocation:"
            )
            try:
                feedback.text = self.controller.continuation_feedback(execution.id)
                self.query_one("#execution-continue", Button).disabled = not feedback.text.strip()
            except Exception as exc:
                feedback.text = ""
                self.query_one("#execution-continue", Button).disabled = True
                self.notify(f"Could not prepare continuation feedback: {exc}", severity="error")

        details = {
            "execution": asdict(execution),
            "steps": [asdict(step) for step in steps],
            "detailed_logs": [diagnostic_for_display(item) for item in diagnostics],
            "links": {
                key.removeprefix("open-"): value for key, value in self._links.items() if value
            },
        }
        rendered = json.dumps(redact_payload(details), ensure_ascii=False, indent=2, default=str)
        self.query_one("#execution-content", Static).update(rendered)
        # The content Static has its natural height; refresh the owning page
        # so persisted diagnostics extend the main scroll range immediately.
        self.query_one("#execution-detail", VerticalScroll).refresh(layout=True)

    def _copy_detailed_logs(self) -> None:
        content = format_diagnostics_for_export(self._diagnostics)
        if not content:
            self.notify("No detailed logs are available to copy.", severity="warning")
            return
        try:
            self.app.copy_to_clipboard(content)
        except Exception as exc:
            self.notify(f"Could not copy detailed logs: {exc}", severity="error")
            return
        self.notify("Detailed logs copied to clipboard.")
