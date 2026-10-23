from __future__ import annotations

from pathlib import Path

from prchef.models.agent import AgentRequest, AgentResult, AgentRole
from prchef.models.execution_diagnostic import DiagnosticCategory
from prchef.providers.agent_provider import AgentProviderError
from prchef.providers.diagnostic_provider import DiagnosticAgentProvider


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
    token = "ghs_" + ("FAKE_NOT_A_SECRET.AaZz09-_" * 22)[:516]
    instructions = f"Authorization: Bearer {token}\nAPI_KEY=also-secret\n{token}\nfull context"
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
            instructions=instructions,
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
        "Authorization=[REDACTED] [REDACTED]\nAPI_KEY=[REDACTED]\n[REDACTED]\nfull context"
    )
    assert "instructions" not in request
    assert request["provider"] == {"name": "provider", "model": "test", "api_key": "[REDACTED]"}
    assert response["parsed_structured_output"] == {"safe_to_implement": True}
    assert response["finish_reason"] == "stop"
    assert response["provider_metadata"] == {
        "finish_reason": "stop", "input_tokens": 12, "api_key": "[REDACTED]"
    }
    assert wrapped_provider.request is not None
    assert wrapped_provider.request.instructions == instructions


def test_diagnostic_provider_redacts_retryable_provider_error_metadata() -> None:
    class FailingProvider:
        def execute(self, request: AgentRequest, *, on_started=None) -> AgentResult:
            del request, on_started
            raise AgentProviderError(
                "API Error: Request rejected (429), Authorization: Bearer provider-secret",
                category="rate_limit",
                metadata={"errors": [{"api_key": "provider-key", "code": "rate_limit"}]},
            )

    events: list[tuple[DiagnosticCategory, AgentRole, dict[str, object]]] = []
    provider = DiagnosticAgentProvider(
        FailingProvider(),  # type: ignore[arg-type]
        role=AgentRole.PLANNER,
        provider_config={"name": "claude"},
        record=lambda category, role, payload: events.append((category, role, payload)),
    )

    try:
        provider.execute(AgentRequest(title="plan", instructions="work"))
    except AgentProviderError:
        pass
    else:
        raise AssertionError("the failing provider should propagate its error")

    payload = events[1][2]
    assert payload["message"] == (
        "API Error: Request rejected (429), Authorization=[REDACTED] [REDACTED]"
    )
    assert payload["provider_metadata"] == {
        "errors": [{"api_key": "[REDACTED]", "code": "rate_limit"}]
    }
