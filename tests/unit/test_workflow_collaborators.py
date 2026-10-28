"""Focused tests for collaborators without constructing WorkflowService."""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from prchef.config.project_config import WorkspaceSettings
from prchef.models.development_task import DevelopmentTask
from prchef.models.validation_result import ValidationResult
from prchef.models.workflow_event import WorkflowEventKind
from prchef.models.workflow_execution import WorkflowExecution
from prchef.models.workflow_status import (
    ReviewDecision,
    StepStatus,
    WorkflowStage,
    WorkflowStatus,
)
from prchef.services.exceptions import WorkflowError
from prchef.services.workflow_agent_runtime import WorkflowAgentRuntime
from prchef.services.workflow_correction_policy import CorrectionPolicy
from prchef.services.workflow_execution_lifecycle import ExecutionLifecycle
from prchef.services.workflow_stage_actions import StageActions
from prchef.services.workflow_stage_events import ExecutionRuntimes, StageEvents
from prchef.services.workflow_stage_sequencer import StageSequencer
from prchef.services.workflow_stage_values import StageValues


def commit_task(title: str, task_id: str = "9") -> DevelopmentTask:
    return DevelopmentTask(id=task_id, source="test", title=title, description="", url="")


@pytest.mark.parametrize("title", ["A short task", "x" * 62])
def test_commit_message_preserves_fitting_subject(title: str) -> None:
    assert StageValues._commit_message(commit_task(title)) == f"feat: {title} (9)"


@pytest.mark.parametrize(
    ("title", "shortened"),
    [
        ("x" * 63, "x" * 61),
        (
            "Show the best price per game, with a ranking that separates official and unofficial",
            "Show the best price per game, with a ranking that separates",
        ),
        (
            "Scaffold the Electron + React + TypeScript app with test, lint and typecheck",
            "Scaffold the Electron + React + TypeScript app with test,",
        ),
        ("A " + "x" * 70, "A"),
        ("Meaning " + "x" * 40 + " AND, (With) THE! " + "tail" * 10,
         "Meaning " + "x" * 40),
        ("and with the " * 10, "and with the and with the and with the and with the and with"),
    ],
)
def test_commit_message_shortens_at_word_boundaries(title: str, shortened: str) -> None:
    message = StageValues._commit_message(commit_task(title))
    subject, body = message.split("\n\n", 1)
    assert subject == f"feat: {shortened}… (9)"
    assert len(subject) <= 72
    assert body == title


@pytest.mark.parametrize("word", ["é", "界", "😀"])
def test_commit_message_counts_unicode_characters(word: str) -> None:
    title = word * 62
    assert StageValues._commit_message(commit_task(title)) == f"feat: {title} (9)"
    title += word
    assert StageValues._commit_message(commit_task(title)) == (
        f"feat: {word * 61}… (9)\n\n{title}"
    )


@pytest.mark.parametrize("id_length", [1, 30, 62, 63, 90])
def test_commit_message_preserves_reference_for_every_budget(id_length: int) -> None:
    task_id = "i" * id_length
    title = "word " * 30
    subject, body = StageValues._commit_message(commit_task(title, task_id)).split("\n\n", 1)
    minimal = f"feat: … ({task_id})"
    assert subject.endswith(f"… ({task_id})")
    assert len(subject) <= max(72, len(minimal))
    if id_length >= 62:
        assert subject == minimal
    assert body == title


def test_commit_message_normalizes_only_subject_whitespace() -> None:
    title = "  alpha\n\tbeta  " + "word  " * 20
    subject, body = StageValues._commit_message(commit_task(title)).split("\n\n", 1)
    assert subject.startswith("feat: alpha beta word word")
    assert "  " not in subject
    assert body == title


def test_commit_stage_passes_complete_message() -> None:
    task = commit_task("A long title " * 10)
    repository, events, git = Mock(), Mock(), Mock()
    repository.get_execution.return_value = SimpleNamespace(base_commit="base")
    events._step.return_value = nullcontext()
    git.commit_all.return_value = "sha"
    config = SimpleNamespace(repository=SimpleNamespace(base_branch="main", protected_branches=[]))
    StageActions(repository, events)._commit_changes(
        "one", SimpleNamespace(git=git), task, "feature/task", config
    )
    git.commit_all.assert_called_once_with(StageValues._commit_message(task), "feature/task", [])
    repository.record_commit.assert_called_once_with("one", "sha")


@pytest.mark.parametrize(
    ("recorded", "head", "matching_subject", "parent", "accepted"),
    [
        (None, "new", True, "base", True),
        (None, "new", False, "base", False),
        (None, "new", True, "other", False),
        (None, "base", True, "base", False),
        ("previous", "new", True, "previous", True),
        ("previous", "new", True, "base", False),
        ("new", "new", False, "other", True),
    ],
)
def test_commit_reconciliation_checks_subject_and_ancestry(
    recorded: str | None, head: str, matching_subject: bool, parent: str, accepted: bool
) -> None:
    task = commit_task("A long title " * 10)
    repository, events, git = Mock(), Mock(), Mock()
    repository.get_execution.return_value = SimpleNamespace(base_commit="base", commit_sha=recorded)
    events._step.return_value = nullcontext()
    git.changed_files.return_value = []
    git.head_commit.return_value = head
    git.head_commit_message.return_value = (
        StageValues._commit_message(task).split("\n", 1)[0] if matching_subject else "wrong"
    )
    git.head_parent_commit.return_value = parent
    config = SimpleNamespace(repository=SimpleNamespace(base_branch="main", protected_branches=[]))
    action = StageActions(repository, events)
    if accepted:
        action._commit_changes("one", SimpleNamespace(git=git), task, "feature", config,
                               reconcile=True)
        repository.record_commit.assert_called_once_with("one", head)
    else:
        with pytest.raises(WorkflowError, match="Cannot verify"):
            action._commit_changes("one", SimpleNamespace(git=git), task, "feature", config,
                                   reconcile=True)
        repository.record_commit.assert_not_called()
    git.ensure_merge_base.assert_called_once_with("base")
    git.commit_all.assert_not_called()


@pytest.mark.parametrize(
    ("stage", "exists", "retained_branch"),
    [
        (WorkflowStage.PREPARE_BRANCH, True, True),
        (WorkflowStage.PREPARE_BRANCH, False, True),
        (WorkflowStage.PREPARE_BRANCH, False, False),
        (WorkflowStage.IMPLEMENT, True, True),
        (WorkflowStage.REVIEW, True, True),
    ],
)
def test_interrupted_recovery_uses_recorded_legacy_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    stage: WorkflowStage, exists: bool, retained_branch: bool,
) -> None:
    control = tmp_path / "control"
    control.mkdir()
    legacy = tmp_path / "prchef-workspaces" / control.name / "one"
    if exists:
        legacy.mkdir(parents=True)
    execution = WorkflowExecution(
        id="one", status=WorkflowStatus.INTERRUPTED, resume_stage=stage,
        workspace_path=str(legacy), working_branch="orchestrator/one", base_commit="base",
        task_snapshot={"id": "task", "source": "test", "title": "Task",
                       "description": "", "url": ""},
        spec_snapshot={"technical_context": "", "current_behavior": ""},
    )
    config = SimpleNamespace(
        path=control, workspace=WorkspaceSettings(),
        repository=SimpleNamespace(base_branch="develop", protected_branches=["develop"]),
    )
    repository = Mock()
    repository.list_steps.return_value = []
    loader = Mock()
    loader.load.return_value = config
    git = Mock()
    git.github_repository.return_value = "owner/repo"
    git.branch_exists.return_value = retained_branch
    git.create_worktree.side_effect = lambda path, *args: path.mkdir()
    git.attach_existing_worktree.side_effect = lambda path, *args: path.mkdir(parents=True)
    factory = Mock()
    factory.create.return_value = SimpleNamespace(git=git)
    events = Mock()
    events._step.side_effect = lambda *args: nullcontext()
    lifecycle = Mock()
    lifecycle._runtime_overrides_for_execution.return_value = ({}, {})
    sequencer = StageSequencer(
        repository, Mock(), loader, factory, events, Mock(), Mock(), lifecycle, Mock()
    )
    monkeypatch.setattr(sequencer, "_repository_context_strategy", Mock())
    sentinel = object()
    implement = Mock(return_value=sentinel)
    monkeypatch.setattr(sequencer, "_implement_and_review", implement)

    result = sequencer._execute_interrupted(execution)

    assert result is sentinel, repository.reinterrupt.call_args
    assert implement.call_args.args[6] == legacy
    assert execution.workspace_path == str(legacy)
    assert not (Path.home() / ".prchef" / "workspaces").exists()
    git.at_path.assert_called_with(legacy)
    if stage is WorkflowStage.PREPARE_BRANCH:
        repository.record_workspace.assert_called_once_with("one", str(legacy))
        if not exists and retained_branch:
            git.attach_existing_worktree.assert_called_once_with(
                legacy, "orchestrator/one", "base"
            )
        elif not exists:
            assert git.create_worktree.call_args.args[0] == legacy
    if exists:
        git.create_worktree.assert_not_called()
        git.attach_existing_worktree.assert_not_called()


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
        "prchef.services.workflow_correction_policy.ValidationService", Validation
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

    monkeypatch.setattr("prchef.services.workflow_stage_sequencer.SpecService", Spec)
    monkeypatch.setattr(
        "prchef.services.workflow_stage_sequencer.WorkspaceManager", Workspace
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
