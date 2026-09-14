"""Claude Code implementation of the provider-neutral agent contract."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from orchestrator.clients.claude_code_client import ClaudeCodeClient, ClaudeCodeResult
from orchestrator.clients.exceptions import CommandExecutionError
from orchestrator.models.agent import AgentRequest, AgentResult, AgentStartedCallback
from orchestrator.providers.agent_provider import AgentProviderError


class ClaudeCodeAgentProvider:
    """Adapt Claude Code CLI results without introducing workflow semantics."""

    def __init__(self, client: ClaudeCodeClient) -> None:
        self._client = client

    def execute(
        self,
        request: AgentRequest,
        *,
        on_started: AgentStartedCallback | None = None,
    ) -> AgentResult:
        del on_started
        workspace_path = request.workspace_path
        if workspace_path is None:
            raise ValueError("Claude Code provider requires a workspace path")
        try:
            workspace_path = workspace_path.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ValueError(
                f"Claude Code workspace path is unavailable: {workspace_path}"
            ) from exc
        if not workspace_path.is_dir():
            raise ValueError(f"Claude Code workspace path is not a directory: {workspace_path}")

        try:
            result = self._client.execute(
                request.instructions,
                workspace_path,
                output_schema=(
                    request.structured_output.schema
                    if request.structured_output is not None
                    else None
                ),
            )
        except CommandExecutionError as exc:
            raise _command_error(exc) from None

        if not result.success:
            error = _result_error(result)
            if error is not None:
                raise error
            return AgentResult(
                output=result.output,
                success=False,
                error_message=result.error_message,
                workspace_changes_may_exist=result.workspace_changes_may_exist,
            )

        structured_output = _structured_output(result, request)
        metadata: dict[str, Any] = {"provider_type": "claude_code"}
        if result.session_id is not None:
            metadata["session_id"] = result.session_id
        return AgentResult(
            output=result.output,
            structured_output=structured_output,
            workspace_changes_may_exist=result.workspace_changes_may_exist,
            metadata=metadata,
        )


def _structured_output(
    result: ClaudeCodeResult, request: AgentRequest
) -> Mapping[str, Any] | None:
    expectation = request.structured_output
    if expectation is None:
        return None
    value: Any = result.structured_output
    if value is None:
        try:
            value = json.loads(result.output)
        except json.JSONDecodeError as exc:
            raise AgentProviderError(
                "Claude Code returned malformed structured output; expected a JSON object",
                category="invalid_structured_output",
            ) from exc
    if not isinstance(value, Mapping):
        raise AgentProviderError(
            "Claude Code returned structured output with a non-object JSON value",
            category="invalid_structured_output",
        )
    parsed = dict(value)
    _validate_schema(parsed, expectation.schema)
    return parsed


def _result_error(result: ClaudeCodeResult) -> AgentProviderError | None:
    message = result.error_message or "Claude Code request failed"
    details = message.casefold()
    if "malformed json" in details:
        return AgentProviderError(message, category="invalid_structured_output")
    authentication_markers = (
        "authentication",
        "not logged in",
        "unauthorized",
        "forbidden",
        "login",
    )
    if any(marker in details for marker in authentication_markers):
        return AgentProviderError(
            "Claude Code authentication or authorization failed. Authenticate the installed "
            "Claude CLI and retry.",
            category="authentication",
        )
    if "quota" in details or "usage limit" in details:
        return AgentProviderError(
            "Claude Code quota or usage limit was exhausted.", category="quota"
        )
    if any(marker in details for marker in ("rate limit", "ratelimit", "429")):
        return AgentProviderError(
            "Claude Code rate limit was reached.", category="rate_limit"
        )
    if any(
        marker in details
        for marker in ("capacity", "overloaded", "temporarily unavailable", "503")
    ):
        return AgentProviderError(
            "Claude Code is temporarily unavailable or out of capacity.", category="capacity"
        )
    return None


def _command_error(error: CommandExecutionError) -> AgentProviderError:
    details = str(error).casefold()
    if "executable not found" in details:
        return AgentProviderError(
            "Claude Code CLI is unavailable. Install it and authenticate before running this "
            "provider.",
            category="unavailable",
        )
    if "timed out" in details:
        return AgentProviderError("Claude Code request timed out.", category="timeout")
    return AgentProviderError("Claude Code CLI could not be started.", category="unknown")


def _validate_schema(value: Any, schema: Mapping[str, Any], path: str = "$") -> None:
    expected_type = schema.get("type")
    if expected_type == "object":
        if not isinstance(value, Mapping):
            _schema_error(path, "expected an object")
        required = schema.get("required", [])
        if isinstance(required, list):
            missing = [key for key in required if isinstance(key, str) and key not in value]
            if missing:
                _schema_error(path, f"is missing required field {missing[0]!r}")
        properties = schema.get("properties", {})
        if isinstance(properties, Mapping):
            if schema.get("additionalProperties") is False:
                unexpected = [key for key in value if key not in properties]
                if unexpected:
                    _schema_error(path, f"contains unexpected field {unexpected[0]!r}")
            for name, property_schema in properties.items():
                if name in value and isinstance(name, str) and isinstance(property_schema, Mapping):
                    _validate_schema(value[name], property_schema, f"{path}.{name}")
    elif expected_type == "array":
        if not isinstance(value, list):
            _schema_error(path, "expected an array")
        items = schema.get("items")
        if isinstance(items, Mapping):
            for index, item in enumerate(value):
                _validate_schema(item, items, f"{path}[{index}]")
    elif expected_type == "string" and not isinstance(value, str):
        _schema_error(path, "expected a string")
    elif expected_type == "boolean" and not isinstance(value, bool):
        _schema_error(path, "expected a boolean")
    elif expected_type == "integer" and (not isinstance(value, int) or isinstance(value, bool)):
        _schema_error(path, "expected an integer")
    elif expected_type == "number" and (
        not isinstance(value, (int, float)) or isinstance(value, bool)
    ):
        _schema_error(path, "expected a number")
    elif expected_type == "null" and value is not None:
        _schema_error(path, "expected null")
    enum = schema.get("enum")
    if isinstance(enum, list) and value not in enum:
        _schema_error(path, "contains a value outside the allowed enum")


def _schema_error(path: str, detail: str) -> None:
    raise AgentProviderError(
        "Claude Code returned structured output that does not match the requested schema "
        f"at {path}: {detail}",
        category="invalid_structured_output",
    )
