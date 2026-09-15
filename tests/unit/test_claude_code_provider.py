from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from orchestrator.clients.claude_code_client import ClaudeCodeResult
from orchestrator.models import AgentRequest, StructuredOutputExpectation
from orchestrator.providers.agent_provider import AgentProviderError
from orchestrator.providers.claude_code_provider import ClaudeCodeAgentProvider


class FakeClaudeClient:
    def __init__(self, result: ClaudeCodeResult) -> None:
        self.result = result
        self.calls: list[tuple[str, Path, dict[str, Any] | None]] = []

    def execute(
        self, prompt: str, workspace: Path, *, output_schema: dict[str, Any] | None = None
    ) -> ClaudeCodeResult:
        self.calls.append((prompt, workspace, output_schema))
        return self.result


def test_claude_provider_adapts_structured_output_and_execution_reference(tmp_path: Path) -> None:
    client = FakeClaudeClient(
        ClaudeCodeResult(
            success=True,
            output="result",
            structured_output={"plan": "ready"},
            session_id="session-3",
            metadata={"model": "sonnet"},
        )
    )
    provider = ClaudeCodeAgentProvider("claude-plan", client)  # type: ignore[arg-type]
    schema = {"type": "object", "properties": {"plan": {"type": "string"}}}
    started = []

    result = provider.execute(
        AgentRequest(
            instructions="plan", title="Plan", workspace_path=tmp_path,
            structured_output=StructuredOutputExpectation("plan", schema),
        ),
        on_started=started.append,
    )

    assert result.structured_output == {"plan": "ready"}
    assert result.execution is not None and result.execution.external_id == "session-3"
    assert started == []
    assert client.calls == [("plan", tmp_path, schema)]


def test_claude_provider_preserves_partial_failure_and_fallback_categories(tmp_path: Path) -> None:
    partial_client = FakeClaudeClient(
        ClaudeCodeResult(False, error_message="failed", workspace_changes_may_exist=True)
    )
    partial = ClaudeCodeAgentProvider("claude", partial_client).execute(  # type: ignore[arg-type]
        AgentRequest(instructions="implement", title="Implement", workspace_path=tmp_path)
    )
    limited_client = FakeClaudeClient(
        ClaudeCodeResult(False, error_message="limited", error_category="rate_limit")
    )
    provider = ClaudeCodeAgentProvider(
        "claude", limited_client  # type: ignore[arg-type]
    )

    assert not partial.success
    assert partial.workspace_changes_may_exist
    with pytest.raises(AgentProviderError, match="limited") as exc_info:
        provider.execute(AgentRequest(instructions="plan", title="Plan", workspace_path=tmp_path))
    assert exc_info.value.category == "rate_limit"


def test_claude_provider_requires_a_workspace_path() -> None:
    client = FakeClaudeClient(ClaudeCodeResult(True))
    provider = ClaudeCodeAgentProvider("claude", client)  # type: ignore[arg-type]

    with pytest.raises(AgentProviderError, match="workspace path"):
        provider.execute(AgentRequest(instructions="plan", title="Plan"))
