from __future__ import annotations

import logging

from orchestrator.clients.notifications.base import NotificationEvent, Notifier
from orchestrator.clients.notifications.desktop import DesktopNotifier
from orchestrator.config.project_config import NotificationsSettings
from orchestrator.models.workflow_execution import WorkflowExecution
from orchestrator.models.workflow_status import WorkflowStatus
from orchestrator.runtime_logging import redact_text

logger = logging.getLogger(__name__)

_RELEVANT_STATUSES = frozenset(
    {
        WorkflowStatus.APPROVED,
        WorkflowStatus.CHANGES_REQUESTED,
        WorkflowStatus.NEEDS_HUMAN,
        WorkflowStatus.FAILED,
    }
)

_STATUS_TITLES = {
    WorkflowStatus.APPROVED: "Workflow approved",
    WorkflowStatus.CHANGES_REQUESTED: "Workflow needs changes",
    WorkflowStatus.NEEDS_HUMAN: "Workflow needs your input",
    WorkflowStatus.FAILED: "Workflow failed",
}


class NotificationService:
    """Dispatch terminal workflow outcomes through configured, best-effort channels."""

    def __init__(self, channels: dict[str, Notifier] | None = None) -> None:
        self._channels = {"desktop": DesktopNotifier()} if channels is None else channels
        self._delivered: set[tuple[str, WorkflowStatus]] = set()

    def notify_outcome(
        self, settings: NotificationsSettings, execution: WorkflowExecution
    ) -> None:
        if not settings.enabled or execution.status not in _RELEVANT_STATUSES:
            return
        transition = (execution.id, execution.status)
        if transition in self._delivered:
            return
        # Mark before delivery: notification is expressly best-effort and retries must not create
        # duplicate desktop alerts after a backend reports an ambiguous delivery failure.
        self._delivered.add(transition)
        notifier = self._channels.get(settings.channel)
        if notifier is None:
            logger.warning(
                "[notifications] Unsupported notification channel %r; skipping %s",
                settings.channel,
                execution.status,
            )
            return
        event = NotificationEvent(
            execution_id=execution.id,
            task_id=execution.task_id,
            status=execution.status,
            title=_STATUS_TITLES[execution.status],
            message=f"Task {execution.task_id} finished with status {execution.status}.",
        )
        try:
            notifier.notify(event)
        except Exception as exc:
            logger.warning(
                "[notifications] Notification delivery failed: %s", redact_text(str(exc))
            )
