from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

import pytest

from orchestrator.clients.factory import ClientFactory
from orchestrator.config.project_config import GeminiVertexProviderSettings, ProjectConfig
from orchestrator.models import AgentRequest, AgentRole, StructuredOutputExpectation
from orchestrator.providers.agent_provider import AgentProviderError
from orchestrator.providers.gemini_vertex_provider import (
    GeminiVertexAgentProvider,
    _create_vertex_client,
)


@dataclass
class FakeUsage:
    prompt_token_count: int = 12
    candidates_token_count: int = 34
    total_token_count: int = 46


@dataclass
class FakeResponse:
    text: str
    parsed: object | None = None
    usage_metadata: FakeUsage | None = None


class FakeModels:
    def __init__(self, response: FakeResponse | Exception) -> None:
        self.response = response
        self.calls: list[dict[str, object]] = []

    def generate_content(self, **kwargs: object) -> FakeResponse:
        self.calls.append(kwargs)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class FakeClient:
    def __init__(self, response: FakeResponse | Exception) -> None:
        self.models = FakeModels(response)


def _settings(**overrides: object) -> GeminiVertexProviderSettings:
    values: dict[str, object] = {
        "type": "gemini-vertex",
        "project": "example-project",
        "location": "global",
        "model": "gemini-test-model",
    }
    values.update(overrides)
    return GeminiVertexProviderSettings.model_validate(values)


def _request(*, structured: StructuredOutputExpectation | None = None) -> AgentRequest:
    return AgentRequest(
        instructions="Analyze this supplied task context.",
        title="Agent task",
        structured_output=structured,
    )


def _provider(
    response: FakeResponse | Exception,
    settings: GeminiVertexProviderSettings | None = None,
) -> tuple[GeminiVertexAgentProvider, FakeClient, list[dict[str, object]]]:
    client = FakeClient(response)
    client_calls: list[dict[str, object]] = []

    def factory(**kwargs: object) -> FakeClient:
        client_calls.append(kwargs)
        return client

    return (
        GeminiVertexAgentProvider("vertex-gemini", settings or _settings(), client_factory=factory),
        client,
        client_calls,
    )


def test_provider_builds_vertex_sdk_client_with_configured_project_and_location() -> None:
    provider, _, client_calls = _provider(FakeResponse("done"), _settings(timeout_seconds=12))

    provider.execute(_request())

    assert client_calls == [
        {
            "vertexai": True,
            "project": "example-project",
            "location": "global",
            "timeout_seconds": 12,
        }
    ]


def test_sdk_client_factory_enables_vertex_mode_and_bounded_timeout() -> None:
    sentinel = object()

    with patch("google.genai.Client", return_value=sentinel) as client:
        result = _create_vertex_client(
            vertexai=True,
            project="example-project",
            location="global",
            timeout_seconds=12,
        )

    assert result is sentinel
    client.assert_called_once_with(
        vertexai=True,
        project="example-project",
        location="global",
        http_options={"timeout": 12_000},
    )


def test_plain_text_request_uses_configured_model_and_returns_agent_result() -> None:
    provider, client, _ = _provider(FakeResponse("A concise answer", usage_metadata=FakeUsage()))

    result = provider.execute(_request())

    assert result.output == "A concise answer"
    assert result.structured_output is None
    assert result.metadata == {
        "provider": "vertex-gemini",
        "provider_type": "gemini-vertex",
        "model": "gemini-test-model",
        "input_tokens": 12,
        "output_tokens": 34,
        "total_tokens": 46,
    }
    assert client.models.calls == [
        {
            "model": "gemini-test-model",
            "contents": "Analyze this supplied task context.",
            "config": {"temperature": 0.2},
        }
    ]


def test_structured_request_uses_native_json_schema_and_deserializes_output() -> None:
    expectation = StructuredOutputExpectation(
        name="answer",
        schema={
            "type": "object",
            "properties": {"summary": {"type": "string"}},
            "required": ["summary"],
        },
    )
    settings = _settings(generation={"temperature": 0.1, "max_output_tokens": 256})
    provider, client, _ = _provider(
        FakeResponse('{"summary":"native JSON"}', parsed={"summary": "native JSON"}),
        settings,
    )

    result = provider.execute(_request(structured=expectation))

    assert result.output == '{"summary":"native JSON"}'
    assert result.structured_output == {"summary": "native JSON"}
    assert client.models.calls[0]["config"] == {
        "temperature": 0.1,
        "max_output_tokens": 256,
        "response_mime_type": "application/json",
        "response_json_schema": expectation.schema,
    }


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (FakeResponse("this is not JSON"), "malformed structured output"),
        (FakeResponse("[]", parsed=[]), "non-object JSON value"),
        (FakeResponse("{}", parsed={}), "missing required fields: summary"),
        (FakeResponse('{"summary":3}', parsed={"summary": 3}), "expected a string"),
    ],
)
def test_malformed_structured_output_fails_clearly(
    response: FakeResponse, message: str
) -> None:
    provider, _, _ = _provider(response)
    expectation = StructuredOutputExpectation(
        name="answer",
        schema={
            "type": "object",
            "properties": {"summary": {"type": "string"}},
            "required": ["summary"],
        },
    )

    with pytest.raises(AgentProviderError, match=message):
        provider.execute(_request(structured=expectation))


def test_authentication_failure_is_actionable_and_does_not_leak_sdk_error() -> None:
    provider, _, _ = _provider(RuntimeError("Default credentials were unavailable"))

    with pytest.raises(AgentProviderError) as caught:
        provider.execute(_request())

    assert "Application Default Credentials" in str(caught.value)
    assert "Default credentials were unavailable" not in str(caught.value)


def test_sdk_failure_is_translated_without_leaking_provider_exception_details() -> None:
    provider, _, _ = _provider(RuntimeError("network failure with token=secret-value"))

    with pytest.raises(AgentProviderError) as caught:
        provider.execute(_request())

    assert str(caught.value) == (
        "Gemini Vertex request failed. Verify the configured project, location, model, "
        "and network connectivity."
    )


def test_registry_resolves_gemini_vertex_and_reuses_it_for_multiple_roles(tmp_path: Path) -> None:
    config = ProjectConfig.model_validate(
        {
            "path": tmp_path,
            "agents": {
                "planner": {"provider": "vertex-gemini"},
                "implementer": {"provider": "codex"},
                "reviewer": {"provider": "vertex-gemini"},
            },
            "providers": {
                "vertex-gemini": {
                    "type": "gemini-vertex",
                    "project": "example-project",
                    "location": "global",
                    "model": "gemini-test-model",
                },
                "codex": {"type": "codex-cli", "command": ["codex", "exec", "-"]},
            },
        }
    )

    clients = ClientFactory(None).create(config)

    assert isinstance(clients.agent(AgentRole.PLANNER), GeminiVertexAgentProvider)
    assert clients.agent(AgentRole.PLANNER) is clients.agent(AgentRole.REVIEWER)


def test_role_services_do_not_import_gemini_provider_classes() -> None:
    root = Path(__file__).parents[2] / "src" / "orchestrator" / "services"

    for filename in ("spec_service.py", "review_service.py", "implementation_service.py"):
        assert "Gemini" not in (root / filename).read_text(encoding="utf-8")


def test_provider_has_no_agent_role_specific_restriction() -> None:
    source = (
        Path(__file__).parents[2]
        / "src"
        / "orchestrator"
        / "providers"
        / "gemini_vertex_provider.py"
    )

    assert "AgentRole" not in source.read_text(encoding="utf-8")
