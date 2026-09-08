"""Provider wrapper which records the exact request and normalized response."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import asdict
from typing import Any

from orchestrator.models.agent import (
    AgentRequest,
    AgentResult,
    AgentRole,
    AgentStartedCallback,
)
from orchestrator.models.execution_diagnostic import DiagnosticCategory
from orchestrator.providers.agent_provider import AgentProvider
from orchestrator.runtime_logging import redact_payload

DiagnosticRecorder = Callable[[DiagnosticCategory, AgentRole, dict[str, Any]], None]


class DiagnosticAgentProvider:
    """Observe provider traffic without altering the provider-neutral contract."""

    def __init__(
        self,
        provider: AgentProvider,
        *,
        role: AgentRole,
        provider_config: Mapping[str, Any],
        record: DiagnosticRecorder,
    ) -> None:
        self._provider = provider
        self._role = role
        self._provider_config = dict(provider_config)
        self._record = record

    def execute(
        self, request: AgentRequest, *, on_started: AgentStartedCallback | None = None
    ) -> AgentResult:
        self._record(DiagnosticCategory.REQUEST, self._role, self._request_payload(request))
        try:
            result = self._provider.execute(request, on_started=on_started)
        except Exception as exc:
            self._record(
                DiagnosticCategory.ERROR,
                self._role,
                {
                    "operation": "agent_provider.execute",
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                    "provider": self._provider_config,
                },
            )
            raise
        self._record(DiagnosticCategory.RESPONSE, self._role, self._response_payload(result))
        return result

    def _request_payload(self, request: AgentRequest) -> dict[str, Any]:
        schema = request.structured_output
        return self._sanitize(
            {
                "role": self._role.value,
                "provider": self._provider_config,
                "title": request.title,
                "workspace_path": str(request.workspace_path) if request.workspace_path else None,
                "repository": request.repository,
                "revision": request.revision,
                "effective_instructions": request.instructions,
                "instructions": request.instructions,
                "request_metadata": dict(request.metadata),
                "structured_output": (
                    {"name": schema.name, "schema": dict(schema.schema)} if schema else None
                ),
            }
        )

    def _response_payload(self, result: AgentResult) -> dict[str, Any]:
        execution = asdict(result.execution) if result.execution is not None else None
        return self._sanitize(
            {
                "role": self._role.value,
                "success": result.success,
                "raw_output": result.output,
                "parsed_structured_output": self._structured_output(result),
                "finish_reason": result.metadata.get("finish_reason"),
                "usage": {
                    key: value for key, value in result.metadata.items()
                    if "token" in key.casefold() or key.casefold() == "usage"
                },
                "provider_metadata": dict(result.metadata),
                "error": result.error_message,
                "workspace_changes_may_exist": result.workspace_changes_may_exist,
                "execution": execution,
            }
        )

    @staticmethod
    def _sanitize(payload: dict[str, Any]) -> dict[str, Any]:
        value = redact_payload(payload)
        return value if isinstance(value, dict) else {"value": value}

    @staticmethod
    def _structured_output(result: AgentResult) -> dict[str, Any] | None:
        if result.structured_output is not None:
            return dict(result.structured_output)
        try:
            parsed = json.loads(result.output)
        except (TypeError, ValueError):
            return None
        return parsed if isinstance(parsed, dict) else None
