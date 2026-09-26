from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import orchestrator.services.debug_service as debug_service_module
from orchestrator.config.project_config import ProjectConfig
from orchestrator.models import (
    AgentRole,
    DevelopmentTask,
    PullRequest,
    RepositorySpec,
    ValidationResult,
    WorkflowExecution,
)
from orchestrator.services.debug_service import (
    DebugInputs,
    DebugService,
    DebugStage,
    parse_debug_stage,
)
from orchestrator.services.project_context import MAX_PROJECT_CONTEXT_CHARS
from orchestrator.services.repository_context import RepositoryContextRequest
from tests.unit.fakes import FakeClientFactory, FakeConfigLoader, FakeTrelloClient


def _task() -> DevelopmentTask:
    return DevelopmentTask(
        id="card-debug",
        source="trello",
        title="Add debug mode",
        description="Run one stage",
        url="https://trello.example/card-debug",
        repository="example",
        metadata={"custom_fields": {"Repository": "example"}},
    )


def _spec() -> RepositorySpec:
    return RepositorySpec(
        technical_context="service",
        current_behavior="workflow",
        implementation_proposal="add diagnostics",
    )


def _execution(tmp_path: Path) -> WorkflowExecution:
    task = _task()
    return WorkflowExecution(
        id="execution-debug",
        task_id=task.id,
        task_source=task.source,
        repository=str(tmp_path),
        base_branch="develop",
        working_branch="orchestrator/debug",
        task_snapshot=asdict(task),
        spec_snapshot=asdict(_spec()),
        validation_snapshot=asdict(ValidationResult(success=True)),
        pull_request_snapshot=asdict(
            PullRequest(
                number=42,
                url="https://github.example/pr/42",
                title=task.title,
                base_branch="develop",
                head_branch="orchestrator/debug",
            )
        ),
    )


class RecordingFactory(FakeClientFactory):
    def __init__(self) -> None:
        super().__init__()
        self.create_calls = 0

    def create(
        self,
        config: ProjectConfig,
        *,
        agent_roles: set[AgentRole] | None = None,
    ) -> SimpleNamespace:
        self.create_calls += 1
        return super().create(config, agent_roles=agent_roles)


def _service(
    tmp_path: Path,
    factory: RecordingFactory | None = None,
    task_provider_factory: Any = None,
) -> tuple[DebugService, RecordingFactory]:
    clients = factory or RecordingFactory()
    provider_factory = task_provider_factory or (lambda source, repository: FakeTrelloClient())
    return (
        DebugService(
            config_loader=FakeConfigLoader(tmp_path),
            client_factory=clients,  # type: ignore[arg-type]
            task_provider_factory=provider_factory,
        ),
        clients,
    )


def test_parse_debug_stage_accepts_documented_aliases() -> None:
    assert parse_debug_stage("spec") is DebugStage.SPEC_GENERATION
    assert parse_debug_stage("GENERATE_SPEC") is DebugStage.SPEC_GENERATION
    assert parse_debug_stage("task-loading") is DebugStage.TASK_LOADING


def test_dry_run_describes_inputs_without_creating_clients(tmp_path: Path) -> None:
    service, factory = _service(tmp_path)
    execution = _execution(tmp_path)
    execution.working_branch = None

    result = service.execute(
        execution, DebugStage.IMPLEMENTATION, dry_run=True
    )

    assert result["dry_run"] is True
    assert result["execution_mutated"] is False
    assert result["inputs"]["spec_snapshot"] is True
    assert result["inputs"]["requires_original_branch_checkout"] is False
    assert factory.create_calls == 0


def test_task_loading_debug_runs_only_the_selected_provider(tmp_path: Path) -> None:
    execution = _execution(tmp_path)
    calls: list[str] = []

    class Provider:
        def get_task(self, task_id: str) -> DevelopmentTask:
            calls.append(task_id)
            return _task()

    service, factory = _service(
        tmp_path, task_provider_factory=lambda source, repository: Provider()
    )

    result = service.execute(
        execution,
        DebugStage.TASK_LOADING,
        inputs=DebugInputs(task_source="trello", task_id=execution.task_id),
    )

    assert result["result"]["task"]["id"] == "card-debug"
    assert calls == ["card-debug"]
    assert factory.create_calls == 0


def test_spec_debug_reuses_persisted_task_without_mutating_execution(tmp_path: Path) -> None:
    execution = _execution(tmp_path)
    service, factory = _service(tmp_path)
    original = asdict(execution)

    result = service.execute(execution, DebugStage.SPEC_GENERATION)

    assert result["result"]["spec"]["safe_to_implement"] is True
    assert result["result"]["jules_sessions"][0]["id"] == "spec-123"
    assert asdict(execution) == original
    assert factory.create_calls == 1


@pytest.mark.parametrize(
    "stage",
    [DebugStage.SPEC_GENERATION, DebugStage.IMPLEMENTATION, DebugStage.REVIEW],
)
def test_debug_agent_stages_warn_when_project_context_is_truncated(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, stage: DebugStage
) -> None:
    context = tmp_path / "docs" / "PROJECT_CONTEXT.md"
    context.parent.mkdir()
    service, _ = _service(tmp_path)
    execution = _execution(tmp_path)

    context.write_text("x" * MAX_PROJECT_CONTEXT_CHARS, encoding="utf-8")
    service.execute(execution, stage)
    assert "Durable project context" not in caplog.text

    context.write_text("x" * (MAX_PROJECT_CONTEXT_CHARS + 1), encoding="utf-8")
    service.execute(execution, stage)
    assert str(context) in caplog.text
    assert f"{MAX_PROJECT_CONTEXT_CHARS + 1} characters" in caplog.text
    assert f"cap: {MAX_PROJECT_CONTEXT_CHARS}" in caplog.text


def test_spec_debug_uses_snapshot_or_direct_task_comments(tmp_path: Path) -> None:
    execution = _execution(tmp_path)
    execution.task_snapshot["metadata"]["comments"] = ["Snapshot note"]
    factory = RecordingFactory()
    prompts: list[str] = []
    original_execute = factory.planner.execute

    def record_planner(request, *, on_started=None):
        if request.structured_output and request.structured_output.name == "repository-spec":
            prompts.append(request.instructions)
        return original_execute(request, on_started=on_started)

    factory.planner.execute = record_planner

    class Provider:
        def get_task(self, task_id: str) -> DevelopmentTask:
            task = _task()
            task.metadata["comments"] = ["Direct note"]
            return task

    service, _ = _service(
        tmp_path, factory=factory, task_provider_factory=lambda source, repository: Provider()
    )
    service.execute(execution, DebugStage.SPEC_GENERATION)
    service.execute(None, DebugStage.SPEC_GENERATION, inputs=DebugInputs(
        task_source="trello", task_id="card-debug", repository="example",
    ))

    assert "Snapshot note" in prompts[0]
    assert "Direct note" not in prompts[0]
    assert "Direct note" in prompts[1]


def test_spec_debug_uses_configured_repository_context_strategy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _ = _service(tmp_path)
    requests: list[RepositoryContextRequest] = []

    class Strategy:
        def render(self, request: RepositoryContextRequest) -> str:
            requests.append(request)
            return "DEBUG SPEC CONTEXT\n\n"

    monkeypatch.setattr(
        debug_service_module,
        "resolve_repository_context_strategy",
        lambda name, **kwargs: Strategy(),
    )

    service.execute(_execution(tmp_path), DebugStage.SPEC_GENERATION)

    assert [request.role for request in requests] == ["planner"]


def test_spec_debug_accepts_direct_provider_inputs_without_execution(tmp_path: Path) -> None:
    execution = None

    class Provider:
        def get_task(self, task_id: str) -> DevelopmentTask:
            return _task()

    service, factory = _service(
        tmp_path, task_provider_factory=lambda source, repository: Provider()
    )

    result = service.execute(
        execution,
        DebugStage.SPEC_GENERATION,
        inputs=DebugInputs(
            task_source="trello",
            task_id="card-debug",
            repository="example",
        ),
    )

    assert result["execution_id"] is None
    assert result["result"]["spec"]["safe_to_implement"] is True
    assert factory.create_calls == 1


def test_dry_run_does_not_load_direct_provider_or_create_clients(tmp_path: Path) -> None:
    provider_calls: list[tuple[str, str | None]] = []

    def provider_factory(source: str, repository: str | None) -> Any:
        provider_calls.append((source, repository))
        raise AssertionError("dry-run must not create task providers")

    service, factory = _service(tmp_path, task_provider_factory=provider_factory)

    result = service.execute(
        None,
        DebugStage.SPEC_GENERATION,
        inputs=DebugInputs(
            task_source="trello",
            task_id="card-debug",
            repository="example",
        ),
        dry_run=True,
    )

    assert result["dry_run"] is True
    assert provider_calls == []
    assert factory.create_calls == 0


def test_implementation_debug_does_not_require_original_working_branch(tmp_path: Path) -> None:
    execution = _execution(tmp_path)
    execution.working_branch = None
    service, factory = _service(tmp_path)
    original = asdict(execution)

    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("implementation debug must not perform workflow Git operations")

    factory.git.create_branch = forbidden  # type: ignore[method-assign]
    factory.git.commit_all = forbidden  # type: ignore[method-assign]
    factory.git.push = forbidden  # type: ignore[method-assign]
    factory.git.current_branch = forbidden  # type: ignore[attr-defined]

    result = service.execute(execution, DebugStage.IMPLEMENTATION)

    assert result["result"]["implementation"]["success"] is True
    assert asdict(execution) == original
    assert factory.create_calls == 1


def test_implementation_debug_accepts_direct_prompt_without_execution(tmp_path: Path) -> None:
    service, factory = _service(tmp_path)

    result = service.execute(
        None,
        DebugStage.IMPLEMENTATION,
        inputs=DebugInputs(repository="example", prompt="Add a version flag"),
    )

    assert result["execution_id"] is None
    assert result["result"]["implementation"]["success"] is True
    assert factory.create_calls == 1


def test_validation_debug_accepts_direct_repository_without_execution(tmp_path: Path) -> None:
    service, factory = _service(tmp_path)

    result = service.execute(
        None,
        DebugStage.VALIDATION,
        inputs=DebugInputs(repository="example"),
    )

    assert result["execution_id"] is None
    assert result["result"]["validation"]["success"] is True
    assert factory.create_calls == 1


def test_validation_debug_returns_result_without_persisting_snapshot(tmp_path: Path) -> None:
    execution = _execution(tmp_path)
    service, _ = _service(tmp_path)
    original = execution.validation_snapshot

    result = service.execute(execution, DebugStage.VALIDATION)

    assert result["result"]["validation"]["success"] is True
    assert execution.validation_snapshot == original


def test_review_debug_reuses_all_persisted_inputs(tmp_path: Path) -> None:
    execution = _execution(tmp_path)
    service, factory = _service(tmp_path)
    factory.git.current_branch = lambda: "orchestrator/debug"  # type: ignore[attr-defined]

    result = service.execute(execution, DebugStage.REVIEW)

    assert result["result"]["review"]["result"] == "APPROVED"
    assert result["result"]["jules_sessions"][0]["id"] == "review-123"


def test_review_debug_uses_configured_repository_context_strategy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, factory = _service(tmp_path)
    factory.git.current_branch = lambda: "orchestrator/debug"  # type: ignore[attr-defined]
    requests: list[RepositoryContextRequest] = []

    class Strategy:
        def render(self, request: RepositoryContextRequest) -> str:
            requests.append(request)
            return "DEBUG REVIEW CONTEXT\n\n"

    monkeypatch.setattr(
        debug_service_module,
        "resolve_repository_context_strategy",
        lambda name, **kwargs: Strategy(),
    )

    service.execute(_execution(tmp_path), DebugStage.REVIEW)

    assert [request.role for request in requests] == ["reviewer"]


def test_review_debug_explicit_inputs_override_historical_defaults(tmp_path: Path) -> None:
    execution = _execution(tmp_path)
    service, factory = _service(tmp_path)

    result = service.execute(
        execution,
        DebugStage.REVIEW,
        inputs=DebugInputs(
            feature_branch="debug/explicit",
            diff="explicit diff",
        ),
    )

    assert result["result"]["review"]["result"] == "APPROVED"
    assert factory.planner.review_starting_branch == "debug/explicit"
