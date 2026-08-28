from __future__ import annotations

import tempfile
from collections.abc import Sequence
from pathlib import Path

from orchestrator.clients.command_client import CommandClient
from orchestrator.models.implementation_result import ImplementationResult


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
            result = self._commands.run(
                command,
                cwd=repository_path,
                input_text=prompt,
                timeout_seconds=self._timeout_seconds,
            )
            summary = (
                output_file.read_text(encoding="utf-8", errors="replace").strip()
                if output_file.exists()
                else result.stdout.strip()
            )
            return ImplementationResult(
                success=result.success,
                summary=summary,
                error_message=None
                if result.success
                else (result.stderr.strip() or result.stdout.strip() or "Codex failed"),
            )
