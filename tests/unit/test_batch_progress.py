"""Unit tests for the shared batch progress projection.

These exercise ``orchestrator.tui.batch_progress`` and
``orchestrator.tui.workflow_presentation`` directly, without driving a
Textual screen, so the event-to-row mapping stays fast and deterministic.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from orchestrator.models.agent import AgentRole
from orchestrator.models.execution_diagnostic import DiagnosticCategory, ExecutionDiagnostic
from orchestrator.models.human_interaction import HumanInteraction
from orchestrator.models.workflow_batch import WorkflowBatch, WorkflowBatchItem
from orchestrator.models.workflow_event import WorkflowEvent, WorkflowEventKind
from orchestrator.models.workflow_execution import WorkflowExecution
from orchestrator.models.workflow_status import StepStatus, WorkflowStage, WorkflowStatus
from orchestrator.models.workflow_step import WorkflowStep
from orchestrator.tui.batch_progress import (
    LiveBatchProgress,
    build_persisted_rows,
    category_for_execution,
    persisted_row,
    summarize,
)
from orchestrator.tui.workflow_presentation import step_timeline_lines


def _batch(positions: int = 3) -> WorkflowBatch:
    return WorkflowBatch(
        id="batch-1",
        repository="example",
        task_source="github",
        items=tuple(
            WorkflowBatchItem(position, f"task-{position}", f"Task {position}")
            for position in range(positions)
        ),
    )


def _status_event(
    execution_id: str,
    status: WorkflowStatus,
    *,
    position: int,
    provider: str | None = None,
    kind: WorkflowEventKind = WorkflowEventKind.STATUS_CHANGED,
) -> WorkflowEvent:
    payload: dict[str, Any] = {"batch_position": position, "batch_size": 3}
    if provider is not None:
        payload["provider"] = provider
    return WorkflowEvent(execution_id=execution_id, kind=kind, status=status, payload=payload)


class _Controller:
    """Minimal stand-in exposing only what ``persisted_row`` needs."""

    def __init__(self) -> None:
        self.executions: dict[str, tuple[WorkflowExecution, list[WorkflowStep]]] = {}
        self.errors: dict[str, Exception] = {}
        self.diagnostics: dict[str, list[ExecutionDiagnostic]] = {}

    def execution_detail(self, execution_id: str) -> tuple[WorkflowExecution, list[WorkflowStep]]:
        if execution_id in self.errors:
            raise self.errors[execution_id]
        return self.executions[execution_id]

    def execution_diagnostics(self, execution_id: str) -> list[ExecutionDiagnostic]:
        return self.diagnostics.get(execution_id, [])


def test_unlinked_items_render_as_queued_in_original_order() -> None:
    progress = LiveBatchProgress(_batch(3))

    rows = progress.rows()

    assert [(row.position, row.category, row.status_label) for row in rows] == [
        (0, "QUEUED", "QUEUED"),
        (1, "QUEUED", "QUEUED"),
        (2, "QUEUED", "QUEUED"),
    ]
    assert summarize(rows)["QUEUED"] == 3
    assert all(not row.can_cancel for row in rows)


@pytest.mark.parametrize(
    "status",
    [WorkflowStatus.PENDING, WorkflowStatus.WAITING, WorkflowStatus.RUNNING,
     WorkflowStatus.NEEDS_HUMAN],
)
def test_linked_open_rows_offer_cancel_in_live_and_persisted_views(
    status: WorkflowStatus,
) -> None:
    progress = LiveBatchProgress(_batch(1))
    progress.apply(WorkflowEvent(
        execution_id="execution-1", kind=WorkflowEventKind.EXECUTION_CREATED,
        payload={"batch_position": 0},
    ))
    progress.apply(_status_event("execution-1", status, position=0))
    live_row = progress.rows()[0]
    assert live_row.can_cancel
    assert live_row.action == ("answer" if status is WorkflowStatus.NEEDS_HUMAN else None)

    controller = _Controller()
    controller.executions["execution-1"] = (
        WorkflowExecution(id="execution-1", status=status), []
    )
    item = WorkflowBatchItem(0, "task-0", "Task 0", "execution-1")
    persisted = persisted_row(item, controller)  # type: ignore[arg-type]
    assert persisted.can_cancel
    assert persisted.action == live_row.action


@pytest.mark.parametrize(
    "status",
    [
        WorkflowStatus.APPROVED,
        WorkflowStatus.CHANGES_REQUESTED,
        WorkflowStatus.FAILED,
        WorkflowStatus.CANCELLED,
    ],
)
def test_terminal_rows_do_not_offer_cancel(status: WorkflowStatus) -> None:
    progress = LiveBatchProgress(_batch(1))
    progress.apply(WorkflowEvent(
        execution_id="execution-1", kind=WorkflowEventKind.EXECUTION_CREATED,
        payload={"batch_position": 0},
    ))
    progress.apply(_status_event("execution-1", status, position=0))
    assert not progress.rows()[0].can_cancel

    controller = _Controller()
    controller.executions["execution-1"] = (
        WorkflowExecution(id="execution-1", status=status), []
    )
    item = WorkflowBatchItem(0, "task-0", "Task 0", "execution-1")
    persisted = persisted_row(item, controller)  # type: ignore[arg-type]
    assert not persisted.can_cancel


def test_running_cancellation_request_stays_pending_until_terminal_event() -> None:
    progress = LiveBatchProgress(_batch(1))
    progress.apply(WorkflowEvent(
        execution_id="execution-1", kind=WorkflowEventKind.EXECUTION_CREATED,
        payload={"batch_position": 0},
    ))
    progress.apply(_status_event("execution-1", WorkflowStatus.RUNNING, position=0))
    progress.mark_cancellation_requested("execution-1")
    row = progress.rows()[0]
    assert row.category == "RUNNING"
    assert row.status_label == "CANCELLATION REQUESTED"
    assert not row.can_cancel

    progress.apply(_status_event("execution-1", WorkflowStatus.CANCELLED, position=0))
    assert progress.summary()["CANCELLED"] == 1

    controller = _Controller()
    controller.executions["execution-1"] = (
        WorkflowExecution(
            id="execution-1", status=WorkflowStatus.RUNNING,
            cancellation_requested_at=datetime.now(UTC),
        ), []
    )
    item = WorkflowBatchItem(0, "task-0", "Task 0", "execution-1")
    persisted = persisted_row(item, controller)  # type: ignore[arg-type]
    assert persisted.status_label == "CANCELLATION REQUESTED"
    assert not persisted.can_cancel


def test_legacy_human_row_with_cancellation_marker_offers_recovery_cancel() -> None:
    controller = _Controller()
    controller.executions["execution-1"] = (
        WorkflowExecution(
            id="execution-1",
            status=WorkflowStatus.NEEDS_HUMAN,
            cancellation_requested_at=datetime.now(UTC),
        ), []
    )

    row = persisted_row(
        WorkflowBatchItem(0, "task-0", "Task 0", "execution-1"),
        controller,  # type: ignore[arg-type]
    )

    assert row.category == "NEEDS_HUMAN"
    assert row.status_label == "CANCELLATION REQUESTED"
    assert row.action is None
    assert row.can_cancel


def test_late_finish_event_does_not_reopen_cancelled_row() -> None:
    progress = LiveBatchProgress(_batch(1))
    progress.apply(WorkflowEvent(
        execution_id="execution-1", kind=WorkflowEventKind.EXECUTION_CREATED,
        payload={"batch_position": 0},
    ))
    progress.apply(_status_event("execution-1", WorkflowStatus.CANCELLED, position=0))

    progress.apply(_status_event(
        "execution-1", WorkflowStatus.NEEDS_HUMAN, position=0,
        kind=WorkflowEventKind.WORKFLOW_FINISHED,
    ))

    assert progress.rows()[0].category == "CANCELLED"
    assert progress.summary()["CANCELLED"] == 1


def test_execution_created_links_the_row_and_resets_its_timeline() -> None:
    progress = LiveBatchProgress(_batch(2))

    position = progress.apply(
        WorkflowEvent(
            execution_id="execution-1",
            kind=WorkflowEventKind.EXECUTION_CREATED,
            payload={"batch_position": 0},
        )
    )

    assert position == 0
    rows = progress.rows()
    assert rows[0].execution_id == "execution-1"
    assert rows[0].category == "QUEUED"
    assert rows[1].execution_id is None


def test_waiting_event_does_not_hold_queued_rows() -> None:
    progress = LiveBatchProgress(_batch(3), required_providers=frozenset({"jules"}))
    progress.apply(
        WorkflowEvent(
            execution_id="execution-1",
            kind=WorkflowEventKind.EXECUTION_CREATED,
            payload={"batch_position": 0},
        )
    )
    progress.apply(
        _status_event("execution-1", WorkflowStatus.WAITING, position=0, provider="jules")
    )

    rows = progress.rows()
    assert rows[0].category == "WAITING"
    assert rows[0].status_label == "WAITING FOR AGENT (jules)"
    assert rows[1].held_by_provider is None
    assert rows[1].status_label == "QUEUED"
    assert rows[2].held_by_provider is None


def test_quota_hold_clears_when_provider_is_available() -> None:
    progress = LiveBatchProgress(_batch(2), required_providers=frozenset({"jules"}))
    progress.apply(
        WorkflowEvent(
            execution_id="execution-1",
            kind=WorkflowEventKind.EXECUTION_CREATED,
            payload={"batch_position": 0},
        )
    )
    progress.apply(
        _status_event("execution-1", WorkflowStatus.WAITING, position=0, provider="jules")
    )
    progress.set_unavailable_providers({"jules"})

    rows = progress.rows()
    assert rows[0].category == "WAITING"
    assert rows[1].held_by_provider == "jules"
    progress.set_unavailable_providers(set())
    rows = progress.rows()
    assert rows[1].held_by_provider is None
    assert rows[1].status_label == "QUEUED"


def test_quota_hold_survives_other_item_terminal_status() -> None:
    progress = LiveBatchProgress(_batch(2), required_providers=frozenset({"jules"}))
    progress.apply(
        WorkflowEvent(
            execution_id="execution-1",
            kind=WorkflowEventKind.EXECUTION_CREATED,
            payload={"batch_position": 0},
        )
    )
    progress.apply(
        _status_event("execution-1", WorkflowStatus.WAITING, position=0, provider="jules")
    )
    progress.set_unavailable_providers({"jules"})
    progress.apply(
        _status_event(
            "execution-1",
            WorkflowStatus.FAILED,
            position=0,
            kind=WorkflowEventKind.WORKFLOW_FINISHED,
        )
    )

    assert progress.rows()[1].held_by_provider == "jules"


def test_quota_timeline_and_batch_actions_survive_resume() -> None:
    progress = LiveBatchProgress(_batch(2), required_providers=frozenset({"jules"}))
    progress.apply(
        WorkflowEvent(
            execution_id="execution-1",
            kind=WorkflowEventKind.EXECUTION_CREATED,
            payload={"batch_position": 0},
        )
    )
    progress.apply(
        WorkflowEvent(
            execution_id="execution-1",
            kind=WorkflowEventKind.DIAGNOSTIC,
            payload={
                "batch_position": 0,
                "kind": "PROVIDER_QUOTA_EXHAUSTED",
                "provider": "jules",
            },
        )
    )
    progress.apply(_status_event("execution-1", WorkflowStatus.NEEDS_HUMAN, position=0))
    assert progress.rows()[0].action == "answer"
    progress.apply(_status_event("execution-1", WorkflowStatus.PENDING, position=0))
    assert progress.rows()[0].action is None
    assert "Provider jules quota exhausted" in progress.rows()[0].timeline[0]
    progress.set_continuation_eligible(0, True)
    progress.apply(_status_event("execution-1", WorkflowStatus.FAILED, position=0))
    assert progress.rows()[0].action == "continue"


@pytest.mark.parametrize(
    ("status", "category", "label"),
    [
        (WorkflowStatus.NEEDS_HUMAN, "NEEDS_HUMAN", "AWAITING HUMAN"),
        (WorkflowStatus.APPROVED, "APPROVED", "APPROVED"),
        (WorkflowStatus.CHANGES_REQUESTED, "CHANGES_REQUESTED", "CHANGES_REQUESTED"),
        (WorkflowStatus.FAILED, "FAILED", "FAILED"),
        (WorkflowStatus.CANCELLED, "CANCELLED", "CANCELLED"),
    ],
)
def test_terminal_and_needs_human_status_mapping(
    status: WorkflowStatus, category: str, label: str
) -> None:
    progress = LiveBatchProgress(_batch(1))
    progress.apply(
        WorkflowEvent(
            execution_id="execution-1",
            kind=WorkflowEventKind.EXECUTION_CREATED,
            payload={"batch_position": 0},
        )
    )
    progress.apply(
        _status_event(
            "execution-1", status, position=0, kind=WorkflowEventKind.WORKFLOW_FINISHED
        )
    )

    row = progress.rows()[0]
    assert row.category == category
    assert row.status_label == label


def test_cancellation_requested_label_clears_once_a_terminal_status_arrives() -> None:
    progress = LiveBatchProgress(_batch(1))
    progress.apply(
        WorkflowEvent(
            execution_id="execution-1",
            kind=WorkflowEventKind.EXECUTION_CREATED,
            payload={"batch_position": 0},
        )
    )
    progress.apply(_status_event("execution-1", WorkflowStatus.RUNNING, position=0))
    progress.mark_cancellation_requested("execution-1")

    assert progress.rows()[0].status_label == "CANCELLATION REQUESTED"

    progress.apply(
        _status_event(
            "execution-1",
            WorkflowStatus.CANCELLED,
            position=0,
            kind=WorkflowEventKind.WORKFLOW_FINISHED,
        )
    )

    row = progress.rows()[0]
    assert row.status_label == "CANCELLED"
    assert row.category == "CANCELLED"


def test_interleaved_events_only_update_their_own_position() -> None:
    progress = LiveBatchProgress(_batch(2))
    progress.apply(
        WorkflowEvent(
            execution_id="execution-1",
            kind=WorkflowEventKind.EXECUTION_CREATED,
            payload={"batch_position": 0},
        )
    )
    progress.apply(
        WorkflowEvent(
            execution_id="execution-2",
            kind=WorkflowEventKind.EXECUTION_CREATED,
            payload={"batch_position": 1},
        )
    )

    changed = progress.apply(
        WorkflowEvent(
            execution_id="execution-2",
            kind=WorkflowEventKind.STAGE_STARTED,
            stage=WorkflowStage.IMPLEMENT,
            payload={"batch_position": 1},
        )
    )

    assert changed == 1
    rows = progress.rows()
    assert rows[0].timeline == ()
    assert rows[1].timeline and "Implementing changes" in rows[1].timeline[0]


def test_stage_started_and_finished_collapse_into_one_timeline_line() -> None:
    progress = LiveBatchProgress(_batch(1))
    progress.apply(
        WorkflowEvent(
            execution_id="execution-1",
            kind=WorkflowEventKind.EXECUTION_CREATED,
            payload={"batch_position": 0},
        )
    )
    progress.apply(
        WorkflowEvent(
            execution_id="execution-1",
            kind=WorkflowEventKind.STAGE_STARTED,
            stage=WorkflowStage.VALIDATE,
            payload={"batch_position": 0},
        )
    )
    progress.apply(
        WorkflowEvent(
            execution_id="execution-1",
            kind=WorkflowEventKind.STAGE_FINISHED,
            stage=WorkflowStage.VALIDATE,
            payload={"batch_position": 0},
        )
    )

    timeline = progress.rows()[0].timeline
    assert len(timeline) == 1
    assert timeline[0] == "✓ Validation passed"


def test_diagnostics_accumulate_for_copy_and_display() -> None:
    progress = LiveBatchProgress(_batch(1))
    progress.apply(
        WorkflowEvent(
            execution_id="execution-1",
            kind=WorkflowEventKind.EXECUTION_CREATED,
            payload={"batch_position": 0},
        )
    )
    progress.apply(
        WorkflowEvent(
            execution_id="execution-1",
            kind=WorkflowEventKind.DIAGNOSTIC,
            category=DiagnosticCategory.REQUEST.value,
            payload={"batch_position": 0, "effective_instructions": "do the thing"},
        )
    )

    diagnostics = progress.diagnostics_for(0)
    assert len(diagnostics) == 1
    assert diagnostics[0].category is DiagnosticCategory.REQUEST
    assert diagnostics[0].payload["effective_instructions"] == "do the thing"


def test_agent_context_names_the_shared_provider_and_model() -> None:
    progress = LiveBatchProgress(
        _batch(1),
        role_providers={AgentRole.IMPLEMENTER: "codex"},
        role_models={AgentRole.IMPLEMENTER: "gpt-batch"},
    )
    progress.apply(
        WorkflowEvent(
            execution_id="execution-1",
            kind=WorkflowEventKind.EXECUTION_CREATED,
            payload={"batch_position": 0},
        )
    )
    progress.apply(
        WorkflowEvent(
            execution_id="execution-1",
            kind=WorkflowEventKind.STAGE_STARTED,
            stage=WorkflowStage.IMPLEMENT,
            payload={"batch_position": 0},
        )
    )

    assert "(codex · gpt-batch)" in progress.rows()[0].timeline[0]


def test_persisted_row_for_unlinked_item_is_queued() -> None:
    controller = _Controller()
    item = WorkflowBatchItem(0, "task-1", "Task 1")
    row = persisted_row(item, controller)  # type: ignore[arg-type]
    assert not row.can_cancel

    assert row.category == "QUEUED"
    assert row.status_label == "QUEUED"
    assert row.execution_id is None


def test_persisted_row_uses_execution_status_and_step_timeline() -> None:
    controller = _Controller()
    execution = WorkflowExecution(
        id="execution-1",
        status=WorkflowStatus.CHANGES_REQUESTED,
        current_stage=WorkflowStage.REVIEW,
    )
    now = datetime.now(UTC)
    steps = [
        WorkflowStep(1, "execution-1", WorkflowStage.IMPLEMENT, StepStatus.SUCCEEDED, now),
        WorkflowStep(
            2, "execution-1", WorkflowStage.REVIEW, StepStatus.FAILED, now, error_message="boom"
        ),
    ]
    controller.executions["execution-1"] = (execution, steps)
    item = WorkflowBatchItem(0, "task-1", "Task 1", "execution-1")

    row = persisted_row(item, controller)  # type: ignore[arg-type]

    assert row.category == "CHANGES_REQUESTED"
    assert row.status_label == "CHANGES_REQUESTED"
    assert row.stage_label == "Review"
    assert row.action == "continue"
    assert row.timeline == ("✓ Changes implemented", "✗ Reviewing pull request failed: boom")


def test_persisted_failed_row_only_offers_continue_with_retained_workspace() -> None:
    controller = _Controller()
    execution = WorkflowExecution(id="execution-1", status=WorkflowStatus.FAILED)
    controller.executions[execution.id] = (execution, [])
    item = WorkflowBatchItem(0, "task-1", "Task 1", execution.id)
    assert persisted_row(item, controller).action is None  # type: ignore[arg-type]

    execution.workspace_path = "worktree"
    execution.working_branch = "branch"
    execution.spec_snapshot = {"title": "Specification"}
    assert persisted_row(item, controller).action == "continue"  # type: ignore[arg-type]


def test_persisted_row_offers_continue_after_planning_quota_failure() -> None:
    controller = _Controller()
    execution = WorkflowExecution(id="execution-1", status=WorkflowStatus.FAILED)
    controller.executions[execution.id] = (execution, [])
    item = WorkflowBatchItem(0, "task-1", "Task 1", execution.id)
    controller.diagnostics[execution.id] = [
        ExecutionDiagnostic(
            execution_id=execution.id,
            category=DiagnosticCategory.WORKFLOW_EVENT,
            payload={"kind": "PROVIDER_QUOTA_EXHAUSTED", "provider": "jules"},
        )
    ]

    assert persisted_row(item, controller).action == "continue"  # type: ignore[arg-type]


def test_persisted_row_reports_unavailable_execution_without_losing_the_task() -> None:
    controller = _Controller()
    controller.errors["execution-1"] = RuntimeError("database unavailable")
    item = WorkflowBatchItem(0, "task-1", "Task 1", "execution-1")

    row = persisted_row(item, controller)  # type: ignore[arg-type]

    assert row.execution_id == "execution-1"
    assert row.category == "FAILED"
    assert "database unavailable" in row.status_label


def test_build_persisted_rows_preserves_batch_order_for_a_mixed_batch() -> None:
    controller = _Controller()
    controller.executions["execution-1"] = (
        WorkflowExecution(id="execution-1", status=WorkflowStatus.APPROVED),
        [],
    )
    batch = WorkflowBatch(
        id="batch-1",
        repository="example",
        task_source="github",
        items=(
            WorkflowBatchItem(0, "task-0", "Task 0", "execution-1"),
            WorkflowBatchItem(1, "task-1", "Task 1", None),
        ),
    )

    rows = build_persisted_rows(batch, controller)  # type: ignore[arg-type]

    assert [row.position for row in rows] == [0, 1]
    assert rows[0].category == "APPROVED"
    assert rows[1].category == "QUEUED"


def test_category_for_execution_matches_summary_categories() -> None:
    for status in WorkflowStatus:
        execution = WorkflowExecution(id="execution-1", status=status)
        assert category_for_execution(execution) in {
            "QUEUED",
            "WAITING",
            "RUNNING",
            "NEEDS_HUMAN",
            "APPROVED",
            "CHANGES_REQUESTED",
            "FAILED",
            "CANCELLED",
        }


def test_step_timeline_lines_render_retry_labels_on_a_second_attempt() -> None:
    now = datetime.now(UTC)
    steps = [
        WorkflowStep(1, "execution-1", WorkflowStage.IMPLEMENT, StepStatus.SUCCEEDED, now),
        WorkflowStep(
            2, "execution-1", WorkflowStage.VALIDATE, StepStatus.FAILED, now, error_message="lint"
        ),
        WorkflowStep(3, "execution-1", WorkflowStage.IMPLEMENT, StepStatus.SUCCEEDED, now),
        WorkflowStep(4, "execution-1", WorkflowStage.VALIDATE, StepStatus.SUCCEEDED, now),
    ]

    lines = step_timeline_lines(steps)

    assert lines == [
        "✓ Changes implemented",
        "✗ Validating changes failed: lint",
        "✓ Implementing corrections",
        "✓ Validation passed",
    ]


@pytest.mark.parametrize("stage", [WorkflowStage.GENERATE_SPEC, WorkflowStage.REVIEW])
def test_live_human_blocked_stage_replaces_success_and_preserves_resumed_attempt(
    stage: WorkflowStage,
) -> None:
    progress = LiveBatchProgress(_batch(1))
    execution_id = "execution-1"
    progress.apply(WorkflowEvent(execution_id, WorkflowEventKind.EXECUTION_CREATED,
                                 payload={"batch_position": 0}))

    def stage_event(kind: WorkflowEventKind, status: StepStatus) -> WorkflowEvent:
        return WorkflowEvent(execution_id, kind, stage=stage, step_status=status)

    progress.apply(stage_event(WorkflowEventKind.STAGE_STARTED, StepStatus.RUNNING))
    progress.apply(stage_event(WorkflowEventKind.STAGE_FINISHED, StepStatus.SUCCEEDED))
    assert progress.rows()[0].timeline == (
        "✓ Specification generated" if stage is WorkflowStage.GENERATE_SPEC
        else "✓ Review completed",
    )
    progress.apply(WorkflowEvent(execution_id, WorkflowEventKind.STATUS_CHANGED,
                                 stage=stage, status=WorkflowStatus.NEEDS_HUMAN))
    waiting = (
        "! Generating specification — waiting for human input"
        if stage is WorkflowStage.GENERATE_SPEC
        else "! Reviewing pull request — waiting for human input"
    )
    assert progress.rows()[0].timeline == (waiting,)
    progress.apply(WorkflowEvent(execution_id, WorkflowEventKind.WORKFLOW_FINISHED,
                                 stage=stage, status=WorkflowStatus.NEEDS_HUMAN))
    assert progress.rows()[0].timeline[0] == waiting

    progress.apply(WorkflowEvent(execution_id, WorkflowEventKind.STATUS_CHANGED,
                                 status=WorkflowStatus.RUNNING))
    progress.apply(stage_event(WorkflowEventKind.STAGE_STARTED, StepStatus.RUNNING))
    progress.apply(stage_event(WorkflowEventKind.STAGE_FINISHED, StepStatus.SUCCEEDED))
    assert progress.rows()[0].timeline[-1] == (
        "✓ Specification generated" if stage is WorkflowStage.GENERATE_SPEC
        else "✓ Review completed"
    )
    assert progress.rows()[0].timeline[0] == waiting


@pytest.mark.parametrize("stage", [WorkflowStage.GENERATE_SPEC, WorkflowStage.REVIEW])
def test_persisted_human_blocked_stage_and_resumed_attempt(stage: WorkflowStage) -> None:
    controller = _Controller()
    execution = WorkflowExecution(id="execution-1", status=WorkflowStatus.NEEDS_HUMAN,
                                  current_stage=stage)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    first = WorkflowStep(1, execution.id, stage, StepStatus.SUCCEEDED, start,
                         start + timedelta(seconds=1))
    execution.human_interactions = [HumanInteraction(
        1, execution.id, stage, "Clarify", start + timedelta(seconds=2)
    )]
    execution.spec_snapshot = {"safe_to_implement": False}
    execution.review_snapshot = {"result": "NEEDS_HUMAN"}
    controller.executions[execution.id] = (execution, [first])
    item = WorkflowBatchItem(0, "task-1", "Task 1", execution.id)

    waiting = (
        "! Generating specification — waiting for human input"
        if stage is WorkflowStage.GENERATE_SPEC
        else "! Reviewing pull request — waiting for human input"
    )
    assert persisted_row(item, controller).timeline == (waiting,)  # type: ignore[arg-type]

    execution.human_interactions[0].response = "Clarification"
    execution.status = WorkflowStatus.RUNNING
    steps = controller.executions[execution.id][1]
    steps.append(WorkflowStep(2, execution.id, stage, StepStatus.SUCCEEDED,
                              start + timedelta(seconds=3), start + timedelta(seconds=4)))
    if stage is WorkflowStage.GENERATE_SPEC:
        execution.spec_snapshot = {"safe_to_implement": True}
    else:
        execution.review_snapshot = {"result": "APPROVED"}
    assert persisted_row(item, controller).timeline == (  # type: ignore[arg-type]
        waiting,
        "✓ Specification generated" if stage is WorkflowStage.GENERATE_SPEC
        else "✓ Review completed",
    )


def test_human_pause_after_failed_attempt_keeps_failure_in_both_projections() -> None:
    stage = WorkflowStage.GENERATE_SPEC
    progress = LiveBatchProgress(_batch(1))
    progress.apply(WorkflowEvent("execution-1", WorkflowEventKind.EXECUTION_CREATED,
                                 payload={"batch_position": 0}))
    progress.apply(WorkflowEvent("execution-1", WorkflowEventKind.STAGE_STARTED,
                                 stage=stage, step_status=StepStatus.RUNNING))
    progress.apply(WorkflowEvent("execution-1", WorkflowEventKind.STAGE_FAILED,
                                 stage=stage, step_status=StepStatus.FAILED, message="blocked"))
    progress.apply(WorkflowEvent("execution-1", WorkflowEventKind.STATUS_CHANGED,
                                 stage=stage, status=WorkflowStatus.NEEDS_HUMAN))
    assert progress.rows()[0].timeline == ("✗ Generating specification failed: blocked",)

    start = datetime(2026, 1, 1, tzinfo=UTC)
    execution = WorkflowExecution(id="execution-1", status=WorkflowStatus.NEEDS_HUMAN,
                                  current_stage=stage,
                                  human_interactions=[HumanInteraction(
                                      1, "execution-1", stage, "Clarify",
                                      start + timedelta(seconds=4)
                                  )])
    steps = [
        WorkflowStep(1, execution.id, stage, StepStatus.SUCCEEDED, start,
                     start + timedelta(seconds=1)),
        WorkflowStep(2, execution.id, stage, StepStatus.FAILED,
                     start + timedelta(seconds=2), start + timedelta(seconds=3), "blocked"),
    ]
    controller = _Controller()
    controller.executions[execution.id] = (execution, steps)
    timeline = persisted_row(
        WorkflowBatchItem(0, "task-1", "Task 1", execution.id), controller  # type: ignore[arg-type]
    ).timeline
    assert timeline == ("✓ Specification generated", "✗ Generating specification failed: blocked")
