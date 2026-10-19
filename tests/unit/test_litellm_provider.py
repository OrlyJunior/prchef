from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from prchef.clients.factory import ClientFactory
from prchef.config.project_config import (
    ConfigurationError,
    LiteLLMProviderSettings,
    ProjectConfig,
    convert_legacy_vertex_provider,
)
from prchef.models import AgentRequest, AgentRole, StructuredOutputExpectation
from prchef.providers.agent_provider import AgentProviderError
from prchef.providers.litellm_provider import LiteLLMAgentProvider


@dataclass
class FakeMessage:
    content: str


@dataclass
class FakeChoice:
    message: FakeMessage
    finish_reason: str = "stop"


@dataclass
class FakeUsage:
    prompt_tokens: int = 12
    completion_tokens: int = 34
    total_tokens: int = 46


@dataclass
class FakeResponse:
    content: str
    model: str = "returned-model"
    usage: FakeUsage | None = None
    finish_reason: str = "stop"

    @property
    def choices(self) -> list[FakeChoice]:
        return [FakeChoice(FakeMessage(self.content), self.finish_reason)]


class FakeLiteLLM:
    def __init__(self, response: FakeResponse | Exception) -> None:
        self.response = response
        self.calls: list[dict[str, object]] = []

    def completion(self, **kwargs: object) -> FakeResponse:
        self.calls.append(kwargs)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class FakeProviderException(Exception):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retry_after: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response = type(
            "Response", (), {"headers": {"Retry-After": retry_after} if retry_after else {}}
        )()


def _settings(**overrides: object) -> LiteLLMProviderSettings:
    values: dict[str, object] = {"type": "litellm", "model": "openai/example-model"}
    values.update(overrides)
    return LiteLLMProviderSettings.model_validate(values)


def _request(*, structured: StructuredOutputExpectation | None = None) -> AgentRequest:
    return AgentRequest(
        instructions="Analyze this supplied task context.",
        title="Agent task",
        repository="owner/repo",
        revision="feature-branch",
        workspace_path=Path("workspace"),
        structured_output=structured,
    )


def _provider(
    response: FakeResponse | Exception,
    settings: LiteLLMProviderSettings | None = None,
    *,
    environ: dict[str, str] | None = None,
) -> tuple[LiteLLMAgentProvider, FakeLiteLLM]:
    client = FakeLiteLLM(response)
    return (
        LiteLLMAgentProvider(
            "generic-api",
            settings or _settings(),
            litellm_loader=lambda: client,
            environ=environ,
        ),
        client,
    )


def test_registry_resolves_litellm_without_loading_the_sdk_until_execution(tmp_path: Path) -> None:
    config = ProjectConfig.model_validate(
        {
            "path": tmp_path,
            "agents": {
                "planner": {"provider": "generic"},
                "implementer": {"provider": "codex"},
                "reviewer": {"provider": "generic"},
            },
            "providers": {
                "generic": {"type": "litellm", "model": "ollama/llama3.2"},
                "codex": {"type": "codex-cli", "command": ["codex", "exec", "-"]},
            },
        }
    )

    clients = ClientFactory().create(config)

    assert isinstance(clients.agent(AgentRole.PLANNER), LiteLLMAgentProvider)
    assert clients.agent(AgentRole.PLANNER) is clients.agent(AgentRole.REVIEWER)


def test_converted_vertex_options_reach_litellm_with_structured_output() -> None:
    options = convert_legacy_vertex_provider("vertex", {
        "type": "gemini-vertex", "project": "example-project", "model": "gemini-test",
        "timeout_seconds": 45, "generation": {"temperature": 0.4, "max_output_tokens": 256},
    })
    expectation = StructuredOutputExpectation(
        name="answer", schema={"type": "object", "properties": {"answer": {"type": "string"}},
                               "required": ["answer"], "additionalProperties": False},
    )
    provider, client = _provider(
        FakeResponse('{"answer":"ready"}'), LiteLLMProviderSettings.model_validate(options),
        environ={},
    )
    result = provider.execute(_request(structured=expectation))
    call = client.calls[0]
    assert call["model"] == "vertex_ai/gemini-test"
    assert call["vertex_project"] == "example-project"
    assert call["vertex_location"] == "global"
    assert call["timeout"] == 45
    assert call["temperature"] == 0.4
    assert call["max_tokens"] == 256
    assert call["response_format"] == {
        "type": "json_schema", "json_schema": {"name": "answer", "schema": expectation.schema},
    }
    assert "api_key" not in call
    assert result.structured_output == {"answer": "ready"}


@pytest.mark.parametrize("model", ["", "   "])
def test_missing_or_empty_model_is_a_configuration_error(model: str, tmp_path: Path) -> None:
    config = ProjectConfig.model_validate(
        {
            "path": tmp_path,
            "agents": {
                "planner": {"provider": "generic"},
                "implementer": {"provider": "codex"},
                "reviewer": {"provider": "generic"},
            },
            "providers": {
                "generic": {"type": "litellm", "model": model},
                "codex": {"type": "codex-cli", "command": ["codex", "exec", "-"]},
            },
        }
    )

    with pytest.raises(ConfigurationError, match="model"):
        ClientFactory().create(config, agent_roles={AgentRole.PLANNER})


def test_missing_model_is_a_configuration_error(tmp_path: Path) -> None:
    config = ProjectConfig.model_validate(
        {
            "path": tmp_path,
            "agents": {
                "planner": {"provider": "generic"},
                "implementer": {"provider": "codex"},
                "reviewer": {"provider": "generic"},
            },
            "providers": {
                "generic": {"type": "litellm"},
                "codex": {"type": "codex-cli", "command": ["codex", "exec", "-"]},
            },
        }
    )

    with pytest.raises(ConfigurationError, match="model"):
        ClientFactory().create(config, agent_roles={AgentRole.PLANNER})


def test_missing_configured_api_key_environment_variable_is_clear_and_secret_safe() -> None:
    provider, _ = _provider(
        FakeResponse("unused"), _settings(api_key_env="MISSING_KEY"), environ={}
    )

    with pytest.raises(ConfigurationError) as caught:
        provider.execute(_request())

    assert "MISSING_KEY" in str(caught.value)
    assert "unused" not in str(caught.value)


def test_unauthenticated_local_endpoint_maps_request_and_generation_options() -> None:
    provider, client = _provider(
        FakeResponse("A concise answer"),
        _settings(
            model="ollama/llama3.2",
            api_base="http://localhost:11434",
            timeout_seconds=12,
            temperature=0.1,
            max_output_tokens=256,
            generation={"top_p": 0.9},
        ),
    )

    result = provider.execute(_request())

    assert result.output == "A concise answer"
    assert client.calls == [
        {
            "model": "ollama/llama3.2",
            "messages": [
                {
                    "role": "system",
                    "content": "You are executing an orchestrator agent request. "
                    "Request title: Agent task\nRepository: owner/repo\n"
                    "Revision: feature-branch\nWorkspace path: workspace",
                },
                {"role": "user", "content": "Analyze this supplied task context."},
            ],
            "api_base": "http://localhost:11434",
            "timeout": 12,
            "temperature": 0.1,
            "max_tokens": 256,
            "top_p": 0.9,
        }
    ]
    assert "api_key" not in client.calls[0]


def test_plain_text_result_maps_usage_and_finish_reason() -> None:
    provider, _ = _provider(FakeResponse("A concise answer", usage=FakeUsage()))

    result = provider.execute(_request())

    assert result.metadata == {
        "provider": "generic-api",
        "provider_type": "litellm",
        "model": "returned-model",
        "input_tokens": 12,
        "output_tokens": 34,
        "total_tokens": 46,
        "finish_reason": "stop",
    }


def test_structured_output_uses_json_schema_and_validates_response() -> None:
    expectation = StructuredOutputExpectation(
        name="answer",
        schema={
            "type": "object",
            "properties": {"summary": {"type": "string"}},
            "required": ["summary"],
            "additionalProperties": False,
        },
    )
    provider, client = _provider(FakeResponse('{"summary":"native JSON"}'))

    result = provider.execute(_request(structured=expectation))

    assert result.structured_output == {"summary": "native JSON"}
    assert client.calls[0]["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "answer", "schema": expectation.schema},
    }


@pytest.mark.parametrize("content", ["not json", "[]", '{"summary": 3}'])
def test_invalid_structured_output_is_normalized(content: str) -> None:
    expectation = StructuredOutputExpectation(
        name="answer",
        schema={
            "type": "object",
            "properties": {"summary": {"type": "string"}},
            "required": ["summary"],
        },
    )
    provider, _ = _provider(FakeResponse(content))

    with pytest.raises(AgentProviderError) as caught:
        provider.execute(_request(structured=expectation))

    assert caught.value.category == "invalid_structured_output"


@pytest.mark.parametrize(
    ("error", "category"),
    [
        (FakeProviderException("invalid credentials", status_code=401), "authentication"),
        (FakeProviderException("operation timed out"), "timeout"),
        (FakeProviderException("connection reset"), "network"),
        (FakeProviderException("rate limit", status_code=429, retry_after="2.5"), "rate_limit"),
        (FakeProviderException("insufficient_quota"), "quota"),
        (FakeProviderException("service overloaded", status_code=503), "capacity"),
        (FakeProviderException("unknown model", status_code=404), "invalid_model"),
    ],
)
def test_provider_errors_are_normalized(error: Exception, category: str) -> None:
    provider, _ = _provider(error)

    with pytest.raises(AgentProviderError) as caught:
        provider.execute(_request())

    assert caught.value.category == category
    assert "credentials" not in str(caught.value)
    assert caught.value.is_retryable_quota_failure is (
        category in {"rate_limit", "quota", "capacity"}
    )


def test_rate_limit_preserves_retry_after_metadata() -> None:
    provider, _ = _provider(
        FakeProviderException("rate limit", status_code=429, retry_after="3")
    )

    with pytest.raises(AgentProviderError) as caught:
        provider.execute(_request())

    assert caught.value.retry_after_seconds == 3.0


def test_sdk_failure_does_not_expose_a_resolved_secret() -> None:
    provider, _ = _provider(FakeProviderException("network token=secret-value"))

    with pytest.raises(AgentProviderError) as caught:
        provider.execute(_request())

    assert "secret-value" not in str(caught.value)
    assert caught.value.__cause__ is None


def test_missing_required_dependency_is_actionable() -> None:
    provider = LiteLLMAgentProvider(
        "generic-api",
        _settings(),
        litellm_loader=lambda: (_ for _ in ()).throw(ModuleNotFoundError()),
    )

    with pytest.raises(ConfigurationError, match="Reinstall"):
        provider.execute(_request())


def test_generation_cannot_persist_an_api_key() -> None:
    with pytest.raises(ValueError, match="api_key"):
        _settings(generation={"api_key": "secret-value"})


def test_litellm_runtime_overrides_keep_generation_provider_local(tmp_path: Path) -> None:
    config = ProjectConfig.model_validate(
        {
            "path": tmp_path,
            "agents": {
                "planner": {"provider": "generic"},
                "implementer": {"provider": "codex"},
                "reviewer": {"provider": "generic"},
            },
            "providers": {
                "generic": {
                    "type": "litellm",
                    "model": "ollama/llama3.2",
                    "temperature": 0.2,
                    "generation": {"top_p": 0.8},
                },
                "codex": {"type": "codex-cli", "command": ["codex", "exec", "-"]},
            },
        }
    )

    overridden = config.with_agent_runtime_overrides(
        {},
        {
            "planner": {
                "model": "vllm/free-form-model",
                "generation": {"temperature": 0.7, "max_output_tokens": 512},
            }
        },
    )

    assert overridden.provider_runtime_values("__tui_runtime_planner") == {
        "model": "vllm/free-form-model",
        "generation": {"top_p": 0.8, "temperature": 0.7, "max_output_tokens": 512},
    }
