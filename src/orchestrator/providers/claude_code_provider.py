"""Provider-neutral Claude Code adapter."""

from __future__ import annotations

from orchestrator.clients.claude_code_client import ClaudeCodeClient
from orchestrator.models.agent import (
    AgentExecutionReference,
    AgentRequest,
    AgentResult,
    AgentStartedCallback,
)
from orchestrator.providers.agent_provider import AgentProviderError


class ClaudeCodeAgentProvider:
    def __init__(self, name: str, client: ClaudeCodeClient) -> None:
        self._name = name
        self._client = client

    def execute(
        self, request: AgentRequest, *, on_started: AgentStartedCallback | None = None
    ) -> AgentResult:
        # CommandClient buffers output, so Claude's session ID is unavailable
        # until completion. Do not fabricate an early startup reference.
        del on_started
        if request.workspace_path is None:
            raise AgentProviderError(
                "Claude Code provider requires a workspace path.", category="invalid_request"
            )
        response = self._client.execute(
            request.instructions,
            request.workspace_path,
            output_schema=(
                request.structured_output.schema if request.structured_output else None
            ),
        )
        if not response.success and response.error_category in {
            "quota",
            "rate_limit",
            "capacity",
        }:
            raise AgentProviderError(
                response.error_message or "Claude Code execution failed.",
                category=response.error_category,
            )
        execution = None
        if response.session_id is not None:
            execution = AgentExecutionReference(
                provider=self._name, external_id=response.session_id
            )
        metadata = dict(response.metadata or {})
        if not response.success:
            metadata["error_category"] = response.error_category
        return AgentResult(
            output=response.output,
            success=response.success,
            structured_output=response.structured_output,
            error_message=response.error_message,
            workspace_changes_may_exist=response.workspace_changes_may_exist,
            execution=execution,
            metadata=metadata,
        )
