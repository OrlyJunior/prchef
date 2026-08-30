from __future__ import annotations

import logging
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path

from orchestrator.clients.command_client import CommandClient
from orchestrator.models.implementation_result import ImplementationResult
from orchestrator.runtime_logging import format_duration, summarize_text

logger = logging.getLogger(__name__)


class CodexClient:
    def __init__(
        self,
        command_client: CommandClient,
        command: Sequence[str],
        timeout_seconds: int,
    ) -> None:
        self._commands = command_client
        self._command = list(command)
        self._timeout_seconds = timeout_seconds

    def implement(self, prompt: str, repository_path: Path) -> ImplementationResult:
        with tempfile.TemporaryDirectory(prefix="orchestrator-codex-") as temp_root:
            output_file = Path(temp_root) / "last-message.txt"
            command = [part.replace("{output_file}", str(output_file)) for part in self._command]
            logger.info("[codex] Starting implementation")
            started = time.monotonic()
            try:
                result = self._commands.run(
                    command,
                    cwd=repository_path,
                    input_text=prompt,
                    timeout_seconds=self._timeout_seconds,
                    on_progress=lambda elapsed: logger.info(
                        "[codex] Still running after %s", format_duration(elapsed)
                    ),
                    heartbeat_interval_seconds=60.0,
                )
            except Exception as exc:
                logger.error(
                    "[codex] Failed (%s after %s)",
                    type(exc).__name__,
                    format_duration(time.monotonic() - started),
                )
                raise
            summary = (
                output_file.read_text(encoding="utf-8", errors="replace").strip()
                if output_file.exists()
                else result.stdout.strip()
            )
            if result.success:
                logger.info("[codex] Completed in %s", format_duration(result.duration_seconds))
            else:
                detail = summarize_text(result.stderr or result.stdout)
                logger.error(
                    "[codex] Failed (exit %s, %s): %s",
                    result.exit_code,
                    format_duration(result.duration_seconds),
                    detail or "no output",
                )
            return ImplementationResult(
                success=result.success,
                summary=summary,
                error_message=None
                if result.success
                else (result.stderr.strip() or result.stdout.strip() or "Codex failed"),
            )
