from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime


@dataclass(frozen=True, slots=True)
class WorkflowBatchItem:
    """One task in the stable order captured when a batch starts."""

    position: int
    task_id: str
    task_title: str
    execution_id: str | None = None


@dataclass(frozen=True, slots=True)
class WorkflowBatch:
    """Durable metadata for a group of independently submitted workflows."""

    id: str
    repository: str
    task_source: str
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    items: tuple[WorkflowBatchItem, ...] = ()
