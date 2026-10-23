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

from prchef.clients.command_client import CommandClient
from prchef.clients.exceptions import CommandExecutionError
from prchef.runtime_logging import redact_payload, summarize_text

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
        effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None,
        permission_mode: Literal["dontAsk", "acceptEdits", "auto"] = "dontAsk",
        max_turns: int | None = None,
        max_budget_usd: float | None = None,
        bare_mode: bool = False,
        provider_name: str = "claude-code",
    ) -> None:
        self._commands = command_client
        self._executable = executable
        self._timeout_seconds = timeout_seconds
        self._model = model
        self._effort = effort
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
            "[claude-code] Starting provider=%s model=%s effort=%s permission_mode=%s bare=%s",
            self._provider_name,
            self._model or "<external-default>",
            self._effort or "<external-default>",
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
                error_message=self._with_details(message, None),
                workspace_changes_may_exist="timed out" in message.casefold(),
            )

        envelope, parse_error = self._parse_envelope(result.stdout)
        if envelope is None:
            details = summarize_text(result.stderr or result.stdout, limit=2_000)
            message = self._with_details(
                parse_error or "Claude Code returned malformed JSON output", details
            )
            return ClaudeCodeResult(
                success=False,
                error_message=message or "Claude Code failed without output",
                workspace_changes_may_exist=True,
            )

        metadata = self._metadata(envelope)
        session_id = self._string(envelope.get("session_id"))
        summary = self._string(envelope.get("result"))
        envelope_type = self._string(envelope.get("type"))
        subtype = self._string(envelope.get("subtype"))
        if envelope_type is None or subtype is None:
            return ClaudeCodeResult(
                success=False,
                error_message=self._with_details(
                    "Claude Code returned a malformed result envelope "
                    "without a type or subtype",
                    self._terminal_details(envelope, result.stderr),
                ),
                workspace_changes_may_exist=True,
                session_id=session_id,
                metadata=metadata,
            )
        is_error = (
            envelope_type != "result"
            or envelope.get("is_error") is True
            or subtype != "success"
        )
        if not result.success or is_error:
            return ClaudeCodeResult(
                success=False,
                summary=summary or "",
                error_message=self._with_details(
                    "Claude Code failed",
                    self._terminal_details(envelope, result.stderr),
                ),
                workspace_changes_may_exist=True,
                session_id=session_id,
                metadata=metadata,
            )
        if summary is None:
            return ClaudeCodeResult(
                success=False,
                error_message="Claude Code returned a malformed successful result without text",
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
            # Restricted mode is the enforcement boundary: it ignores user and
            # project settings, confines file tools to cwd, blocks cloud runs,
            # and removes command tools unless explicitly re-added below.
            "--restricted",
            # Keep the mutable surface intentionally file-only.  In
            # particular, do not re-add Bash: otherwise a model could bypass
            # prchef-owned Git/GitHub lifecycle policy with a shell.
            "--tools",
            "Read,Edit,Write,Glob,Grep",
            # --tools does not limit MCP tools. Deny them explicitly so the
            # boundary holds even if an administrator supplies one.
            "--disallowedTools",
            "mcp__*",
            "--permission-mode",
            self._permission_mode,
            "--permission-prompts",
            "none",
        ]
        if self._bare_mode:
            command.append("--bare")
        if self._model is not None:
            command.extend(("--model", self._model))
        if self._effort is not None:
            command.extend(("--effort", self._effort))
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

    def _with_details(self, message: str, details: str | None) -> str:
        diagnostic = f"{message}: {details}" if details else message
        if self._bare_mode and any(
            marker in diagnostic.casefold()
            for marker in (
                "authentication",
                "unauthorized",
                "invalid api key",
                "invalid_api_key",
                "missing api key",
                "api key is required",
                "no api key",
                "not logged in",
                "not authenticated",
                "please run /login",
                "api_error_status=401",
                "api error: 401",
                "http 401",
            )
        ):
            diagnostic += (
                ". Claude Code bare mode requires an API key (ANTHROPIC_API_KEY) or "
                "cloud credentials; it does not use subscription login credentials. "
                "For a Claude subscription, set bare_mode: false or omit bare_mode."
            )
        return diagnostic

    @classmethod
    def _terminal_details(cls, envelope: Mapping[str, Any], stderr: str) -> str | None:
        """Render Claude's terminal fields without losing failure classification data."""

        details: list[str] = []
        for key in (
            "error",
            "errors",
            "terminal_reason",
            "api_error_status",
            "stop_reason",
            "subtype",
            "result",
        ):
            value = envelope.get(key)
            if value is None:
                continue
            if isinstance(value, str):
                rendered = value
            else:
                try:
                    rendered = json.dumps(redact_payload(value), sort_keys=True)
                except (TypeError, ValueError):
                    rendered = str(redact_payload(value))
            if rendered.strip():
                details.append(f"{key}={rendered}")
        if stderr.strip():
            details.append(f"stderr={stderr}")
        return summarize_text("; ".join(details), limit=2_000) or None

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
            "errors",
            "terminal_reason",
            "api_error_status",
            "stop_reason",
        ):
            value = envelope.get(key)
            if value is None:
                continue
            if isinstance(value, (str, int, float, bool)):
                metadata[key] = value
            elif key in {"usage", "modelUsage", "errors"} and isinstance(
                value, (Mapping, list, tuple)
            ):
                # Diagnostics are persisted by provider-neutral wrappers. Keep
                # the documented terminal data but redact it before it crosses
                # this provider boundary.
                metadata[key] = redact_payload(value)
        return metadata
