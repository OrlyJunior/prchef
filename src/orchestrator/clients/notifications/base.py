from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from orchestrator.models.workflow_status import WorkflowStatus


@dataclass(frozen=True, slots=True)
class NotificationEvent:
    """A workflow outcome ready for delivery through any notification channel."""

    execution_id: str
    task_id: str
    status: WorkflowStatus
    title: str
    message: str


class Notifier(Protocol):
    """Transport boundary for user-visible notification channels."""

    def notify(self, event: NotificationEvent) -> None:
        """Deliver an event, without changing workflow execution state."""

