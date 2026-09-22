"""Unit tests for the shared batch progress projection.

These exercise ``orchestrator.tui.batch_progress`` and
``orchestrator.tui.workflow_presentation`` directly, without driving a
Textual screen, so the event-to-row mapping stays fast and deterministic.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from orchestrator.models.agent import AgentRole
from orchestrator.models.execution_diagnostic import DiagnosticCategory
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

    def execution_detail(self, execution_id: str) -> tuple[WorkflowExecution, list[WorkflowStep]]:
        if execution_id in self.errors:
            raise self.errors[execution_id]
        return self.executions[execution_id]


def test_unlinked_items_render_as_queued_in_original_order() -> None:
    progress = LiveBatchProgress(_batch(3))

    rows = progress.rows()

    assert [(row.position, row.category, row.status_label) for row in rows] == [
        (0, "QUEUED", "QUEUED"),
        (1, "QUEUED", "QUEUED"),
        (2, "QUEUED", "QUEUED"),
    ]
    assert summarize(rows)["QUEUED"] == 3


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


def test_waiting_event_holds_only_later_unsubmitted_rows() -> None:
    progress = LiveBatchProgress(_batch(3))
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
    assert rows[1].held_by_provider == "jules"
    assert rows[1].status_label == "QUEUED — held by jules"
    assert rows[2].held_by_provider == "jules"


def test_provider_hold_clears_on_admission() -> None:
    progress = LiveBatchProgress(_batch(2))
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
    progress.apply(_status_event("execution-1", WorkflowStatus.RUNNING, position=0))

    rows = progress.rows()
    assert rows[0].category == "RUNNING"
    assert rows[1].held_by_provider is None
    assert rows[1].status_label == "QUEUED"


def test_provider_hold_clears_on_terminal_status_without_admission() -> None:
    progress = LiveBatchProgress(_batch(2))
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
    progress.apply(
        _status_event(
            "execution-1",
            WorkflowStatus.FAILED,
            position=0,
            kind=WorkflowEventKind.WORKFLOW_FINISHED,
        )
    )

    assert progress.rows()[1].held_by_provider is None


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
    assert row.timeline == ("✓ Changes implemented", "✗ Reviewing pull request failed: boom")


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
