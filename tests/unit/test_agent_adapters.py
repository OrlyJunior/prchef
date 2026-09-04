from typing import Any

from orchestrator.clients.exceptions import JulesSessionNeedsHumanError
from orchestrator.models import (
    AgentExecutionReference,
    AgentRequest,
    JulesOperationResult,
    JulesSession,
    JulesSessionState,
)
from orchestrator.providers.agent_provider import AgentNeedsHumanError
from orchestrator.providers.jules_provider import JulesAgentProvider


class FakeJulesRuntime:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls = 0

    def execute(self, *args: Any) -> JulesOperationResult:
        self.calls += 1
        callback = args[-1]
        session = JulesSession(
            id="session-123",
            url="https://jules.example/session-123",
            state=JulesSessionState.QUEUED,
        )
        callback(session)
        if self.error is not None:
            raise self.error
        return JulesOperationResult(session=session, result_text='{"result": "ok"}')


def _request() -> AgentRequest:
    return AgentRequest(
        instructions="Return JSON",
        title="Planner",
        repository="owner/repo",
        revision="develop",
    )


def test_jules_adapter_exposes_session_reference_before_completion() -> None:
    runtime = FakeJulesRuntime()
    references: list[AgentExecutionReference] = []
    provider = JulesAgentProvider("remote", runtime)  # type: ignore[arg-type]

    result = provider.execute(_request(), on_started=references.append)

    assert runtime.calls == 1
    assert references == [result.execution]
    assert references[0].provider == "remote"
    assert references[0].external_id == "session-123"
    assert references[0].url == "https://jules.example/session-123"


def test_jules_adapter_translates_human_intervention_without_recreating_session() -> None:
    runtime = FakeJulesRuntime(
        JulesSessionNeedsHumanError("Jules needs feedback", "session-123")
    )
    references: list[AgentExecutionReference] = []
    provider = JulesAgentProvider("remote", runtime)  # type: ignore[arg-type]

    try:
        provider.execute(_request(), on_started=references.append)
    except AgentNeedsHumanError as error:
        assert error.external_id == "session-123"
    else:
        raise AssertionError("AgentNeedsHumanError was not raised")

    assert runtime.calls == 1
    assert references[0].external_id == "session-123"
