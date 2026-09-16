"""Claude Code print-mode transport behind the provider boundary."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

from orchestrator.clients.command_client import CommandClient
from orchestrator.clients.exceptions import CommandExecutionError
from orchestrator.runtime_logging import summarize_text

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ClaudeCodeResult:
    success: bool
    summary: str = ""
    structured_output: Mapping[str, Any] | None = None
    error_message: str | None = None
    workspace_changes_may_exist: bool = False
    session_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


class ClaudeCodeClient:
    """Invoke only the safe Claude Code CLI surface owned by this client."""

    def __init__(
        self,
        command_client: CommandClient,
        executable: str,
        timeout_seconds: int,
        *,
        model: str | None = None,
        permission_mode: Literal["dontAsk", "acceptEdits", "auto"] = "dontAsk",
        max_turns: int | None = None,
        max_budget_usd: float | None = None,
        bare_mode: bool = True,
        provider_name: str = "claude-code",
    ) -> None:
        self._commands = command_client
        self._executable = executable
        self._timeout_seconds = timeout_seconds
        self._model = model
        self._permission_mode = permission_mode
        self._max_turns = max_turns
        self._max_budget_usd = max_budget_usd
        self._bare_mode = bare_mode
        self._provider_name = provider_name

    def execute(
        self,
        prompt: str,
        workspace_path: Path,
        output_schema: Mapping[str, Any] | None = None,
    ) -> ClaudeCodeResult:
        command = self._execution_command(output_schema)
        logger.info("[claude-code] Starting execution")
        logger.debug(
            "[claude-code] Starting provider=%s model=%s permission_mode=%s bare=%s",
            self._provider_name,
            self._model or "<external-default>",
            self._permission_mode,
            self._bare_mode,
        )
        try:
            result = self._commands.run(
                command,
                cwd=workspace_path,
                input_text=prompt,
                timeout_seconds=self._timeout_seconds,
                on_progress=lambda elapsed: logger.info(
                    "[claude-code] Still running after %ss", int(elapsed)
                ),
                heartbeat_interval_seconds=60.0,
            )
        except CommandExecutionError as exc:
            message = summarize_text(str(exc), limit=2_000)
            return ClaudeCodeResult(
                success=False,
                error_message=message,
                workspace_changes_may_exist="timed out" in message.casefold(),
            )

        envelope, parse_error = self._parse_envelope(result.stdout)
        if envelope is None:
            message = parse_error or summarize_text(result.stderr or result.stdout, limit=2_000)
            if result.success:
                message = message or "Claude Code returned malformed JSON output"
            return ClaudeCodeResult(
                success=False,
                error_message=message or "Claude Code failed without output",
                workspace_changes_may_exist=True,
            )

        metadata = self._metadata(envelope)
        session_id = self._string(envelope.get("session_id"))
        summary = self._string(envelope.get("result")) or ""
        subtype = self._string(envelope.get("subtype"))
        is_error = (
            envelope.get("type") not in {None, "result"}
            or envelope.get("is_error") is True
            or (subtype is not None and subtype != "success")
        )
        if not result.success or is_error:
            return ClaudeCodeResult(
                success=False,
                summary=summary,
                error_message=summarize_text(
                    self._string(envelope.get("error"))
                    or summary
                    or result.stderr
                    or "Claude Code failed",
                    limit=2_000,
                ),
                workspace_changes_may_exist=True,
                session_id=session_id,
                metadata=metadata,
            )

        structured_output: Mapping[str, Any] | None = None
        if output_schema is not None:
            value = envelope.get("structured_output")
            if not isinstance(value, Mapping):
                return ClaudeCodeResult(
                    success=False,
                    summary=summary,
                    error_message="Claude Code result did not include a structured_output object",
                    workspace_changes_may_exist=True,
                    session_id=session_id,
                    metadata=metadata,
                )
            try:
                Draft202012Validator.check_schema(dict(output_schema))
                Draft202012Validator(dict(output_schema)).validate(dict(value))
            except SchemaError as exc:
                return ClaudeCodeResult(
                    success=False,
                    summary=summary,
                    error_message=f"Invalid structured-output schema: {summarize_text(str(exc))}",
                    workspace_changes_may_exist=True,
                    session_id=session_id,
                    metadata=metadata,
                )
            except ValidationError as exc:
                return ClaudeCodeResult(
                    success=False,
                    summary=summary,
                    error_message=(
                        "Claude Code returned invalid structured output: "
                        f"{summarize_text(exc.message)}"
                    ),
                    workspace_changes_may_exist=True,
                    session_id=session_id,
                    metadata=metadata,
                )
            structured_output = dict(value)

        logger.info("[claude-code] Completed in %.1fs", result.duration_seconds)
        return ClaudeCodeResult(
            success=True,
            summary=summary,
            structured_output=structured_output,
            session_id=session_id,
            metadata=metadata,
        )

    def _execution_command(self, output_schema: Mapping[str, Any] | None) -> list[str]:
        command = [
            self._executable,
            "-p",
            "--output-format",
            "json",
            "--permission-mode",
            self._permission_mode,
            "--permission-prompts",
            "none",
        ]
        if self._bare_mode:
            command.append("--bare")
        if self._model is not None:
            command.extend(("--model", self._model))
        if self._max_turns is not None:
            command.extend(("--max-turns", str(self._max_turns)))
        if self._max_budget_usd is not None:
            command.extend(("--max-budget-usd", str(self._max_budget_usd)))
        if output_schema is not None:
            schema_json = json.dumps(dict(output_schema), separators=(",", ":"))
            command.extend(("--json-schema", schema_json))
        return command

    @staticmethod
    def _parse_envelope(stdout: str) -> tuple[Mapping[str, Any] | None, str | None]:
        try:
            parsed = json.loads(stdout)
        except json.JSONDecodeError:
            return None, "Claude Code returned malformed JSON output"
        if not isinstance(parsed, Mapping):
            return None, "Claude Code returned a JSON envelope that was not an object"
        return parsed, None

    @staticmethod
    def _string(value: object) -> str | None:
        if not isinstance(value, str):
            return None
        normalized = value.strip()
        return normalized or None

    @classmethod
    def _metadata(cls, envelope: Mapping[str, Any]) -> dict[str, Any]:
        """Keep only stable, non-sensitive completion metadata for diagnostics."""

        metadata: dict[str, Any] = {}
        for key in (
            "model",
            "duration_ms",
            "duration_api_ms",
            "num_turns",
            "total_cost_usd",
            "usage",
            "modelUsage",
            "subtype",
        ):
            value = envelope.get(key)
            if value is None:
                continue
            if isinstance(value, (str, int, float, bool)):
                metadata[key] = value
            elif key in {"usage", "modelUsage"} and isinstance(value, Mapping):
                metadata[key] = dict(value)
        return metadata
