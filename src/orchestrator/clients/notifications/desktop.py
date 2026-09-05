from __future__ import annotations

import logging
import platform
from collections.abc import Callable

from orchestrator.clients.notifications.base import NotificationEvent, Notifier
from orchestrator.clients.notifications.windows import WindowsToastNotifier

logger = logging.getLogger(__name__)


class DesktopNotifier:
    """Select the native desktop implementation without exposing it to workflow code."""

    def __init__(
        self,
        *,
        platform_system: Callable[[], str] = platform.system,
        windows_notifier: Notifier | None = None,
    ) -> None:
        self._platform_system = platform_system
        self._windows_notifier = windows_notifier or WindowsToastNotifier()

    def notify(self, event: NotificationEvent) -> None:
        if self._platform_system() != "Windows":
            logger.warning(
                "[notifications] Desktop notifications are unsupported on this platform; "
                "skipping %s",
                event.status,
            )
            return
        self._windows_notifier.notify(event)
