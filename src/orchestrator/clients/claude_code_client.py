"""Subprocess transport for Claude Code's non-interactive CLI."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from orchestrator.clients.command_client import CommandClient
from orchestrator.runtime_logging import format_duration, summarize_text

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ClaudeCodeResult:
    success: bool
    output: str = ""
    structured_output: Mapping[str, Any] | None = None
    error_message: str | None = None
    workspace_changes_may_exist: bool = False
    session_id: str | None = None


class ClaudeCodeClient:
    """Invoke ``claude -p`` in a supplied workspace without shell parsing."""

    def __init__(
        self,
        command_client: CommandClient,
        executable_path: str = "claude",
        *,
        timeout_seconds: int = 900,
        model: str | None = None,
        permission_mode: Literal["acceptEdits", "plan"] = "acceptEdits",
        max_turns: int | None = None,
        provider_name: str = "claude_code",
    ) -> None:
        self._commands = command_client
        self._executable_path = executable_path
        self._timeout_seconds = timeout_seconds
        self._model = model
        self._permission_mode = permission_mode
        self._max_turns = max_turns
        self._provider_name = provider_name

    def execute(
        self,
        prompt: str,
        workspace_path: Path,
        *,
        output_schema: Mapping[str, Any] | None = None,
    ) -> ClaudeCodeResult:
        command = self._execution_command(output_schema)
        logger.info("[claude-code] Starting provider=%s", self._provider_name)
        result = self._commands.run(
            command,
            cwd=workspace_path,
            input_text=prompt,
            timeout_seconds=self._timeout_seconds,
            on_progress=lambda elapsed: logger.info(
                "[claude-code] Still running after %s", format_duration(elapsed)
            ),
            heartbeat_interval_seconds=60.0,
        )
        if not result.success:
            detail = summarize_text(result.stderr or result.stdout, limit=2_000)
            logger.error(
                "[claude-code] Failed (exit %s, %s): %s",
                result.exit_code,
                format_duration(result.duration_seconds),
                detail or "no output",
            )
            return ClaudeCodeResult(
                success=False,
                error_message=detail or f"Claude Code failed with exit code {result.exit_code}",
                workspace_changes_may_exist=True,
            )

        payload = self._payload(result.stdout)
        if payload is None:
            return ClaudeCodeResult(
                success=False,
                error_message="Claude Code returned malformed JSON output",
                workspace_changes_may_exist=True,
            )
        output = payload.get("result")
        if not isinstance(output, str) or not output.strip():
            output = ""
        structured = payload.get("structured_output")
        if not isinstance(structured, Mapping):
            structured = None
        is_error = payload.get("is_error") is True
        if is_error:
            detail = output or summarize_text(result.stderr or result.stdout, limit=2_000)
            return ClaudeCodeResult(
                success=False,
                output=output,
                error_message=detail or "Claude Code reported an unsuccessful result",
                workspace_changes_may_exist=True,
                session_id=_string_field(payload, "session_id"),
            )
        if not output and structured is not None:
            output = json.dumps(dict(structured), ensure_ascii=False)
        if not output:
            return ClaudeCodeResult(
                success=False,
                error_message="Claude Code returned an empty result",
                workspace_changes_may_exist=True,
                session_id=_string_field(payload, "session_id"),
            )
        logger.info("[claude-code] Completed in %s", format_duration(result.duration_seconds))
        return ClaudeCodeResult(
            success=True,
            output=output,
            structured_output=dict(structured) if structured is not None else None,
            session_id=_string_field(payload, "session_id"),
        )

    def _execution_command(self, output_schema: Mapping[str, Any] | None) -> list[str]:
        command = [
            self._executable_path,
            "--print",
            "--output-format",
            "json",
            "--permission-mode",
            self._permission_mode,
        ]
        if self._model is not None:
            command.extend(("--model", self._model))
        if self._max_turns is not None:
            command.extend(("--max-turns", str(self._max_turns)))
        if output_schema is not None:
            command.extend(("--json-schema", json.dumps(dict(output_schema), ensure_ascii=False)))
        return command

    @staticmethod
    def _payload(stdout: str) -> Mapping[str, Any] | None:
        try:
            value = json.loads(stdout)
        except json.JSONDecodeError:
            return None
        return value if isinstance(value, Mapping) else None


def _string_field(value: Mapping[str, Any], name: str) -> str | None:
    item = value.get(name)
    return item.strip() if isinstance(item, str) and item.strip() else None
