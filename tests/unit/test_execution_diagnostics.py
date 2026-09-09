from __future__ import annotations

from pathlib import Path

from orchestrator.models.agent import AgentRequest, AgentResult, AgentRole
from orchestrator.models.execution_diagnostic import DiagnosticCategory
from orchestrator.providers.diagnostic_provider import DiagnosticAgentProvider


class _Provider:
    def __init__(self) -> None:
        self.request: AgentRequest | None = None

    def execute(self, request: AgentRequest, *, on_started=None) -> AgentResult:
        self.request = request
        del on_started
        return AgentResult(
            output='{"safe_to_implement": true}',
            metadata={"finish_reason": "stop", "input_tokens": 12, "api_key": "hidden"},
        )


def test_diagnostic_provider_records_full_redacted_request_and_response() -> None:
    events: list[tuple[DiagnosticCategory, AgentRole, dict[str, object]]] = []
    wrapped_provider = _Provider()
    provider = DiagnosticAgentProvider(
        wrapped_provider,  # type: ignore[arg-type]
        role=AgentRole.PLANNER,
        provider_config={"name": "provider", "model": "test", "api_key": "secret"},
        record=lambda category, role, payload: events.append((category, role, payload)),
    )

    provider.execute(
        AgentRequest(
            title="plan",
            instructions="Authorization: Bearer very-secret\nAPI_KEY=also-secret\nfull context",
            workspace_path=Path("/workspace"),
        )
    )

    request = events[0][2]
    response = events[1][2]
    assert [event[0] for event in events] == [
        DiagnosticCategory.REQUEST,
        DiagnosticCategory.RESPONSE,
    ]
    assert request["effective_instructions"] == (
        "Authorization=[REDACTED] [REDACTED]\nAPI_KEY=[REDACTED]\nfull context"
    )
    assert "instructions" not in request
    assert request["provider"] == {"name": "provider", "model": "test", "api_key": "[REDACTED]"}
    assert response["parsed_structured_output"] == {"safe_to_implement": True}
    assert response["finish_reason"] == "stop"
    assert response["provider_metadata"] == {
        "finish_reason": "stop", "input_tokens": 12, "api_key": "[REDACTED]"
    }
    assert wrapped_provider.request is not None
    assert wrapped_provider.request.instructions == (
        "Authorization: Bearer very-secret\nAPI_KEY=also-secret\nfull context"
    )
