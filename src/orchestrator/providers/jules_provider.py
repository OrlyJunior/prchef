from orchestrator.clients.exceptions import JulesSessionNeedsHumanError
from orchestrator.clients.jules_client import JulesClient
from orchestrator.models.agent import (
    AgentExecutionReference,
    AgentRequest,
    AgentResult,
    AgentStartedCallback,
)
from orchestrator.models.jules_session import JulesSession
from orchestrator.providers.agent_provider import AgentNeedsHumanError


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
        return AgentResult(
            output=operation.result_text,
            execution=reference,
            metadata={"session_state": operation.session.state},
        )
