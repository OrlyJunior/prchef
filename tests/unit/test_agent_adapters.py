from pathlib import Path

from orchestrator.models import (
    AgentRequest,
    ImplementationResult,
    StructuredOutputExpectation,
)
from orchestrator.providers.codex_provider import CodexAgentProvider


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
