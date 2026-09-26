from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text

from orchestrator.config.database import build_engine, build_session_factory
from orchestrator.config.project_config import WorkspaceSettings
from orchestrator.models import (
    DevelopmentTask,
    DiagnosticCategory,
    ExecutionDiagnostic,
    PolishOutcome,
    StepStatus,
    WorkflowStage,
    WorkflowStatus,
)
from orchestrator.repositories.workflow_repository import WorkflowRepository
from orchestrator.services.workflow_service import WorkflowService
from orchestrator.workspace.manager import WorkspaceManager


def test_repository_persists_execution_error_and_steps_after_alembic_upgrade(
    tmp_path: Path,
) -> None:
    project_root = Path(__file__).resolve().parents[2]
    database = tmp_path / "workflow.db"
    database_url = f"sqlite:///{database.as_posix()}"
    alembic_config = Config(str(project_root / "alembic.ini"))
    alembic_config.set_main_option(
        "script_location", str(project_root / "src" / "orchestrator" / "migrations")
    )
    alembic_config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(alembic_config, "head")

    repository = WorkflowRepository(build_session_factory(build_engine(database_url)))
    execution = repository.create_execution("card-123")
    task = DevelopmentTask(
        id="card-123",
        source="trello",
        title="Task title",
        description="Original description",
        url="https://trello.example/card-123",
        repository="api",
        metadata={"custom_fields": {"Repository": "api"}},
    )
    repository.update_task(execution.id, task)
    repository.update_project(execution.id, "/repos/api", "develop")
    repository.record_workspace(execution.id, "/workspaces/api/execution-1")
    repository.update_status(execution.id, WorkflowStatus.RUNNING)
    repository.update_stage(execution.id, WorkflowStage.GENERATE_SPEC)
    repository.record_spec_jules_session(execution.id, "spec-1", "https://jules.example/spec-1")
    step_id = repository.start_step(execution.id, WorkflowStage.GENERATE_SPEC)
    repository.finish_step(step_id, StepStatus.FAILED, "jules unavailable")
    repository.record_error(execution.id, "jules unavailable")
    repository.finish(execution.id, WorkflowStatus.FAILED)

    persisted = repository.get_execution(execution.id)
    steps = repository.list_steps(execution.id)

    assert persisted.trello_card_url == task.url
    assert persisted.task_source == "trello"
    assert persisted.task_id == "card-123"
    assert persisted.task_url == task.url
    assert persisted.task_snapshot is not None
    assert persisted.task_snapshot["description"] == "Original description"
    assert persisted.repository == "/repos/api"
    assert persisted.repository_ref == ""
    assert persisted.workspace_path == "/workspaces/api/execution-1"
    assert persisted.spec_jules_session_id == "spec-1"
    assert persisted.spec_jules_session_url == "https://jules.example/spec-1"
    assert persisted.status is WorkflowStatus.FAILED
    assert persisted.error_message == "jules unavailable"
    assert persisted.finished_at is not None
    assert len(steps) == 1
    assert steps[0].stage is WorkflowStage.GENERATE_SPEC
    assert steps[0].status is StepStatus.FAILED
    assert steps[0].error_message == "jules unavailable"

    github_execution = repository.create_execution("42", task_source="github")
    github_task = DevelopmentTask(
        id="42",
        source="github",
        title="Issue title",
        description="Issue body",
        url="https://github.com/owner/api/issues/42",
        repository="owner/api",
        metadata={"labels": ["ai-ready"]},
    )
    repository.update_task(github_execution.id, github_task)
    persisted_github = repository.get_execution(github_execution.id)

    assert persisted_github.task_source == "github"
    assert persisted_github.task_id == "42"
    assert persisted_github.task_url == github_task.url
    assert persisted_github.trello_card_id == ""

    polish_step_id = repository.start_step(github_execution.id, WorkflowStage.POLISH)
    repository.finish_step(polish_step_id, StepStatus.SUCCEEDED)

    polish_steps = repository.list_steps(github_execution.id)
    assert [(step.stage, step.status) for step in polish_steps] == [
        (WorkflowStage.POLISH, StepStatus.SUCCEEDED)
    ]

    repository.record_polish_outcome(github_execution.id, PolishOutcome.SKIPPED)
    repository.record_polish_skip_comment_attempt(github_execution.id)
    persisted_polish = repository.get_execution(github_execution.id)
    assert persisted_polish.polish_outcome is PolishOutcome.SKIPPED
    assert persisted_polish.polish_skip_comment_attempted is True


def test_cancelled_execution_persists_across_repository_restart(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[2]
    database_url = f"sqlite:///{(tmp_path / 'workflow.db').as_posix()}"
    alembic_config = Config(str(project_root / "alembic.ini"))
    alembic_config.set_main_option(
        "script_location", str(project_root / "src" / "orchestrator" / "migrations")
    )
    alembic_config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(alembic_config, "head")
    repository = WorkflowRepository(build_session_factory(build_engine(database_url)))
    execution = repository.create_execution("card-cancelled")
    repository.update_status(execution.id, WorkflowStatus.RUNNING)
    repository.record_workspace(execution.id, "/workspaces/cancelled")
    repository.record_branch(execution.id, "orchestrator/cancelled")

    assert repository.request_cancellation(execution.id) is True
    repository.finish(execution.id, WorkflowStatus.CANCELLED)

    restarted = WorkflowRepository(build_session_factory(build_engine(database_url)))
    persisted = restarted.get_execution(execution.id)
    assert persisted.status is WorkflowStatus.CANCELLED
    assert persisted.cancellation_requested_at is not None
    assert persisted.workspace_path == "/workspaces/cancelled"
    assert persisted.working_branch == "orchestrator/cancelled"
    assert restarted.request_cancellation(execution.id) is False


def test_human_interactions_persist_response_and_resume_state(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[2]
    database_url = f"sqlite:///{(tmp_path / 'workflow.db').as_posix()}"
    alembic_config = Config(str(project_root / "alembic.ini"))
    alembic_config.set_main_option(
        "script_location", str(project_root / "src" / "orchestrator" / "migrations")
    )
    alembic_config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(alembic_config, "head")
    repository = WorkflowRepository(build_session_factory(build_engine(database_url)))
    execution = repository.create_execution("card-human")
    repository.update_stage(execution.id, WorkflowStage.GENERATE_SPEC)
    repository.record_error(execution.id, "Which retention policy should apply?")
    interaction = repository.request_human_input(
        execution.id, WorkflowStage.GENERATE_SPEC, "Which retention policy should apply?"
    )
    repository.finish(execution.id, WorkflowStatus.NEEDS_HUMAN)
    repository.respond_to_human_input(execution.id, "Use 30 days.")
    repository.resume_execution(execution.id)

    persisted = repository.get_execution(execution.id)
    assert persisted.status is WorkflowStatus.RUNNING
    assert persisted.finished_at is None
    assert persisted.human_interactions[0].id == interaction.id
    assert persisted.human_interactions[0].prompt == "Which retention policy should apply?"
    assert persisted.human_interactions[0].response == "Use 30 days."
    assert persisted.human_interactions[0].responded_at is not None


def test_execution_diagnostics_are_redacted_and_available_after_repository_restart(
    tmp_path: Path,
) -> None:
    project_root = Path(__file__).resolve().parents[2]
    database_url = f"sqlite:///{(tmp_path / 'workflow.db').as_posix()}"
    alembic_config = Config(str(project_root / "alembic.ini"))
    alembic_config.set_main_option(
        "script_location", str(project_root / "src" / "orchestrator" / "migrations")
    )
    alembic_config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(alembic_config, "head")
    repository = WorkflowRepository(build_session_factory(build_engine(database_url)))
    execution = repository.create_execution("diagnostics")
    repository.record_diagnostic(
        ExecutionDiagnostic(
            execution_id=execution.id,
            category=DiagnosticCategory.VALIDATION,
            stage=WorkflowStage.VALIDATE,
            payload={"command": "pytest", "stderr": "Authorization: Bearer secret"},
        )
    )

    restarted = WorkflowRepository(build_session_factory(build_engine(database_url)))
    diagnostics = restarted.list_diagnostics(execution.id)

    assert diagnostics[0].category is DiagnosticCategory.VALIDATION
    assert diagnostics[0].payload["command"] == "pytest"
    assert diagnostics[0].payload["stderr"] == "Authorization=[REDACTED] [REDACTED]"


def test_migration_backfills_common_identity_for_existing_trello_execution(
    tmp_path: Path,
) -> None:
    project_root = Path(__file__).resolve().parents[2]
    database_url = f"sqlite:///{(tmp_path / 'legacy.db').as_posix()}"
    alembic_config = Config(str(project_root / "alembic.ini"))
    alembic_config.set_main_option(
        "script_location", str(project_root / "src" / "orchestrator" / "migrations")
    )
    alembic_config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(alembic_config, "0002")

    engine = build_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workflow_executions "
                "(id, trello_card_id, trello_card_url, repository, base_branch, "
                "current_stage, status, created_at, updated_at) "
                "VALUES (:id, :card_id, :card_url, :repository, :base_branch, "
                ":stage, :status, :created_at, :updated_at)"
            ),
            {
                "id": "legacy-execution",
                "card_id": "legacy-card",
                "card_url": "https://trello.example/legacy-card",
                "repository": "/repos/api",
                "base_branch": "develop",
                "stage": WorkflowStage.LOAD_TASK.value,
                "status": WorkflowStatus.FAILED.value,
                "created_at": "2026-08-28 10:00:00",
                "updated_at": "2026-08-28 10:00:00",
            },
        )
    engine.dispose()

    command.upgrade(alembic_config, "head")
    repository = WorkflowRepository(
        build_session_factory(build_engine(database_url))
    )

    persisted = repository.get_execution("legacy-execution")

    assert persisted.task_source == "trello"
    assert persisted.task_id == "legacy-card"
    assert persisted.task_url == "https://trello.example/legacy-card"
    assert persisted.trello_card_id == "legacy-card"


def _migrated_repository(tmp_path: Path, name: str) -> WorkflowRepository:
    project_root = Path(__file__).resolve().parents[2]
    database_url = f"sqlite:///{(tmp_path / name).as_posix()}"
    alembic_config = Config(str(project_root / "alembic.ini"))
    alembic_config.set_main_option(
        "script_location", str(project_root / "src" / "orchestrator" / "migrations")
    )
    alembic_config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(alembic_config, "head")
    return WorkflowRepository(build_session_factory(build_engine(database_url)))


def test_waiting_status_round_trips_without_a_new_migration(tmp_path: Path) -> None:
    """The status column is string-backed, so WAITING needs no schema change."""

    repository = _migrated_repository(tmp_path, "waiting.db")
    execution = repository.create_execution("card-waiting")

    repository.update_status(execution.id, WorkflowStatus.WAITING)

    persisted = repository.get_execution(execution.id)
    assert persisted.status is WorkflowStatus.WAITING


@pytest.mark.parametrize(
    "status", [WorkflowStatus.PENDING, WorkflowStatus.WAITING, WorkflowStatus.RUNNING]
)
def test_cancellation_is_accepted_for_every_active_status(
    tmp_path: Path, status: WorkflowStatus
) -> None:
    repository = _migrated_repository(tmp_path, f"cancel-{status.value.lower()}.db")
    execution = repository.create_execution("card-active")
    repository.update_status(execution.id, status)

    assert repository.request_cancellation(execution.id) is True
    assert repository.cancellation_requested(execution.id) is True
    assert repository.get_execution(execution.id).cancellation_requested_at is not None
    # Repeating the request is idempotent rather than an error.
    assert repository.request_cancellation(execution.id) is True


@pytest.mark.parametrize(
    "status",
    [
        WorkflowStatus.APPROVED,
        WorkflowStatus.CHANGES_REQUESTED,
        WorkflowStatus.CANCELLED,
        WorkflowStatus.FAILED,
    ],
)
def test_cancellation_is_rejected_for_terminal_statuses(
    tmp_path: Path, status: WorkflowStatus
) -> None:
    repository = _migrated_repository(tmp_path, f"terminal-{status.value.lower()}.db")
    execution = repository.create_execution("card-terminal")
    repository.finish(execution.id, status)

    assert repository.request_cancellation(execution.id) is False
    assert repository.cancellation_requested(execution.id) is False


def test_human_blocked_execution_can_be_cancelled_immediately(tmp_path: Path) -> None:
    repository = _migrated_repository(tmp_path, "cancel-human.db")
    execution = repository.create_execution("card-human")
    repository.finish(execution.id, WorkflowStatus.NEEDS_HUMAN)

    assert repository.request_cancellation(execution.id) is True
    persisted = repository.get_execution(execution.id)
    assert persisted.status is WorkflowStatus.CANCELLED
    assert persisted.cancellation_requested_at is not None
    assert repository.request_cancellation(execution.id) is False


def test_legacy_human_row_with_cancellation_marker_can_settle(tmp_path: Path) -> None:
    repository = _migrated_repository(tmp_path, "legacy-human-cancel.db")
    execution = repository.create_execution("card-human")
    repository.request_human_input(execution.id, WorkflowStage.REVIEW, "Review finding?")
    repository.update_status(execution.id, WorkflowStatus.RUNNING)
    assert repository.request_cancellation(execution.id)
    # Reproduce the old finish race: NEEDS_HUMAN was written after the request.
    repository.update_status(execution.id, WorkflowStatus.NEEDS_HUMAN)

    with pytest.raises(ValueError, match="not awaiting human input"):
        repository.respond_to_human_input(execution.id, "Proceed")
    assert repository.request_cancellation(execution.id)
    persisted = repository.get_execution(execution.id)
    assert persisted.status is WorkflowStatus.CANCELLED
    assert persisted.finished_at is not None
    assert repository.list_human_interactions(execution.id)[0].response is None


@pytest.mark.parametrize("status", [WorkflowStatus.NEEDS_HUMAN, WorkflowStatus.APPROVED])
def test_finish_honors_cancellation_requested_after_last_check(
    tmp_path: Path, status: WorkflowStatus
) -> None:
    repository = _migrated_repository(tmp_path, f"cancel-at-finish-{status.value.lower()}.db")
    execution = repository.create_execution("card-finishing")
    repository.update_status(execution.id, WorkflowStatus.RUNNING)
    repository.update_stage(execution.id, WorkflowStage.REVIEW)

    assert repository.request_cancellation(execution.id)
    repository.finish(execution.id, status)

    persisted = repository.get_execution(execution.id)
    assert persisted.status is WorkflowStatus.CANCELLED
    assert persisted.current_stage is WorkflowStage.REVIEW
    assert persisted.finished_at is not None


def test_queue_execution_returns_a_resumed_execution_to_pending(tmp_path: Path) -> None:
    repository = _migrated_repository(tmp_path, "requeue.db")
    execution = repository.create_execution("card-resume")
    repository.record_error(execution.id, "needs a human answer")
    repository.finish(execution.id, WorkflowStatus.NEEDS_HUMAN)

    repository.queue_execution(execution.id)

    persisted = repository.get_execution(execution.id)
    assert persisted.status is WorkflowStatus.PENDING
    assert persisted.finished_at is None
    assert persisted.error_message is None
    # A queued resume is cancellable again before it consumes any capacity.
    assert persisted.cancellation_requested_at is None
    assert repository.request_cancellation(execution.id) is True


def test_human_answer_and_cancellation_cannot_overwrite_each_other(tmp_path: Path) -> None:
    database = tmp_path / "human-cancel-race.db"
    first = _migrated_repository(tmp_path, database.name)
    second = WorkflowRepository(build_session_factory(build_engine(f"sqlite:///{database.as_posix()}")))

    cancelled = first.create_execution("cancel-first")
    first.request_human_input(cancelled.id, WorkflowStage.REVIEW, "Review finding?")
    first.finish(cancelled.id, WorkflowStatus.NEEDS_HUMAN)
    stale_view = second.get_execution(cancelled.id)
    assert stale_view.status is WorkflowStatus.NEEDS_HUMAN
    assert first.request_cancellation(cancelled.id)
    with pytest.raises(ValueError, match="not awaiting human input"):
        second.respond_to_human_input(cancelled.id, "False positive")
    with pytest.raises(ValueError, match="cancelled"):
        second.queue_execution(cancelled.id)
    assert second.get_execution(cancelled.id).status is WorkflowStatus.CANCELLED
    assert second.list_human_interactions(cancelled.id)[0].response is None

    answered = first.create_execution("answer-first")
    first.request_human_input(answered.id, WorkflowStage.REVIEW, "Review finding?")
    first.finish(answered.id, WorkflowStatus.NEEDS_HUMAN)
    second.respond_to_human_input(answered.id, "Proceed")
    assert first.get_execution(answered.id).status is WorkflowStatus.PENDING
    assert first.request_cancellation(answered.id)
    persisted = second.get_execution(answered.id)
    assert persisted.status is WorkflowStatus.PENDING
    assert persisted.cancellation_requested_at is not None
    assert second.list_human_interactions(answered.id)[0].response == "Proceed"
    with pytest.raises(ValueError, match="cancelled"):
        second.queue_execution(answered.id)


def test_workflow_batches_round_trip_order_links_and_restart(tmp_path: Path) -> None:
    database_url = f"sqlite:///{(tmp_path / 'batches.db').as_posix()}"
    project_root = Path(__file__).resolve().parents[2]
    alembic_config = Config(str(project_root / "alembic.ini"))
    alembic_config.set_main_option(
        "script_location", str(project_root / "src" / "orchestrator" / "migrations")
    )
    alembic_config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(alembic_config, "head")
    repository = WorkflowRepository(build_session_factory(build_engine(database_url)))
    tasks = [
        DevelopmentTask("12", "github", "First task", "", "https://example/12"),
        DevelopmentTask("7", "github", "Second task", "", "https://example/7"),
    ]

    batch = repository.create_batch("example", "github", tasks)
    execution = repository.create_execution("12", "github")
    repository.record_submission_repository(execution.id, "example")
    repository.record_batch_item_execution(batch.id, 0, execution.id)

    restarted = WorkflowRepository(build_session_factory(build_engine(database_url)))
    persisted = restarted.get_batch(batch.id)

    assert [item.task_id for item in persisted.items] == ["12", "7"]
    assert [item.task_title for item in persisted.items] == ["First task", "Second task"]
    assert persisted.items[0].execution_id == execution.id
    assert persisted.items[1].execution_id is None
    assert [stored.id for stored in restarted.list_batches()] == [batch.id]

    second_execution = restarted.create_execution("7", "github")
    restarted.update_status(second_execution.id, WorkflowStatus.RUNNING)
    assert restarted.execution_statuses(
        [execution.id, second_execution.id, "missing-execution"]
    ) == {
        execution.id: WorkflowStatus.PENDING,
        second_execution.id: WorkflowStatus.RUNNING,
    }
    assert restarted.execution_statuses([]) == {}

    restarted.delete_batch(batch.id)
    assert restarted.list_batches() == []
    assert restarted.get_execution(execution.id).id == execution.id
    engine = build_engine(database_url)
    with engine.connect() as connection:
        count = connection.execute(text("SELECT COUNT(*) FROM workflow_batch_items")).scalar_one()
    engine.dispose()
    assert count == 0


def test_executions_for_tasks_returns_latest_in_the_selected_repository(
    tmp_path: Path,
) -> None:
    repository = _migrated_repository(tmp_path, "task-lookups.db")
    older = repository.create_execution("12", "github")
    repository.record_submission_repository(older.id, "example")
    newer = repository.create_execution("12", "github")
    repository.record_submission_repository(newer.id, "example")
    other_repository = repository.create_execution("12", "github")
    repository.record_submission_repository(other_repository.id, "other")

    matches = repository.executions_for_tasks("github", ["12"], "example")

    assert matches["12"].id == newer.id


@pytest.mark.parametrize(
    ("status", "allowed"),
    [
        (WorkflowStatus.FAILED, True),
        (WorkflowStatus.CANCELLED, True),
        (WorkflowStatus.NEEDS_HUMAN, True),
        (WorkflowStatus.CHANGES_REQUESTED, True),
        (WorkflowStatus.PENDING, False),
        (WorkflowStatus.WAITING, False),
        (WorkflowStatus.RUNNING, False),
        (WorkflowStatus.APPROVED, False),
    ],
)
def test_fresh_execution_admission_uses_latest_repository_scoped_status(
    tmp_path: Path, status: WorkflowStatus, allowed: bool
) -> None:
    repository = _migrated_repository(tmp_path, f"fresh-{status.value}.db")
    older = repository.create_execution("12", "github", repository_ref="example")
    repository.update_status(older.id, status)

    if allowed:
        fresh = repository.create_execution("12", "github", repository_ref="example")
        assert fresh.id != older.id
        assert repository.get_execution(older.id).status == status
        assert repository.executions_for_tasks("github", ["12"], "example")["12"].id == fresh.id
        with pytest.raises(ValueError, match="PENDING"):
            repository.create_execution("12", "github", repository_ref="example")
    else:
        with pytest.raises(ValueError, match=status.value):
            repository.create_execution("12", "github", repository_ref="example")
        assert repository.get_execution(older.id).status == status

    # Neither another repository nor another source shares this admission gate.
    repository.create_execution("12", "github", repository_ref="other")
    repository.create_execution("12", "trello", repository_ref="example")


def test_service_fresh_run_reloads_edited_task_and_keeps_earlier_execution(
    tmp_path: Path,
) -> None:
    repository = _migrated_repository(tmp_path, "service-fresh.db")
    description = ["original description"]

    class Provider:
        source = "github"

        def get_task(self, task_id: str) -> DevelopmentTask:
            if task_id == "14":
                raise RuntimeError("provider unavailable")
            return DevelopmentTask(
                task_id, "github", "Current task", description[0], "https://example/12",
                repository="example",
            )

    class FailingConfig:
        def load(self, task: DevelopmentTask, repository_override: str | None):
            raise RuntimeError("stop after loading the current task")

    service = WorkflowService(
        repository, Provider(), FailingConfig(), object(),  # type: ignore[arg-type]
    )
    first = service.run("12", "example")
    assert first.status == WorkflowStatus.FAILED
    description[0] = "edited description"

    second = service.run("12", "example")

    assert second.status == WorkflowStatus.FAILED
    assert second.id != first.id
    assert second.task_snapshot is not None
    assert second.task_snapshot["description"] == "edited description"
    assert repository.get_execution(first.id).task_snapshot["description"] == "original description"
    assert repository.executions_for_tasks("github", ["12"], "example")["12"].id == second.id
    pending = repository.create_execution("13", "github", repository_ref="example")
    with pytest.raises(ValueError, match="PENDING"):
        service.submit_execution("13")
    assert repository.get_execution(pending.id).status == WorkflowStatus.PENDING
    unavailable_pending = repository.create_execution("14", "github", repository_ref="example")
    with pytest.raises(ValueError, match="PENDING"):
        service.submit_execution("14")
    assert repository.get_execution(unavailable_pending.id).status == WorkflowStatus.PENDING


def test_fresh_runs_keep_separate_branch_and_worktree_records(tmp_path: Path) -> None:
    repository = _migrated_repository(tmp_path, "fresh-workspaces.db")
    manager = WorkspaceManager(
        object(), tmp_path / "control", WorkspaceSettings(root=tmp_path / "worktrees")
    )  # type: ignore[arg-type]
    first = repository.create_execution("12", "github", repository_ref="example")
    first_branch = WorkflowService._branch_name("orchestrator", "12", "Fix task", first.id)
    first_path = manager.workspace_path(first.id)
    repository.record_branch(first.id, first_branch)
    repository.record_workspace(first.id, str(first_path))
    repository.update_status(first.id, WorkflowStatus.FAILED)

    second = repository.create_execution("12", "github", repository_ref="example")
    second_branch = WorkflowService._branch_name("orchestrator", "12", "Fix task", second.id)
    second_path = manager.workspace_path(second.id)
    repository.record_branch(second.id, second_branch)
    repository.record_workspace(second.id, str(second_path))

    assert first_branch != second_branch
    assert first_path != second_path
    assert repository.get_execution(first.id).working_branch == first_branch
    assert repository.get_execution(first.id).workspace_path == str(first_path)
    assert repository.get_execution(first.id).status == WorkflowStatus.FAILED
    assert repository.get_execution(second.id).working_branch == second_branch
    assert repository.executions_for_tasks("github", ["12"], "example")["12"].id == second.id


def test_workflow_batch_migration_downgrades_to_0008(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[2]
    database_url = f"sqlite:///{(tmp_path / 'batch-downgrade.db').as_posix()}"
    alembic_config = Config(str(project_root / "alembic.ini"))
    alembic_config.set_main_option(
        "script_location", str(project_root / "src" / "orchestrator" / "migrations")
    )
    alembic_config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, "0008")

    engine = build_engine(database_url)
    tables = inspect(engine).get_table_names()
    engine.dispose()

    assert "workflow_batches" not in tables
    assert "workflow_batch_items" not in tables
    assert "workflow_executions" in tables
