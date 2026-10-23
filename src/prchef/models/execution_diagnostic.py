"""Sanitized, persisted execution diagnostics for the detailed log view."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from prchef.models.agent import AgentRole
from prchef.models.workflow_status import WorkflowStage


class DiagnosticCategory(StrEnum):
    REQUEST = "REQUEST"
    RESPONSE = "RESPONSE"
    VALIDATION = "VALIDATION"
    ERROR = "ERROR"
    WORKFLOW_EVENT = "WORKFLOW_EVENT"


@dataclass(frozen=True, slots=True)
class ExecutionDiagnostic:
    """A JSON-safe, redacted fact recorded while one execution is running."""

    execution_id: str
    category: DiagnosticCategory
    payload: dict[str, Any]
    stage: WorkflowStage | None = None
    role: AgentRole | None = None
    id: int | None = None
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))
