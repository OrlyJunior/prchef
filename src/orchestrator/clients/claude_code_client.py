"""Subprocess adapter for Claude Code's non-interactive JSON protocol."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orchestrator.clients.command_client import CommandClient
from orchestrator.clients.exceptions import CommandExecutionError
from orchestrator.runtime_logging import format_duration

logger = logging.getLogger("orchestrator")


@dataclass(frozen=True, slots=True)
class ClaudeCodeResult:
    success: bool
    output: str = ""
    structured_output: Mapping[str, Any] | None = None
    error_message: str | None = None
    error_category: str = "unknown"
    workspace_changes_may_exist: bool = False
    session_id: str | None = None
    metadata: Mapping[str, Any] | None = None


class ClaudeCodeClient:
    """Run one Claude Code print-mode request in an orchestrator workspace.

    The client owns the CLI envelope and deliberately never adds working
    directories, resumes sessions, or runs Git/GitHub commands.
    """

    def __init__(
        self,
        command_client: CommandClient,
        command: Sequence[str],
        timeout_seconds: int,
        *,
        model: str | None = None,
        permission_mode: str = "plan",
        max_turns: int | None = None,
        max_budget_usd: float | None = None,
        bare: bool = True,
        provider_name: str = "claude-code",
    ) -> None:
        self._commands = command_client
        self._command = list(command)
        self._timeout_seconds = timeout_seconds
        self._model = model
        self._permission_mode = permission_mode
        self._max_turns = max_turns
        self._max_budget_usd = max_budget_usd
        self._bare = bare
        self._provider_name = provider_name

    def execute(
        self,
        prompt: str,
        workspace_path: Path,
        *,
        output_schema: Mapping[str, Any] | None = None,
    ) -> ClaudeCodeResult:
        if not workspace_path.is_dir():
            return ClaudeCodeResult(
                success=False,
                error_message="Claude Code provider requires an existing workspace directory.",
                error_category="invalid_request",
            )
        command = self._execution_command(output_schema)
        logger.info(
            "[claude-code] Starting provider=%s model=%s structured_output=%s",
            self._provider_name,
            self._model or "<external-default>",
            output_schema is not None,
        )
        try:
            response = self._commands.run(
                command,
                cwd=workspace_path,
                input_text=prompt,
                timeout_seconds=self._timeout_seconds,
                on_progress=lambda elapsed: logger.info(
                    "[claude-code] Still running after %s", format_duration(elapsed)
                ),
                heartbeat_interval_seconds=60.0,
            )
        except CommandExecutionError as exc:
            return ClaudeCodeResult(
                success=False,
                error_message=_command_failure_message(exc),
                error_category=_error_category(str(exc)),
                workspace_changes_may_exist="timed out" in str(exc).casefold(),
            )

        parsed = _parse_envelope(response.stdout)
        if parsed is None:
            if response.exit_code != 0:
                details = response.stderr or response.stdout
                category = _error_category(details)
                return ClaudeCodeResult(
                    success=False,
                    error_message=_failure_message(details),
                    error_category=category,
                    workspace_changes_may_exist=True,
                )
            return ClaudeCodeResult(
                success=False,
                error_message="Claude Code returned a malformed JSON result envelope.",
                error_category="malformed_output",
                workspace_changes_may_exist=True,
            )
        if response.exit_code != 0 or parsed.get("subtype") != "success":
            details = _envelope_error_details(parsed, response.stderr)
            return ClaudeCodeResult(
                success=False,
                error_message=_failure_message(details),
                error_category=_error_category(details),
                workspace_changes_may_exist=True,
                session_id=_string_field(parsed, "session_id"),
                metadata=_safe_metadata(parsed, self._provider_name, self._model),
            )

        output = parsed.get("result")
        if not isinstance(output, str):
            return ClaudeCodeResult(
                success=False,
                error_message="Claude Code result envelope did not contain text output.",
                error_category="malformed_output",
                workspace_changes_may_exist=True,
            )
        structured_output: Mapping[str, Any] | None = None
        if output_schema is not None:
            value = parsed.get("structured_output")
            if not isinstance(value, Mapping):
                return ClaudeCodeResult(
                    success=False,
                    error_message="Claude Code result did not contain an object structured output.",
                    error_category="invalid_structured_output",
                    workspace_changes_may_exist=True,
                )
            structured_output = dict(value)
            schema_error = _schema_error(structured_output, output_schema)
            if schema_error is not None:
                return ClaudeCodeResult(
                    success=False,
                    error_message=(
                        "Claude Code returned structured output that does not match the "
                        f"requested schema at {schema_error}."
                    ),
                    error_category="invalid_structured_output",
                    workspace_changes_may_exist=True,
                )
        logger.info("[claude-code] Completed provider=%s", self._provider_name)
        return ClaudeCodeResult(
            success=True,
            output=output,
            structured_output=structured_output,
            session_id=_string_field(parsed, "session_id"),
            metadata=_safe_metadata(parsed, self._provider_name, self._model),
        )

    def _execution_command(self, output_schema: Mapping[str, Any] | None) -> list[str]:
        command = [*self._command, "--print", "--output-format", "json"]
        if self._bare:
            command.append("--bare")
        command.extend(
            ("--permission-mode", self._permission_mode, "--permission-prompts", "none")
        )
        if self._model is not None:
            command.extend(("--model", self._model))
        if self._max_turns is not None:
            command.extend(("--max-turns", str(self._max_turns)))
        if self._max_budget_usd is not None:
            command.extend(("--max-budget-usd", str(self._max_budget_usd)))
        if output_schema is not None:
            schema = json.dumps(dict(output_schema), separators=(",", ":"))
            command.extend(("--json-schema", schema))
        return command


def _parse_envelope(stdout: str) -> dict[str, Any] | None:
    try:
        value = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _string_field(value: Mapping[str, Any], key: str) -> str | None:
    candidate = value.get(key)
    return candidate if isinstance(candidate, str) and candidate else None


def _safe_metadata(
    envelope: Mapping[str, Any], provider_name: str, configured_model: str | None
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "provider": provider_name,
        "provider_type": "claude-code",
    }
    model = _string_field(envelope, "model") or configured_model
    if model is not None:
        metadata["model"] = model
    for key in ("total_cost_usd", "duration_ms", "num_turns"):
        value = envelope.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            metadata[key] = value
    usage = envelope.get("usage")
    if isinstance(usage, Mapping):
        safe_usage = {
            str(key): value
            for key, value in usage.items()
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        }
        if safe_usage:
            metadata["usage"] = safe_usage
    return metadata


def _envelope_error_details(envelope: Mapping[str, Any], stderr: str) -> str:
    for key in ("error", "result", "message"):
        value = envelope.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return stderr


def _command_failure_message(error: CommandExecutionError) -> str:
    details = str(error).casefold()
    if "executable not found" in details:
        return (
            "Claude Code CLI executable is unavailable. "
            "Install Claude Code or configure its path."
        )
    if "timed out" in details:
        return "Claude Code execution timed out."
    return "Claude Code CLI could not be started."


def _failure_message(details: str) -> str:
    category = _error_category(details)
    messages = {
        "authentication": "Claude Code authentication or authorization failed.",
        "quota": "Claude Code quota or usage limit was exhausted.",
        "rate_limit": "Claude Code rate limit was reached.",
        "capacity": "Claude Code is temporarily unavailable or out of capacity.",
        "invalid_model": "Claude Code rejected the configured model or request.",
        "unsupported_runtime": (
            "Claude Code CLI is too old for the configured non-interactive options. "
            "Upgrade Claude Code to version 2.1.259 or newer."
        ),
        "timeout": "Claude Code execution timed out.",
    }
    return messages.get(category, "Claude Code execution failed.")


def _error_category(details: str) -> str:
    normalized = details.casefold()
    if "executable not found" in normalized or "not recognized" in normalized:
        return "unavailable"
    if any(
        marker in normalized
        for marker in ("unknown option", "unknown argument", "unrecognized option")
    ):
        return "unsupported_runtime"
    if "timed out" in normalized or "timeout" in normalized:
        return "timeout"
    if any(
        marker in normalized
        for marker in ("authentication", "unauthorized", "forbidden", "login")
    ):
        return "authentication"
    if "quota" in normalized or "billing" in normalized or "usage limit" in normalized:
        return "quota"
    if "rate limit" in normalized or "rate_limit" in normalized or "429" in normalized:
        return "rate_limit"
    if any(
        marker in normalized for marker in ("overloaded", "capacity", "server_error", "503")
    ):
        return "capacity"
    if any(
        marker in normalized
        for marker in ("model_not_found", "invalid_request", "invalid model")
    ):
        return "invalid_model"
    return "unknown"


def _schema_error(value: Any, schema: Mapping[str, Any], path: str = "$") -> str | None:
    expected_type = schema.get("type")
    if expected_type == "object":
        if not isinstance(value, Mapping):
            return f"{path}: expected an object"
        required = schema.get("required", [])
        if isinstance(required, list):
            for name in required:
                if isinstance(name, str) and name not in value:
                    return f"{path}: missing required field {name!r}"
        properties = schema.get("properties", {})
        if isinstance(properties, Mapping):
            if schema.get("additionalProperties") is False:
                for name in value:
                    if name not in properties:
                        return f"{path}: contains unexpected field {name!r}"
            for name, nested_schema in properties.items():
                if (
                    name in value
                    and isinstance(name, str)
                    and isinstance(nested_schema, Mapping)
                ):
                    error = _schema_error(value[name], nested_schema, f"{path}.{name}")
                    if error is not None:
                        return error
    elif expected_type == "array":
        if not isinstance(value, list):
            return f"{path}: expected an array"
        item_schema = schema.get("items")
        if isinstance(item_schema, Mapping):
            for index, item in enumerate(value):
                error = _schema_error(item, item_schema, f"{path}[{index}]")
                if error is not None:
                    return error
    elif expected_type == "string" and not isinstance(value, str):
        return f"{path}: expected a string"
    elif expected_type == "boolean" and not isinstance(value, bool):
        return f"{path}: expected a boolean"
    elif expected_type == "integer" and (
        not isinstance(value, int) or isinstance(value, bool)
    ):
        return f"{path}: expected an integer"
    elif expected_type == "number" and (
        not isinstance(value, (int, float)) or isinstance(value, bool)
    ):
        return f"{path}: expected a number"
    enum = schema.get("enum")
    if isinstance(enum, list) and value not in enum:
        return f"{path}: contains a value outside the allowed enum"
    return None
