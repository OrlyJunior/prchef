"""Read-side batch progress projection, shared by live and persisted views.

This is deliberately a presentation projection, not a second workflow state
machine: it only reshapes durable batch items, workflow observer events, and
persisted execution/step rows into one ordered, per-position view. Workflow
stage transitions and provider-capacity policy remain owned by
``WorkflowService`` and ``ProviderConcurrencyCoordinator``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from orchestrator.models.agent import AgentRole
from orchestrator.models.execution_diagnostic import DiagnosticCategory, ExecutionDiagnostic
from orchestrator.models.workflow_batch import WorkflowBatch, WorkflowBatchItem
from orchestrator.models.workflow_event import WorkflowEvent, WorkflowEventKind
from orchestrator.models.workflow_execution import WorkflowExecution
from orchestrator.models.workflow_status import (
    ACTIVE_WORKFLOW_STATUSES,
    StepStatus,
    WorkflowStage,
    WorkflowStatus,
)
from orchestrator.models.workflow_step import WorkflowStep
from orchestrator.runtime_logging import redact_payload
from orchestrator.services.workflow_service import WorkflowService
from orchestrator.tui.workflow_presentation import (
    STAGE_ROLES,
    clear_index_progress,
    is_active_index_line,
    is_index_progress,
    step_timeline_lines,
    timeline_message,
    update_human_blocked_line,
)

if TYPE_CHECKING:
    from orchestrator.tui.bridge import WorkflowController

# Ordered so a summary line and per-category counts are always presented the
# same way, regardless of how many items land in each bucket.
SUMMARY_CATEGORIES: tuple[str, ...] = (
    "QUEUED",
    "WAITING",
    "RUNNING",
    "NEEDS_HUMAN",
    "APPROVED",
    "CHANGES_REQUESTED",
    "FAILED",
    "CANCELLED",
)

CANCELLABLE_ROW_STATUSES = ACTIVE_WORKFLOW_STATUSES | {WorkflowStatus.NEEDS_HUMAN}

SUMMARY_LABELS: dict[str, str] = {
    "QUEUED": "Queued",
    "WAITING": "Waiting for provider",
    "RUNNING": "Running",
    "NEEDS_HUMAN": "Needs human",
    "APPROVED": "Approved",
    "CHANGES_REQUESTED": "Changes requested",
    "FAILED": "Failed",
    "CANCELLED": "Cancelled",
}

# Mirrors the single-task live view's concise timeline: EXECUTION_CREATED and
# STATUS_CHANGED only update the current status label, not the timeline log.
_TIMELINE_EVENT_KINDS: frozenset[WorkflowEventKind] = frozenset(
    {
        WorkflowEventKind.STAGE_STARTED,
        WorkflowEventKind.STAGE_FINISHED,
        WorkflowEventKind.STAGE_FAILED,
        WorkflowEventKind.WORKFLOW_FINISHED,
        WorkflowEventKind.AGENT_FALLBACK,
        WorkflowEventKind.DIAGNOSTIC,
    }
)

_CATEGORY_FOR_STATUS: dict[WorkflowStatus, str] = {
    WorkflowStatus.PENDING: "QUEUED",
    WorkflowStatus.WAITING: "WAITING",
    WorkflowStatus.RUNNING: "RUNNING",
    WorkflowStatus.NEEDS_HUMAN: "NEEDS_HUMAN",
    WorkflowStatus.APPROVED: "APPROVED",
    WorkflowStatus.CHANGES_REQUESTED: "CHANGES_REQUESTED",
    WorkflowStatus.CANCELLED: "CANCELLED",
    WorkflowStatus.FAILED: "FAILED",
}


@dataclass(frozen=True)
class BatchRowView:
    """One batch item's presentation-ready state, live or persisted."""

    position: int
    task_id: str
    task_title: str
    execution_id: str | None
    category: str
    status_label: str
    stage_label: str
    timeline: tuple[str, ...] = ()
    held_by_provider: str | None = None
    action: str | None = None
    can_cancel: bool = False


def category_for_execution(execution: WorkflowExecution) -> str:
    return _CATEGORY_FOR_STATUS.get(execution.status, "FAILED")


def summarize(rows: list[BatchRowView]) -> dict[str, int]:
    counts = dict.fromkeys(SUMMARY_CATEGORIES, 0)
    for row in rows:
        counts[row.category] = counts.get(row.category, 0) + 1
    return counts


def summary_line(rows: list[BatchRowView]) -> str:
    counts = summarize(rows)
    return " · ".join(
        f"{SUMMARY_LABELS[category]}: {counts[category]}" for category in SUMMARY_CATEGORIES
    )


@dataclass
class _LiveItem:
    position: int
    task_id: str
    task_title: str
    execution_id: str | None = None
    status: WorkflowStatus | None = None
    stage: WorkflowStage | None = None
    provider_wait: str | None = None
    cancellation_requested: bool = False
    finished: bool = False
    timeline: list[str] = field(default_factory=list)
    active_stage_line: dict[WorkflowStage, int] = field(default_factory=dict)
    successful_stage_lines: dict[WorkflowStage, int] = field(default_factory=dict)
    stage_attempts: dict[WorkflowStage, int] = field(default_factory=dict)
    diagnostics: list[ExecutionDiagnostic] = field(default_factory=list)
    continuation_eligible: bool = False


def _live_status(item: _LiveItem) -> tuple[str, str]:
    if item.status is None:
        return "QUEUED", "QUEUED"
    if item.cancellation_requested and item.status in ACTIVE_WORKFLOW_STATUSES:
        return _CATEGORY_FOR_STATUS[item.status], "CANCELLATION REQUESTED"
    if item.status is WorkflowStatus.NEEDS_HUMAN:
        return "NEEDS_HUMAN", "AWAITING HUMAN"
    if item.status is WorkflowStatus.PENDING:
        return "QUEUED", "QUEUED"
    if item.status is WorkflowStatus.WAITING:
        label = (
            f"WAITING FOR AGENT ({item.provider_wait})"
            if item.provider_wait
            else "WAITING FOR AGENT"
        )
        return "WAITING", label
    return _CATEGORY_FOR_STATUS[item.status], item.status.value


class LiveBatchProgress:
    """Event-driven per-position projection for a batch that is still running."""

    def __init__(
        self,
        batch: WorkflowBatch,
        *,
        role_providers: Mapping[AgentRole, str] | None = None,
        role_models: Mapping[AgentRole, str | None] | None = None,
        required_providers: frozenset[str] = frozenset(),
    ) -> None:
        self.batch_id = batch.id
        self._items = [
            _LiveItem(item.position, item.task_id, item.task_title, item.execution_id)
            for item in batch.items
        ]
        self._by_execution: dict[str, int] = {
            item.execution_id: item.position for item in self._items if item.execution_id
        }
        self._role_providers = dict(role_providers or {})
        self._role_models = dict(role_models or {})
        self._required_providers = required_providers
        self._unavailable: set[str] = set()

    @property
    def held_provider(self) -> str | None:
        return next(iter(sorted(self._required_providers & self._unavailable)), None)

    def set_unavailable_providers(self, providers: set[str]) -> None:
        self._unavailable = set(providers)

    def set_continuation_eligible(self, position: int, eligible: bool) -> None:
        self._items[position].continuation_eligible = eligible

    def held_count(self, provider: str) -> int:
        if provider not in self._required_providers:
            return 0
        return sum(
            item.status in (None, WorkflowStatus.PENDING) for item in self._items
        )

    def position_for_execution(self, execution_id: str) -> int | None:
        return self._by_execution.get(execution_id)

    def execution_id_for(self, position: int) -> str | None:
        if not (0 <= position < len(self._items)):
            return None
        return self._items[position].execution_id

    def mark_cancellation_requested(self, execution_id: str) -> None:
        position = self._by_execution.get(execution_id)
        if position is not None:
            self._items[position].cancellation_requested = True

    def reconcile_execution(
        self, position: int, execution: WorkflowExecution, steps: list[WorkflowStep] | None = None
    ) -> bool:
        """Bring one live row up to date after another screen changed durable state."""

        if not (0 <= position < len(self._items)):
            return False
        item = self._items[position]
        if item.execution_id not in (None, execution.id):
            return False
        if (
            item.status is WorkflowStatus.CANCELLED
            and execution.status is not WorkflowStatus.CANCELLED
        ):
            return False
        item.execution_id = execution.id
        self._by_execution[execution.id] = position
        item.status = execution.status
        item.stage = execution.current_stage
        item.cancellation_requested = (
            execution.status in ACTIVE_WORKFLOW_STATUSES
            and execution.cancellation_requested_at is not None
        )
        if execution.status is not WorkflowStatus.WAITING:
            item.provider_wait = None
        if steps is not None:
            supplemental = [
                line for line in item.timeline
                if line.startswith((
                    "! docs/PROJECT_CONTEXT.md",
                    "! Semantic context unavailable",
                    "Provider ",
                    "↳",
                ))
            ]
            if (
                steps
                and steps[-1].stage is WorkflowStage.GENERATE_SPEC
                and steps[-1].status is StepStatus.RUNNING
                and execution.status in ACTIVE_WORKFLOW_STATUSES
            ):
                supplemental.extend(
                    line for line in item.timeline if line.startswith("Semantic index: ")
                )
            item.timeline = step_timeline_lines(steps, execution.human_interactions) + supplemental
            item.active_stage_line.clear()
            item.successful_stage_lines.clear()
            item.stage_attempts.clear()
            for index, step in enumerate(steps):
                item.stage_attempts[step.stage] = item.stage_attempts.get(step.stage, 0) + 1
                if step.status is StepStatus.RUNNING:
                    item.active_stage_line[step.stage] = index
                elif (
                    step.status is StepStatus.SUCCEEDED
                    and "waiting for human input" not in item.timeline[index]
                ):
                    item.successful_stage_lines[step.stage] = index
        return True

    def apply(self, event: WorkflowEvent) -> int | None:
        """Update state for one event; return the affected position, if any."""

        payload = event.payload or {}
        position = payload.get("batch_position")
        if not isinstance(position, int):
            position = self._by_execution.get(event.execution_id)
        if position is None or not (0 <= position < len(self._items)):
            return None
        item = self._items[position]

        if event.kind is WorkflowEventKind.EXECUTION_CREATED:
            item.execution_id = event.execution_id
            self._by_execution[event.execution_id] = position
            item.status = WorkflowStatus.PENDING
            item.stage = None
            item.provider_wait = None
            item.cancellation_requested = False
            item.finished = False
            item.timeline.clear()
            item.active_stage_line.clear()
            item.successful_stage_lines.clear()
            item.stage_attempts.clear()
            item.diagnostics.clear()
        elif event.execution_id != item.execution_id:
            # A stale event for a position whose item has since been replaced.
            return None

        if (
            item.status is WorkflowStatus.CANCELLED
            and event.status is not None
            and event.status is not WorkflowStatus.CANCELLED
        ):
            # Cancellation may settle after the service read its finish result
            # but before that result's observer event reaches the screen.
            return position

        if event.kind in (WorkflowEventKind.STATUS_CHANGED, WorkflowEventKind.WORKFLOW_FINISHED):
            item.status = event.status
            update_human_blocked_line(event, item.timeline, item.successful_stage_lines)
            if event.status is WorkflowStatus.WAITING:
                provider = payload.get("provider")
                item.provider_wait = provider if isinstance(provider, str) else None
            else:
                item.provider_wait = None
            if event.status is not None and event.status not in ACTIVE_WORKFLOW_STATUSES:
                item.cancellation_requested = False
        if event.kind is WorkflowEventKind.WORKFLOW_FINISHED:
            item.finished = True

        if event.kind in {
            WorkflowEventKind.STAGE_STARTED,
            WorkflowEventKind.STAGE_FINISHED,
            WorkflowEventKind.STAGE_FAILED,
            WorkflowEventKind.STATUS_CHANGED,
            WorkflowEventKind.WORKFLOW_FINISHED,
        }:
            clear_index_progress(item.timeline)

        message = timeline_message(event, item.stage_attempts, agent_context=self._agent_context)
        if is_index_progress(event) and (
            item.finished
            or (item.status is not None and item.status not in ACTIVE_WORKFLOW_STATUSES)
            or (item.stage is not None and item.stage is not WorkflowStage.GENERATE_SPEC)
            or (
                item.stage_attempts.get(WorkflowStage.GENERATE_SPEC, 0) > 0
                and WorkflowStage.GENERATE_SPEC not in item.active_stage_line
            )
        ):
            message = None
        if event.stage is not None and event.kind == WorkflowEventKind.STAGE_STARTED:
            item.successful_stage_lines.pop(event.stage, None)
            item.stage_attempts[event.stage] = item.stage_attempts.get(event.stage, 0) + 1
        if event.stage is not None and event.kind in {
            WorkflowEventKind.STAGE_STARTED,
            WorkflowEventKind.STAGE_FINISHED,
            WorkflowEventKind.STAGE_FAILED,
            WorkflowEventKind.STATUS_CHANGED,
            WorkflowEventKind.WORKFLOW_FINISHED,
        }:
            item.stage = event.stage
        if message is not None and event.kind in _TIMELINE_EVENT_KINDS:
            self._append_timeline(item, event, message)

        if event.kind is WorkflowEventKind.DIAGNOSTIC or event.kind in {
            WorkflowEventKind.STATUS_CHANGED,
            WorkflowEventKind.STAGE_STARTED,
            WorkflowEventKind.STAGE_FINISHED,
            WorkflowEventKind.STAGE_FAILED,
            WorkflowEventKind.AGENT_FALLBACK,
            WorkflowEventKind.WORKFLOW_FINISHED,
        }:
            item.diagnostics.append(self._diagnostic_from_event(event))

        return position

    def _append_timeline(self, item: _LiveItem, event: WorkflowEvent, message: str) -> None:
        if is_index_progress(event):
            for index, line in enumerate(item.timeline):
                if is_active_index_line(line):
                    item.timeline[index] = message
                    break
            else:
                item.timeline.append(message)
        elif event.kind is WorkflowEventKind.STAGE_STARTED and event.stage is not None:
            item.active_stage_line[event.stage] = len(item.timeline)
            item.timeline.append(message)
        elif (
            event.kind in {WorkflowEventKind.STAGE_FINISHED, WorkflowEventKind.STAGE_FAILED}
            and event.stage is not None
        ):
            active_line = item.active_stage_line.pop(event.stage, None)
            if active_line is None:
                active_line = len(item.timeline)
                item.timeline.append(message)
            else:
                item.timeline[active_line] = message
            if (
                event.kind is WorkflowEventKind.STAGE_FINISHED
                and event.step_status is StepStatus.SUCCEEDED
            ):
                item.successful_stage_lines[event.stage] = active_line
            else:
                item.successful_stage_lines.pop(event.stage, None)
        else:
            item.timeline.append(message)

    def _agent_context(self, stage: WorkflowStage) -> str:
        role = STAGE_ROLES.get(stage)
        if role is None:
            return ""
        provider = self._role_providers.get(role)
        if provider is None:
            return ""
        details = [provider]
        model = self._role_models.get(role)
        if model:
            details.append(model)
        return f" ({' · '.join(details)})"

    @staticmethod
    def _diagnostic_from_event(event: WorkflowEvent) -> ExecutionDiagnostic:
        if event.kind is WorkflowEventKind.DIAGNOSTIC:
            try:
                category = DiagnosticCategory(event.category or "")
            except ValueError:
                category = DiagnosticCategory.WORKFLOW_EVENT
            payload = event.payload or {}
        else:
            category = DiagnosticCategory.WORKFLOW_EVENT
            payload = {
                "kind": event.kind.value,
                "status": event.status.value if event.status else None,
                "step_status": event.step_status.value if event.step_status else None,
                "message": event.message,
            }
        return ExecutionDiagnostic(
            execution_id=event.execution_id,
            category=category,
            payload=cast(dict[str, Any], redact_payload(payload)),
            stage=event.stage,
            occurred_at=event.occurred_at,
        )

    def diagnostics_for(self, position: int) -> list[ExecutionDiagnostic]:
        if not (0 <= position < len(self._items)):
            return []
        return list(self._items[position].diagnostics)

    def rows(self) -> list[BatchRowView]:
        rows: list[BatchRowView] = []
        for item in self._items:
            if item.execution_id is None:
                held = self.held_provider
                rows.append(
                    BatchRowView(
                        position=item.position,
                        task_id=item.task_id,
                        task_title=item.task_title,
                        execution_id=None,
                        category="QUEUED",
                        status_label=(f"QUEUED — {held} unavailable (quota)" if held else "QUEUED"),
                        stage_label="",
                        held_by_provider=held,
                    )
                )
                continue
            category, label = _live_status(item)
            held = self.held_provider if item.status is WorkflowStatus.PENDING else None
            if held:
                label = f"QUEUED — {held} unavailable (quota)"
            rows.append(
                BatchRowView(
                    position=item.position,
                    task_id=item.task_id,
                    task_title=item.task_title,
                    execution_id=item.execution_id,
                    category=category,
                    status_label=label,
                    held_by_provider=held,
                    action=(
                        "answer" if item.status is WorkflowStatus.NEEDS_HUMAN else
                        "continue" if item.status is WorkflowStatus.CHANGES_REQUESTED
                        or (item.status is WorkflowStatus.FAILED and item.continuation_eligible)
                        else None
                    ),
                    can_cancel=(
                        item.status in CANCELLABLE_ROW_STATUSES
                        and not item.cancellation_requested
                    ),
                    stage_label=(
                        item.stage.value.replace("_", " ").title() if item.stage else ""
                    ),
                    timeline=tuple(line for line in item.timeline if line),
                )
            )
        return rows

    def summary(self) -> dict[str, int]:
        return summarize(self.rows())


def persisted_row(
    item: WorkflowBatchItem, controller: WorkflowController, *, raise_on_error: bool = False
) -> BatchRowView:
    """Build one row from durable storage: for history, restart, and refresh.

    An unlinked item is QUEUED. Provider capacity is transient and
    process-local, so a persisted PENDING/WAITING row never claims an active
    hold here: that would misrepresent work that will not resume on its own
    after a restart.
    """

    if item.execution_id is None:
        return BatchRowView(
            position=item.position,
            task_id=item.task_id,
            task_title=item.task_title,
            execution_id=None,
            category="QUEUED",
            status_label="QUEUED",
            stage_label="",
        )
    # Imported lazily to avoid a hard import cycle at module load time; the
    # existing execution-history status wording is the single source of truth
    # both a persisted batch row and its linked execution detail screen use.
    from orchestrator.tui.screens.execution_history import execution_status_label

    try:
        execution, steps = controller.execution_detail(item.execution_id)
    except Exception as exc:
        if raise_on_error:
            raise
        return BatchRowView(
            position=item.position,
            task_id=item.task_id,
            task_title=item.task_title,
            execution_id=item.execution_id,
            category="FAILED",
            status_label=f"EXECUTION UNAVAILABLE: {exc}",
            stage_label="",
        )
    timeline = step_timeline_lines(steps, execution.human_interactions)
    diagnostics: list[ExecutionDiagnostic] = []
    diagnostics_method = getattr(controller, "execution_diagnostics", None)
    if callable(diagnostics_method):
        try:
            diagnostics = diagnostics_method(item.execution_id)
        except Exception:
            diagnostics = []
        for diagnostic in diagnostics:
            if diagnostic.payload.get("kind") in {
                "PROVIDER_QUOTA_EXHAUSTED",
                "PROJECT_CONTEXT_TRUNCATED",
                "REPOSITORY_CONTEXT_FALLBACK",
            }:
                message = timeline_message(
                    WorkflowEvent(
                        execution_id=item.execution_id,
                        kind=WorkflowEventKind.DIAGNOSTIC,
                        payload=diagnostic.payload,
                    ),
                    {},
                )
                if message:
                    timeline.append(message)
    return BatchRowView(
        position=item.position,
        task_id=item.task_id,
        task_title=item.task_title,
        execution_id=item.execution_id,
        category=category_for_execution(execution),
        status_label=execution_status_label(execution),
        stage_label=execution.current_stage.value.replace("_", " ").title(),
        timeline=tuple(timeline),
        action=(
            "answer" if (
                execution.status is WorkflowStatus.NEEDS_HUMAN
                and execution.cancellation_requested_at is None
            ) else
            "continue"
            if WorkflowService.continuation_stage(execution, lambda: diagnostics) is not None
            else None
        ),
        can_cancel=(
            execution.status in CANCELLABLE_ROW_STATUSES
            and (
                execution.status is WorkflowStatus.NEEDS_HUMAN
                or execution.cancellation_requested_at is None
            )
        ),
    )


def build_persisted_rows(
    batch: WorkflowBatch, controller: WorkflowController
) -> list[BatchRowView]:
    """Render every batch item from durable storage, in its stable order."""

    return [persisted_row(item, controller) for item in batch.items]
