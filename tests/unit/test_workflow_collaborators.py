"""Focused tests for collaborators without constructing WorkflowService."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from orchestrator.models.validation_result import ValidationResult
from orchestrator.models.workflow_event import WorkflowEventKind
from orchestrator.models.workflow_status import (
    ReviewDecision,
    StepStatus,
    WorkflowStage,
    WorkflowStatus,
)
from orchestrator.services.workflow_agent_runtime import WorkflowAgentRuntime
from orchestrator.services.workflow_correction_policy import CorrectionPolicy
from orchestrator.services.workflow_execution_lifecycle import ExecutionLifecycle
from orchestrator.services.workflow_stage_actions import StageActions
from orchestrator.services.workflow_stage_events import ExecutionRuntimes, StageEvents
from orchestrator.services.workflow_stage_sequencer import StageSequencer
from orchestrator.services.workflow_stage_values import StageValues


def test_stage_events_persist_before_delivery_and_isolate_execution_state() -> None:
    calls: list[tuple[Any, ...]] = []

    class Repository:
        def cancellation_requested(self, execution_id: str) -> bool:
            return False

        def update_stage(self, execution_id: str, stage: WorkflowStage) -> None:
            calls.append(("stage", execution_id, stage))

        def start_step(self, execution_id: str, stage: WorkflowStage) -> int:
            calls.append(("start", execution_id, stage))
            return 7

        def finish_step(self, step_id: int, status: StepStatus, error: str | None = None) -> None:
            calls.append(("finish", step_id, status))

        def record_diagnostic(self, diagnostic: Any) -> None:
            calls.append(("diagnostic", diagnostic.payload["kind"]))

    runtimes = ExecutionRuntimes()
    runtimes.begin("one", lambda event: calls.append(("listener", event.kind)))
    runtimes.begin("two", None)
    events = StageEvents(Repository(), runtimes)  # type: ignore[arg-type]
    with events._step("one", WorkflowStage.IMPLEMENT):
        assert events._active_stage_of("one") is WorkflowStage.IMPLEMENT
        assert events._active_stage_of("two") is None
    assert events._active_stage_of("one") is None
    assert calls == [
        ("stage", "one", WorkflowStage.IMPLEMENT),
        ("start", "one", WorkflowStage.IMPLEMENT),
        ("diagnostic", WorkflowEventKind.STAGE_STARTED.value),
        ("listener", WorkflowEventKind.STAGE_STARTED),
        ("finish", 7, StepStatus.SUCCEEDED),
        ("diagnostic", WorkflowEventKind.STAGE_FINISHED.value),
        ("listener", WorkflowEventKind.STAGE_FINISHED),
    ]


def test_correction_policy_retries_validation_once_with_fakes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stages: list[WorkflowStage] = []
    persisted: list[tuple[str, bool]] = []
    failed = ValidationResult(success=False, error_message="failed")
    passed = ValidationResult(success=True)
    results = iter([failed, passed])

    class Validation:
        def __init__(self, commands: object) -> None:
            assert commands is not None

        def validate(self, *_: Any, **__: Any) -> ValidationResult:
            return next(results)

    class Events:
        @contextmanager
        def _step(self, execution_id: str, stage: WorkflowStage) -> Any:
            stages.append(stage)
            yield

        def _record_diagnostic(self, *_: Any, **__: Any) -> None:
            pass

    class Repository:
        def record_validation(self, execution_id: str, result: ValidationResult) -> None:
            persisted.append(("validation", result.success))

        def record_implementation(self, execution_id: str, result: Any) -> None:
            persisted.append(("implementation", result.success))

    class Implementation:
        def correct_validation_failures(self, *_: Any, **__: Any) -> Any:
            return SimpleNamespace(success=True)

    monkeypatch.setattr(
        "orchestrator.services.workflow_correction_policy.ValidationService", Validation
    )
    policy = CorrectionPolicy(Repository(), Events(), object())  # type: ignore[arg-type]
    config = SimpleNamespace(
        validation=SimpleNamespace(max_validation_retries=1),
        safety=SimpleNamespace(require_tests=False),
        path=Path("repo"),
    )
    actual = policy._validate_with_corrections(
        execution_id="one",
        execution_clients=SimpleNamespace(commands=object()),
        implementation_service=Implementation(),
        task=object(),
        spec=object(),
        workspace_path=Path("repo"),
        config=config,
    )
    assert actual is passed
    assert stages == [WorkflowStage.VALIDATE, WorkflowStage.IMPLEMENT, WorkflowStage.VALIDATE]
    assert persisted == [("validation", False), ("implementation", True), ("validation", True)]


def test_correction_policy_owns_review_retry_and_push_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[Any] = []

    class Events:
        @contextmanager
        def _step(self, execution_id: str, stage: WorkflowStage) -> Any:
            calls.append(stage)
            yield

    class Repository:
        def record_implementation(self, execution_id: str, result: Any) -> None:
            calls.append("record implementation")

    class Implementation:
        def correct_review_findings(self, *_: Any, **__: Any) -> Any:
            calls.append("correct review")
            return SimpleNamespace(success=True)

    class Actions:
        def _commit_changes(self, *_: Any) -> None:
            calls.append("commit")

    policy = CorrectionPolicy(Repository(), Events(), Actions())  # type: ignore[arg-type]
    validation = ValidationResult(success=True)
    monkeypatch.setattr(
        policy,
        "_validate_with_corrections",
        lambda **kwargs: calls.append("validate") or validation,
    )
    config = SimpleNamespace(
        path=Path("repo"),
        review=SimpleNamespace(max_review_retries=1),
        repository=SimpleNamespace(protected_branches=[]),
    )
    review = SimpleNamespace(result=ReviewDecision.CHANGES_REQUESTED)
    assert policy._review_retry_available(review, 0, config)
    assert not policy._review_retry_available(review, 1, config)
    clients = SimpleNamespace(git=SimpleNamespace(push=lambda *_: calls.append("push")))
    result = policy._correct_review_findings(
        execution_id="one",
        execution_clients=clients,
        implementation_service=Implementation(),
        task=object(),
        spec=object(),
        review=review,
        workspace_path=Path("repo"),
        working_branch="feature/task",
        config=config,
        attempt=1,
        before_push=lambda: calls.append("check PR"),
    )
    assert result is validation
    assert calls == [
        WorkflowStage.IMPLEMENT,
        "correct review",
        "record implementation",
        "validate",
        "commit",
        WorkflowStage.PUSH,
        "check PR",
        "push",
    ]


def test_stage_sequencer_resumes_planning_in_stage_order(monkeypatch: pytest.MonkeyPatch) -> None:
    stages: list[WorkflowStage] = []
    records: list[str] = []
    spec = SimpleNamespace(has_blocking_ambiguity=False)

    class Events:
        @contextmanager
        def _step(self, execution_id: str, stage: WorkflowStage) -> Any:
            stages.append(stage)
            yield

    class Repository:
        def record_spec_jules_session(self, *_: Any) -> None:
            records.append("spec_session")

        def record_spec(self, *_: Any) -> None:
            records.append("spec")

        def record_workspace(self, *_: Any) -> None:
            records.append("workspace")

        def record_branch(self, *_: Any) -> None:
            records.append("branch")

    class Spec:
        def __init__(self, *_: Any) -> None:
            pass

        def generate(self, *_: Any, **__: Any) -> Any:
            return spec

    class Workspace:
        def __init__(self, *_: Any) -> None:
            pass

        def workspace_path(self, execution_id: str) -> Path:
            return Path("repo/worktree")

        def create(self, *_: Any) -> Path:
            return Path("repo/worktree")

    monkeypatch.setattr("orchestrator.services.workflow_stage_sequencer.SpecService", Spec)
    monkeypatch.setattr(
        "orchestrator.services.workflow_stage_sequencer.WorkspaceManager", Workspace
    )

    @contextmanager
    def snapshot(base: str) -> Any:
        yield Path("repo")

    git = SimpleNamespace(
        at_path=lambda path: SimpleNamespace(ensure_merge_base=lambda base: None),
        planning_snapshot=snapshot,
    )
    config = SimpleNamespace(
        path=Path("repo"),
        workspace=object(),
        repository=SimpleNamespace(
            branch_prefix="feature", base_branch="develop", protected_branches=[]
        ),
    )
    actions = SimpleNamespace(_branch_name=lambda *_: "feature/task")
    agents = SimpleNamespace(_diagnostic_agent=lambda *_: object())
    sequencer = StageSequencer(
        Repository(),
        object(),
        object(),
        object(),
        Events(),
        object(),
        actions,
        object(),
        agents,
    )  # type: ignore[arg-type]
    sentinel = object()

    def implement(*_: Any) -> object:
        records.append("implement")
        return sentinel

    monkeypatch.setattr(sequencer, "_implement_and_review", implement)
    result = sequencer._resume_planning(
        SimpleNamespace(id="one", base_commit=None),
        SimpleNamespace(id="task", title="Task"),
        config,
        SimpleNamespace(git=git, agent=lambda role: object()),
        "owner/repo",
        object(),
    )
    assert result is sentinel
    assert stages == [WorkflowStage.GENERATE_SPEC, WorkflowStage.PREPARE_BRANCH]
    assert records == ["spec", "workspace", "branch", "implement"]


def test_stage_actions_commit_records_sha_inside_stage() -> None:
    calls: list[Any] = []

    class Events:
        @contextmanager
        def _step(self, execution_id: str, stage: WorkflowStage) -> Any:
            calls.append(stage)
            yield

    class Repository:
        def get_execution(self, execution_id: str) -> Any:
            return SimpleNamespace(base_commit=None)

        def record_commit(self, execution_id: str, sha: str) -> None:
            calls.append((execution_id, sha))

    git = SimpleNamespace(
        ensure_merge_base=lambda base: calls.append(("base", base)),
        commit_all=lambda message, branch, protected: calls.append(("commit", branch)) or "sha",
    )
    StageActions(Repository(), Events())._commit_changes(  # type: ignore[arg-type]
        "one",
        SimpleNamespace(git=git),
        SimpleNamespace(id="task", title="A task"),
        "feature/task",
        SimpleNamespace(repository=SimpleNamespace(base_branch="develop", protected_branches=[])),
    )
    assert calls == [
        WorkflowStage.COMMIT,
        ("base", "develop"),
        ("commit", "feature/task"),
        ("one", "sha"),
    ]


def test_stage_values_render_review_prompt_without_repository_or_service() -> None:
    review = SimpleNamespace(
        summary="Please clarify",
        blocking_findings=["The edge case needs a decision"],
        findings=["An unrelated suggestion"],
    )
    assert StageValues._review_prompt(review) == (
        "Please clarify\n- The edge case needs a decision"
    )


def test_execution_lifecycle_finishes_before_events_and_notification() -> None:
    calls: list[Any] = []
    execution = SimpleNamespace(status=WorkflowStatus.RUNNING, current_stage=WorkflowStage.REVIEW)

    class Repository:
        def cancellation_requested(self, execution_id: str) -> bool:
            return False

        def finish(self, execution_id: str, status: WorkflowStatus) -> None:
            calls.append(("finish", status))
            execution.status = status

        def get_execution(self, execution_id: str) -> Any:
            return execution

    class Events:
        def _raise_if_cancellation_requested(self, execution_id: str) -> None:
            pass

        def _emit(self, execution_id: str, kind: WorkflowEventKind, **kwargs: Any) -> None:
            calls.append(("event", kind))

    notifications = SimpleNamespace(
        notify_outcome=lambda settings, result: calls.append(("notify", result.status))
    )
    lifecycle = ExecutionLifecycle(Repository(), Events(), notifications)  # type: ignore[arg-type]
    assert (
        lifecycle._finish("one", WorkflowStatus.APPROVED, SimpleNamespace(notifications=object()))
        is execution
    )
    assert calls == [
        ("finish", WorkflowStatus.APPROVED),
        ("event", WorkflowEventKind.STATUS_CHANGED),
        ("event", WorkflowEventKind.WORKFLOW_FINISHED),
        ("notify", WorkflowStatus.APPROVED),
    ]


def test_agent_runtime_records_provider_wait_and_admission() -> None:
    calls: list[Any] = []

    class Repository:
        def update_status(self, execution_id: str, status: WorkflowStatus) -> None:
            calls.append(("status", status))

    class Events:
        def _active_stage_of(self, execution_id: str) -> WorkflowStage:
            return WorkflowStage.IMPLEMENT

        def _emit(self, execution_id: str, kind: WorkflowEventKind, **kwargs: Any) -> None:
            calls.append(("event", kwargs["status"], kwargs["stage"]))

    agents = WorkflowAgentRuntime(Repository(), Events(), object(), object())  # type: ignore[arg-type]
    agents._record_provider_wait("one", "primary")
    agents._record_provider_admission("one", "primary")
    assert calls == [
        ("status", WorkflowStatus.WAITING),
        ("event", WorkflowStatus.WAITING, WorkflowStage.IMPLEMENT),
        ("status", WorkflowStatus.RUNNING),
        ("event", WorkflowStatus.RUNNING, WorkflowStage.IMPLEMENT),
    ]
