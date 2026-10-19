from __future__ import annotations

from prchef.clients.claude_code_client import ClaudeCodeClient
from prchef.models.agent import (
    AgentExecutionReference,
    AgentRequest,
    AgentResult,
    AgentStartedCallback,
)
from prchef.providers.agent_provider import AgentProviderError


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
        retryable = _retryable_failure_category(result.error_message, result.metadata)
        if not result.success and retryable is not None:
            raise AgentProviderError(
                result.error_message or "Claude Code exhausted a retryable provider resource.",
                category=retryable,
                metadata=dict(result.metadata),
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


def _retryable_failure_category(
    message: str | None, metadata: object | None = None
) -> str | None:
    """Normalize Claude CLI text to the provider-neutral fallback categories."""

    if message is None and metadata is None:
        return None
    details = " ".join((str(message or ""), str(metadata or ""))).casefold()
    if any(
        marker in details
        for marker in (
            "authentication",
            "authentication_failed",
            "unauthorized",
            "forbidden",
            "permission denied",
            "oauth_org_not_allowed",
            "account_on_hold",
            "invalid_request",
            "model_not_found",
            "invalid model",
            "unknown option",
            "content policy",
            "safety filter",
        )
    ):
        return None
    if any(marker in details for marker in ("quota", "insufficient_quota", "billing_error")):
        return "quota"
    if any(
        marker in details
        for marker in ("rate limit", "rate_limit", "rate-limit", "ratelimit", "429")
    ):
        return "rate_limit"
    if any(
        marker in details
        for marker in (
            "http 503",
            "503",
            "capacity",
            "overloaded",
            "temporarily unavailable",
            "server_error",
        )
    ):
        return "capacity"
    return None
