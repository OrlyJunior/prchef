from __future__ import annotations

import logging
import subprocess
from collections.abc import Iterator

import pytest

from prchef.clients.notifications.base import NotificationEvent
from prchef.clients.notifications.desktop import DesktopNotifier
from prchef.clients.notifications.windows import WindowsToastNotifier
from prchef.config.project_config import NotificationsSettings
from prchef.models.workflow_execution import WorkflowExecution
from prchef.models.workflow_status import WorkflowStatus
from prchef.services.notifications import NotificationService


@pytest.fixture
def notification_log_capture(caplog) -> Iterator[None]:
    """Capture notification warnings without relying on prior CLI logging setup."""
    loggers = [
        logging.getLogger("prchef.clients.notifications.windows"),
        logging.getLogger("prchef.clients.notifications.desktop"),
    ]
    previous_propagation = [logger.propagate for logger in loggers]
    for logger in loggers:
        logger.addHandler(caplog.handler)
        logger.propagate = False
    try:
        yield
    finally:
        for logger, propagate in zip(loggers, previous_propagation, strict=True):
            logger.removeHandler(caplog.handler)
            logger.propagate = propagate


def _event() -> NotificationEvent:
    return NotificationEvent(
        execution_id="execution-1",
        task_id="card-1",
        status=WorkflowStatus.APPROVED,
        title="Workflow approved",
        message="Task card-1 finished with status APPROVED.",
    )


def test_windows_toast_command_contains_the_event_text_and_quotes_powershell_literals() -> None:
    event = NotificationEvent(
        execution_id="execution-1",
        task_id="card-1",
        status=WorkflowStatus.FAILED,
        title="Couldn't deliver",
        message="Task's failure needs attention",
    )

    command = WindowsToastNotifier.build_command(event)

    assert command[:5] == ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command"]
    assert "Couldn''t deliver" in command[-1]
    assert "Task''s failure needs attention" in command[-1]
    assert "ToastNotificationManager" in command[-1]


def test_windows_toast_failure_is_logged_and_never_raises(
    caplog, notification_log_capture
) -> None:
    def failing_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        del args, kwargs
        raise OSError("PowerShell unavailable")

    notifier = WindowsToastNotifier(
        run=failing_run,
        environ={"SESSIONNAME": "Console"},
    )

    with caplog.at_level(logging.WARNING):
        notifier.notify(_event())

    assert "Windows desktop notification failed" in caplog.text


def test_windows_toast_skips_headless_sessions_without_running_a_process(
    caplog, notification_log_capture
) -> None:
    calls: list[object] = []
    notifier = WindowsToastNotifier(
        run=lambda *args, **kwargs: calls.append((args, kwargs)),  # type: ignore[arg-type]
        environ={},
    )

    with caplog.at_level(logging.WARNING):
        notifier.notify(_event())

    assert calls == []
    assert "No interactive Windows desktop session" in caplog.text


def test_desktop_notifier_warns_on_unsupported_platform_without_delivery(
    caplog, notification_log_capture
) -> None:
    calls: list[NotificationEvent] = []

    class Recorder:
        def notify(self, event: NotificationEvent) -> None:
            calls.append(event)

    notifier = DesktopNotifier(platform_system=lambda: "Linux", windows_notifier=Recorder())

    with caplog.at_level(logging.WARNING):
        notifier.notify(_event())

    assert calls == []
    assert "unsupported on this platform" in caplog.text


def test_notification_service_is_opt_in_and_deduplicates_a_terminal_transition() -> None:
    calls: list[NotificationEvent] = []

    class Recorder:
        def notify(self, event: NotificationEvent) -> None:
            calls.append(event)

    service = NotificationService({"desktop": Recorder()})
    execution = WorkflowExecution(
        id="execution-1",
        task_id="card-1",
        status=WorkflowStatus.FAILED,
    )

    service.notify_outcome(NotificationsSettings(), execution)
    service.notify_outcome(NotificationsSettings(enabled=True), execution)
    service.notify_outcome(NotificationsSettings(enabled=True), execution)

    assert [event.status for event in calls] == [WorkflowStatus.FAILED]
