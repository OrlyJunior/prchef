"""Observer events emitted after persisted workflow transitions."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from orchestrator.models.workflow_status import StepStatus, WorkflowStage, WorkflowStatus


class WorkflowEventKind(StrEnum):
    EXECUTION_CREATED = "EXECUTION_CREATED"
    STATUS_CHANGED = "STATUS_CHANGED"
    STAGE_STARTED = "STAGE_STARTED"
    STAGE_FINISHED = "STAGE_FINISHED"
    STAGE_FAILED = "STAGE_FAILED"
    AGENT_FALLBACK = "AGENT_FALLBACK"
    WORKFLOW_FINISHED = "WORKFLOW_FINISHED"
    DIAGNOSTIC = "DIAGNOSTIC"


@dataclass(frozen=True, slots=True)
class WorkflowEvent:
    """Informational event; it cannot control workflow execution."""

    execution_id: str
    kind: WorkflowEventKind
    stage: WorkflowStage | None = None
    status: WorkflowStatus | None = None
    step_status: StepStatus | None = None
    message: str | None = None
    category: str | None = None
    payload: dict[str, Any] | None = None
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))


WorkflowEventListener = Callable[[WorkflowEvent], None]
