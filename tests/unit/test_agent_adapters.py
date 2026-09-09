from pathlib import Path
from typing import Any

from orchestrator.clients.exceptions import JulesSessionNeedsHumanError
from orchestrator.models import (
    AgentExecutionReference,
    AgentRequest,
    ImplementationResult,
    JulesOperationResult,
    JulesSession,
    JulesSessionState,
    StructuredOutputExpectation,
)
from orchestrator.providers.agent_provider import AgentNeedsHumanError
from orchestrator.providers.codex_provider import CodexAgentProvider
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


def test_codex_adapter_passes_request_structured_output_to_client(tmp_path: Path) -> None:
    schema = {"type": "object", "properties": {"plan": {"type": "string"}}}

    class FakeCodexClient:
        def __init__(self) -> None:
            self.output_schema: dict[str, object] | None = None

        def implement(
            self,
            prompt: str,
            repository_path: Path,
            output_schema: dict[str, object] | None = None,
        ) -> ImplementationResult:
            assert prompt == "Return JSON"
            assert repository_path == tmp_path
            self.output_schema = output_schema
            return ImplementationResult(success=True, summary='{"plan": "ok"}')

    client = FakeCodexClient()
    provider = CodexAgentProvider(client)  # type: ignore[arg-type]
    request = AgentRequest(
        instructions="Return JSON",
        title="Any structured request",
        workspace_path=tmp_path,
        structured_output=StructuredOutputExpectation(name="plan", schema=schema),
    )

    result = provider.execute(request)

    assert result.success
    assert result.output == '{"plan": "ok"}'
    assert client.output_schema == schema


def test_codex_adapter_leaves_unstructured_requests_unchanged(tmp_path: Path) -> None:
    class FakeCodexClient:
        def implement(self, prompt: str, repository_path: Path) -> ImplementationResult:
            assert prompt == "Implement the change"
            assert repository_path == tmp_path
            return ImplementationResult(success=True, summary="implemented")

    provider = CodexAgentProvider(FakeCodexClient())  # type: ignore[arg-type]

    result = provider.execute(
        AgentRequest(
            instructions="Implement the change",
            title="Unstructured request",
            workspace_path=tmp_path,
        )
    )

    assert result.success
    assert result.output == "implemented"


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
