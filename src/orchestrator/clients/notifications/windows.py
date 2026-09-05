from __future__ import annotations

import logging
import os
import subprocess
from collections.abc import Callable, Mapping

from orchestrator.clients.notifications.base import NotificationEvent
from orchestrator.runtime_logging import redact_text

logger = logging.getLogger(__name__)

_POWERSHELL = ("powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command")


class WindowsToastNotifier:
    """Best-effort Windows toast delivery using only the built-in PowerShell runtime."""

    def __init__(
        self,
        *,
        run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._run = run
        self._environ = os.environ if environ is None else environ

    @staticmethod
    def _headless(environ: Mapping[str, str]) -> bool:
        session = environ.get("SESSIONNAME", "").strip().casefold()
        return session in {"", "services"}

    @staticmethod
    def _quote(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    @classmethod
    def build_command(cls, event: NotificationEvent) -> list[str]:
        """Build a self-contained PowerShell command without interpolating executable input."""
        title = cls._quote(event.title)
        message = cls._quote(event.message)
        script = (
            "$ErrorActionPreference = 'Stop'; "
            "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, "
            "ContentType = WindowsRuntime] > $null; "
            "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, "
            "ContentType = WindowsRuntime] > $null; "
            "$xml = New-Object Windows.Data.Xml.Dom.XmlDocument; "
            "$xml.LoadXml('<toast><visual><binding template=\"ToastGeneric\">"
            "<text></text><text></text></binding></visual></toast>'); "
            "$texts = $xml.GetElementsByTagName('text'); "
            f"$texts.Item(0).AppendChild($xml.CreateTextNode({title})) > $null; "
            f"$texts.Item(1).AppendChild($xml.CreateTextNode({message})) > $null; "
            "$toast = [Windows.UI.Notifications.ToastNotification]::new($xml); "
            "[Windows.UI.Notifications.ToastNotificationManager]::"
            "CreateToastNotifier('Development Orchestrator').Show($toast)"
        )
        return [*_POWERSHELL, script]

    def notify(self, event: NotificationEvent) -> None:
        if self._headless(self._environ):
            logger.warning(
                "[notifications] No interactive Windows desktop session; skipping %s",
                event.status,
            )
            return
        try:
            self._run(
                self.build_command(event),
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning(
                "[notifications] Windows desktop notification failed: %s",
                redact_text(str(exc)),
            )
