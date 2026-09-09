from orchestrator.clients.exceptions import JulesClientError, JulesSessionNeedsHumanError
from orchestrator.clients.jules_client import JulesClient
from orchestrator.models.agent import (
    AgentExecutionReference,
    AgentRequest,
    AgentResult,
    AgentStartedCallback,
)
from orchestrator.models.jules_session import JulesSession
from orchestrator.providers.agent_provider import AgentNeedsHumanError, AgentProviderError


class JulesAgentProvider:
    def __init__(self, name: str, client: JulesClient) -> None:
        self._name = name
        self._client = client

    def execute(
        self,
        request: AgentRequest,
        *,
        on_started: AgentStartedCallback | None = None,
    ) -> AgentResult:
        if not request.repository:
            raise ValueError("Jules provider requires a repository identity")
        if not request.revision:
            raise ValueError("Jules provider requires a repository revision")
        reference: AgentExecutionReference | None = None

        def session_created(session: JulesSession) -> None:
            nonlocal reference
            reference = AgentExecutionReference(
                provider=self._name,
                external_id=session.id,
                url=session.url,
                metadata={
                    "resource_name": session.resource_name,
                    "state": session.state,
                },
            )
            if on_started is not None:
                on_started(reference)

        try:
            operation = self._client.execute(
                request.instructions,
                request.title,
                request.repository,
                request.revision,
                session_created,
            )
        except JulesSessionNeedsHumanError as exc:
            raise AgentNeedsHumanError(str(exc), exc.session_id) from exc
        except JulesClientError as exc:
            category = _retryable_failure_category(str(exc))
            if category is None:
                raise
            raise AgentProviderError(
                "Jules provider quota, rate limit, or temporary capacity was exhausted.",
                category=category,
            ) from None
        return AgentResult(
            output=operation.result_text,
            execution=reference,
            metadata={"session_state": operation.session.state},
        )


def _retryable_failure_category(message: str) -> str | None:
    details = message.casefold()
    if "quota" in details:
        return "quota"
    if "http 429" in details or "rate limit" in details:
        return "rate_limit"
    if any(
        marker in details
        for marker in ("http 503", "capacity", "overloaded", "temporarily unavailable")
    ):
        return "capacity"
    return None
