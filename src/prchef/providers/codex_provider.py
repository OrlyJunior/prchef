from prchef.clients.codex_client import CodexClient
from prchef.models.agent import AgentRequest, AgentResult, AgentStartedCallback
from prchef.providers.agent_provider import AgentProviderError


class CodexAgentProvider:
    def __init__(self, client: CodexClient) -> None:
        self._client = client

    def execute(
        self,
        request: AgentRequest,
        *,
        on_started: AgentStartedCallback | None = None,
    ) -> AgentResult:
        del on_started
        if request.workspace_path is None:
            raise ValueError("Codex CLI provider requires a workspace path")
        if request.structured_output is None:
            result = self._client.implement(request.instructions, request.workspace_path)
        else:
            result = self._client.implement(
                request.instructions,
                request.workspace_path,
                output_schema=request.structured_output.schema,
            )
        retryable = _retryable_failure_category(result.error_message)
        if not result.success and retryable is not None:
            raise AgentProviderError(
                "Codex provider quota, rate limit, or temporary capacity was exhausted.",
                category=retryable,
                metadata=getattr(result, "metadata", {}),
            )
        return AgentResult(
            output=result.summary,
            metadata=getattr(result, "metadata", {}),
            success=result.success,
            error_message=result.error_message,
            workspace_changes_may_exist=result.workspace_changes_may_exist,
        )


def _retryable_failure_category(message: str | None) -> str | None:
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
