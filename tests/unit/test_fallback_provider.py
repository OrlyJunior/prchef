from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from orchestrator.config.project_config import ProjectConfig
from orchestrator.models import AgentRequest, AgentResult, AgentRole, WorkflowStage
from orchestrator.models.execution_diagnostic import ExecutionDiagnostic
from orchestrator.models.workflow_event import WorkflowEventKind
from orchestrator.providers.agent_provider import AgentProviderError
from orchestrator.providers.fallback_provider import FallbackAgentProvider
from orchestrator.services.workflow_service import WorkflowService


class FakeProvider:
    def __init__(self, result: AgentResult | Exception) -> None:
        self.result = result
        self.calls = 0

    def execute(self, request: AgentRequest, *, on_started: Any = None) -> AgentResult:
        del request, on_started
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _request() -> AgentRequest:
    return AgentRequest(instructions="Do the work", title="Work")


def test_primary_success_does_not_use_fallback() -> None:
    primary = FakeProvider(AgentResult(output="primary"))
    fallback = FakeProvider(AgentResult(output="fallback"))
    events: list[str] = []

    def create_fallback() -> FakeProvider:
        events.append("created")
        return fallback

    provider = FallbackAgentProvider(
        primary,
        fallback_factory=create_fallback,
        on_fallback=lambda error: events.append(error.category),
    )

    assert provider.execute(_request()).output == "primary"
    assert primary.calls == 1
    assert fallback.calls == 0
    assert events == []


@pytest.mark.parametrize("category", ["quota", "rate_limit", "capacity"])
def test_retryable_primary_failure_uses_fallback_once(category: str) -> None:
    primary = FakeProvider(AgentProviderError("primary unavailable", category=category))
    fallback = FakeProvider(AgentResult(output="fallback"))
    events: list[str] = []
    provider = FallbackAgentProvider(
        primary,
        fallback_factory=lambda: fallback,
        on_fallback=lambda error: events.append(error.category),
    )

    assert provider.execute(_request()).output == "fallback"
    assert primary.calls == fallback.calls == 1
    assert events == [category]


def test_fallback_failure_is_final_and_does_not_loop() -> None:
    primary = FakeProvider(AgentProviderError("primary limited", category="rate_limit"))
    final_error = AgentProviderError("fallback limited", category="rate_limit")
    fallback = FakeProvider(final_error)
    provider = FallbackAgentProvider(
        primary, fallback_factory=lambda: fallback, on_fallback=lambda error: None
    )

    with pytest.raises(AgentProviderError, match="fallback limited") as caught:
        provider.execute(_request())

    assert caught.value is final_error
    assert primary.calls == fallback.calls == 1


@pytest.mark.parametrize(
    "category",
    ["authentication", "invalid_model", "invalid_structured_output", "content_policy", "unknown"],
)
def test_non_retryable_primary_failure_does_not_use_fallback(category: str) -> None:
    primary_error = AgentProviderError("primary rejected", category=category)
    primary = FakeProvider(primary_error)
    fallback = FakeProvider(AgentResult(output="fallback"))
    provider = FallbackAgentProvider(
        primary, fallback_factory=lambda: fallback, on_fallback=lambda error: None
    )

    with pytest.raises(AgentProviderError) as caught:
        provider.execute(_request())

    assert caught.value is primary_error
    assert fallback.calls == 0


def test_workflow_wrapper_persists_and_streams_sanitized_fallback_event(tmp_path: Path) -> None:
    primary = FakeProvider(
        AgentProviderError(
            "rate limited Authorization: Bearer primary-secret", category="rate_limit"
        )
    )
    fallback = FakeProvider(AgentResult(output="fallback"))

    class Repository:
        def __init__(self) -> None:
            self.diagnostics: list[ExecutionDiagnostic] = []

        def record_diagnostic(self, diagnostic: ExecutionDiagnostic) -> None:
            self.diagnostics.append(diagnostic)

        def cancellation_requested(self, execution_id: str) -> bool:
            del execution_id
            return False

    class Factory:
        def create(self, config: ProjectConfig, *, agent_roles: set[AgentRole]) -> Any:
            del config, agent_roles
            return SimpleNamespace(agent=lambda role: fallback)

    repository = Repository()
    service = WorkflowService(repository, object(), object(), Factory())  # type: ignore[arg-type]
    events = []
    # Listener and stage attribution belong to one execution's runtime, not to
    # the shared service, so concurrent executions cannot cross-contaminate.
    runtime = service._runtimes.begin("execution-1", events.append)
    runtime.active_stage = WorkflowStage.GENERATE_SPEC
    config = ProjectConfig.model_validate(
        {
            "path": tmp_path,
            "providers": {
                "primary": {"type": "jules"},
                "backup": {"type": "jules"},
            },
            "agents": {
                "planner": {"provider": "primary", "fallback_preset": "backup-preset"},
                "implementer": {"provider": "primary"},
                "reviewer": {"provider": "primary"},
            },
            "agent_presets": {
                "planner": {"backup-preset": {"provider": "backup"}}
            },
        }
    )

    provider = service._diagnostic_agent(primary, config, AgentRole.PLANNER, "execution-1")
    assert provider.execute(_request()).output == "fallback"

    fallback_diagnostic = next(
        item
        for item in repository.diagnostics
        if item.payload.get("kind") == WorkflowEventKind.AGENT_FALLBACK.value
    )
    assert fallback_diagnostic.payload["role"] == "planner"
    assert fallback_diagnostic.payload["primary_provider"] == "primary"
    assert fallback_diagnostic.payload["fallback_provider"] == "backup"
    assert fallback_diagnostic.payload["fallback_preset"] == "backup-preset"
    assert "primary-secret" not in str(repository.diagnostics)
    assert any(event.kind is WorkflowEventKind.AGENT_FALLBACK for event in events)
