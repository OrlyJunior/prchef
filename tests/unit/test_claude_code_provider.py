from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.clients.claude_code_client import ClaudeCodeResult
from orchestrator.clients.exceptions import CommandExecutionError
from orchestrator.models import AgentRequest, StructuredOutputExpectation
from orchestrator.providers.agent_provider import AgentProviderError
from orchestrator.providers.claude_code_provider import ClaudeCodeAgentProvider


class FakeClaudeCodeClient:
    def __init__(self, result: ClaudeCodeResult | Exception) -> None:
        self.result = result
        self.calls: list[tuple[str, Path, dict[str, object]]] = []

    def execute(self, prompt: str, workspace_path: Path, **kwargs: object) -> ClaudeCodeResult:
        self.calls.append((prompt, workspace_path, kwargs))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_claude_code_provider_requires_an_existing_workspace() -> None:
    provider = ClaudeCodeAgentProvider(
        FakeClaudeCodeClient(ClaudeCodeResult(success=True))  # type: ignore[arg-type]
    )

    with pytest.raises(ValueError, match="requires a workspace path"):
        provider.execute(AgentRequest(instructions="work", title="work"))


def test_claude_code_provider_preserves_native_structured_output(tmp_path: Path) -> None:
    client = FakeClaudeCodeClient(
        ClaudeCodeResult(
            success=True,
            output="A plan was produced.",
            structured_output={"plan": "ready"},
            session_id="session-1",
        )
    )
    provider = ClaudeCodeAgentProvider(client)  # type: ignore[arg-type]
    expectation = StructuredOutputExpectation(
        name="plan",
        schema={
            "type": "object",
            "properties": {"plan": {"type": "string"}},
            "required": ["plan"],
        },
    )

    result = provider.execute(
        AgentRequest(
            instructions="Return a plan", title="plan", workspace_path=tmp_path,
            structured_output=expectation,
        )
    )

    assert result.success
    assert result.structured_output == {"plan": "ready"}
    assert result.metadata["session_id"] == "session-1"
    assert client.calls[0][1] == tmp_path.resolve()
    assert client.calls[0][2]["output_schema"] == expectation.schema


@pytest.mark.parametrize(
    ("message", "category"),
    [
        ("authentication required", "authentication"),
        ("rate limit reached", "rate_limit"),
        ("quota exhausted", "quota"),
    ],
)
def test_claude_code_provider_classifies_provider_failures(
    tmp_path: Path, message: str, category: str
) -> None:
    provider = ClaudeCodeAgentProvider(
        FakeClaudeCodeClient(ClaudeCodeResult(success=False, error_message=message))
    )  # type: ignore[arg-type]

    with pytest.raises(AgentProviderError) as caught:
        provider.execute(AgentRequest(instructions="work", title="work", workspace_path=tmp_path))

    assert caught.value.category == category


@pytest.mark.parametrize(
    ("message", "category"),
    [
        ("Executable not found: claude", "unavailable"),
        ("Command timed out after 30s: claude", "timeout"),
    ],
)
def test_claude_code_provider_classifies_command_start_failures(
    tmp_path: Path, message: str, category: str
) -> None:
    provider = ClaudeCodeAgentProvider(
        FakeClaudeCodeClient(CommandExecutionError(message))
    )  # type: ignore[arg-type]

    with pytest.raises(AgentProviderError) as caught:
        provider.execute(AgentRequest(instructions="work", title="work", workspace_path=tmp_path))

    assert caught.value.category == category


def test_claude_code_provider_rejects_malformed_structured_output(tmp_path: Path) -> None:
    provider = ClaudeCodeAgentProvider(
        FakeClaudeCodeClient(ClaudeCodeResult(success=True, output="not json"))
    )  # type: ignore[arg-type]

    with pytest.raises(AgentProviderError) as caught:
        provider.execute(
            AgentRequest(
                instructions="work",
                title="work",
                workspace_path=tmp_path,
                structured_output=StructuredOutputExpectation(
                    name="object", schema={"type": "object"}
                ),
            )
        )

    assert caught.value.category == "invalid_structured_output"
