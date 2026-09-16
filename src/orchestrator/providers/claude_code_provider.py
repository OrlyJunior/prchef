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
    """Provider-neutral adapter for one Claude Code print-mode invocation."""

    def __init__(self, name: str, client: ClaudeCodeClient) -> None:
        self._name = name
        self._client = client

    def execute(
        self,
        request: AgentRequest,
        *,
        on_started: AgentStartedCallback | None = None,
    ) -> AgentResult:
        # The CLI returns its session id only in its completion envelope.  There
        # is intentionally no early callback and no automatic session resume.
        del on_started
        if request.workspace_path is None:
            raise ValueError("Claude Code provider requires a workspace path")
        result = self._client.execute(
            request.instructions,
            request.workspace_path,
            output_schema=(request.structured_output.schema if request.structured_output else None),
        )
        retryable = _retryable_failure_category(result.error_message)
        if not result.success and retryable is not None:
            raise AgentProviderError(
                "Claude Code provider quota, rate limit, or temporary capacity was exhausted.",
                category=retryable,
            )
        execution = (
            AgentExecutionReference(
                provider=self._name,
                external_id=result.session_id,
                metadata=dict(result.metadata),
            )
            if result.session_id is not None
            else None
        )
        return AgentResult(
            output=result.summary,
            success=result.success,
            structured_output=result.structured_output,
            error_message=result.error_message,
            workspace_changes_may_exist=result.workspace_changes_may_exist,
            execution=execution,
            metadata=dict(result.metadata),
        )


def _retryable_failure_category(message: str | None) -> str | None:
    """Normalize Claude CLI text to the provider-neutral fallback categories."""

    if message is None:
        return None
    details = message.casefold()
    if any(
        marker in details
        for marker in (
            "authentication",
            "unauthorized",
            "forbidden",
            "permission denied",
            "content policy",
            "safety filter",
        )
    ):
        return None
    if "quota" in details or "insufficient_quota" in details:
        return "quota"
    if "rate limit" in details or "ratelimit" in details or "http 429" in details:
        return "rate_limit"
    if any(
        marker in details
        for marker in ("http 503", "capacity", "overloaded", "temporarily unavailable")
    ):
        return "capacity"
    return None
