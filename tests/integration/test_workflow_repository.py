from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from unittest.mock import Mock, call

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import event, inspect, text

import prchef.repositories.workflow_repository as workflow_repository_module
from prchef.config.database import build_engine, build_session_factory
from prchef.config.project_config import ProjectConfigLoader, WorkspaceSettings
from prchef.models import (
    CodeReview,
    DevelopmentTask,
    DiagnosticCategory,
    ExecutionDiagnostic,
    PolishOutcome,
    ReviewDecision,
    StepStatus,
    WorkflowStage,
    WorkflowStatus,
)
from prchef.repositories.workflow_repository import (
    WorkflowRepository,
    _lock_fresh_run_admission,
)
from prchef.services.workflow_service import WorkflowService
from prchef.workspace.manager import WorkspaceManager


@pytest.mark.parametrize(
    "prior_status", [WorkflowStatus.PENDING, WorkflowStatus.WAITING, WorkflowStatus.RUNNING]
)
def test_recover_interrupted_is_idempotent_and_cancellable(
    tmp_path: Path, prior_status: WorkflowStatus
) -> None:
    project_root = Path(__file__).resolve().parents[2]
    database_url = f"sqlite:///{(tmp_path / 'recovery.db').as_posix()}"
    alembic_config = Config(str(project_root / "alembic.ini"))
    alembic_config.set_main_option(
        "script_location", str(project_root / "src" / "prchef" / "migrations")
    )
    alembic_config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(alembic_config, "head")

    repository = WorkflowRepository(build_session_factory(build_engine(database_url)))
    execution = repository.create_execution(f"task-{prior_status.value}")
    completed = repository.start_step(execution.id, WorkflowStage.LOAD_TASK)
    repository.finish_step(completed, StepStatus.SUCCEEDED)
    repository.update_stage(execution.id, WorkflowStage.GENERATE_SPEC)
    unfinished = repository.start_step(execution.id, WorkflowStage.GENERATE_SPEC)
    repository.update_status(execution.id, prior_status)

    recovered = repository.recover_interrupted()
    assert [item.id for item in recovered] == [execution.id]
    persisted = repository.get_execution(execution.id)
    assert persisted.status is WorkflowStatus.INTERRUPTED
    assert persisted.interrupted_from_status is prior_status
    assert persisted.resume_stage is WorkflowStage.GENERATE_SPEC
    assert repository.list_steps(execution.id)[-1].id == unfinished
    assert repository.list_steps(execution.id)[-1].status is StepStatus.INTERRUPTED
    assert repository.recover_interrupted() == []
    assert repository.get_execution(execution.id).interrupted_from_status is prior_status

    assert repository.request_cancellation(execution.id)
    assert repository.get_execution(execution.id).status is WorkflowStatus.CANCELLED


def test_recovery_uses_next_stage_after_completed_step(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[2]
    database_url = f"sqlite:///{(tmp_path / 'checkpoint.db').as_posix()}"
    alembic_config = Config(str(project_root / "alembic.ini"))
    alembic_config.set_main_option(
        "script_location", str(project_root / "src" / "prchef" / "migrations")
    )
    alembic_config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(alembic_config, "head")

    repository = WorkflowRepository(build_session_factory(build_engine(database_url)))
    execution = repository.create_execution("queued-after-task")
    repository.update_stage(execution.id, WorkflowStage.LOAD_TASK)
    step = repository.start_step(execution.id, WorkflowStage.LOAD_TASK)
    repository.finish_step(step, StepStatus.SUCCEEDED)
    assert repository.get_execution(execution.id).resume_stage is WorkflowStage.LOAD_CONFIG
    repository.recover_interrupted()
    assert repository.get_execution(execution.id).resume_stage is WorkflowStage.LOAD_CONFIG


def test_legacy_polish_and_completed_polish_push_keep_recoverable_checkpoints(
    tmp_path: Path,
) -> None:
    project_root = Path(__file__).resolve().parents[2]
    database_url = f"sqlite:///{(tmp_path / 'polish-recovery.db').as_posix()}"
    alembic_config = Config(str(project_root / "alembic.ini"))
    alembic_config.set_main_option(
        "script_location", str(project_root / "src" / "prchef" / "migrations")
    )
    alembic_config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(alembic_config, "head")
    repository = WorkflowRepository(build_session_factory(build_engine(database_url)))

    legacy = repository.create_execution("legacy-polish")
    repository.update_stage(legacy.id, WorkflowStage.POLISH)
    repository.start_step(legacy.id, WorkflowStage.POLISH)
    repository.update_status(legacy.id, WorkflowStatus.RUNNING)

    between = repository.create_execution("legacy-before-polish")
    review_step = repository.start_step(between.id, WorkflowStage.REVIEW)
    repository.finish_step(review_step, StepStatus.SUCCEEDED)
    repository.update_stage(between.id, WorkflowStage.POLISH)
    repository.update_status(between.id, WorkflowStatus.RUNNING)

    pushed = repository.create_execution("pushed-polish")
    repository.set_resume_mode(pushed.id, "POLISH")
    push_step = repository.start_step(pushed.id, WorkflowStage.PUSH)
    repository.finish_step(push_step, StepStatus.SUCCEEDED)
    repository.update_status(pushed.id, WorkflowStatus.RUNNING)
    assert repository.get_execution(pushed.id).resume_stage is WorkflowStage.REVIEW

    repository.recover_interrupted()
    assert repository.get_execution(legacy.id).resume_mode == "POLISH"
    assert repository.get_execution(legacy.id).resume_stage is WorkflowStage.POLISH
    assert repository.get_execution(between.id).resume_stage is WorkflowStage.POLISH
    assert repository.get_execution(between.id).resume_mode == "POLISH"
    assert repository.get_execution(pushed.id).resume_stage is WorkflowStage.REVIEW


def test_pending_execution_persists_selected_runtime_in_creation_transaction(
    tmp_path: Path,
) -> None:
    project_root = Path(__file__).resolve().parents[2]
    database_url = f"sqlite:///{(tmp_path / 'runtime-admission.db').as_posix()}"
    alembic_config = Config(str(project_root / "alembic.ini"))
    alembic_config.set_main_option(
        "script_location", str(project_root / "src" / "prchef" / "migrations")
    )
    alembic_config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(alembic_config, "head")
    repository = WorkflowRepository(build_session_factory(build_engine(database_url)))

    execution = repository.create_execution(
        "selected-provider",
        runtime_overrides={
            "kind": "RUNTIME_OVERRIDES_APPLIED",
            "provider_overrides": {"planner": "selected"},
            "runtime_overrides": {"planner": {"model": "selected-model"}},
        },
    )
    repository.recover_interrupted()
    persisted = repository.list_diagnostics(execution.id)
    assert len(persisted) == 1
    assert persisted[0].payload["provider_overrides"] == {"planner": "selected"}


def test_review_output_records_the_step_that_produced_it(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[2]
    database_url = f"sqlite:///{(tmp_path / 'review-provenance.db').as_posix()}"
    alembic_config = Config(str(project_root / "alembic.ini"))
    alembic_config.set_main_option(
        "script_location", str(project_root / "src" / "prchef" / "migrations")
    )
    alembic_config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(alembic_config, "head")

    repository = WorkflowRepository(build_session_factory(build_engine(database_url)))
    execution = repository.create_execution("review-provenance")
    first_step = repository.start_step(execution.id, WorkflowStage.REVIEW)
    repository.record_review(execution.id, CodeReview(result=ReviewDecision.APPROVED))
    assert repository.get_execution(execution.id).review_step_id == first_step
    repository.finish_step(first_step, StepStatus.SUCCEEDED)

    second_step = repository.start_step(execution.id, WorkflowStage.REVIEW)
    assert repository.get_execution(execution.id).review_step_id == first_step
    repository.record_review(execution.id, CodeReview(result=ReviewDecision.CHANGES_REQUESTED))
    assert repository.get_execution(execution.id).review_step_id == second_step


def test_upgrade_recovers_legacy_active_execution(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[2]
    database_url = f"sqlite:///{(tmp_path / 'legacy-active.db').as_posix()}"
    alembic_config = Config(str(project_root / "alembic.ini"))
    alembic_config.set_main_option(
        "script_location", str(project_root / "src" / "prchef" / "migrations")
    )
    alembic_config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(alembic_config, "0010")
    engine = build_engine(database_url)
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO workflow_executions "
            "(id, task_source, task_id, task_url, trello_card_id, trello_card_url, "
            "repository, repository_ref, base_branch, current_stage, status, "
            "created_at, updated_at) VALUES "
            "(:id, 'trello', 'legacy', '', 'legacy', '', '', '', '', "
            "'LOAD_TASK', 'PENDING', :now, :now)"
        ), {"id": "legacy-active", "now": now})
    command.upgrade(alembic_config, "head")
    repository = WorkflowRepository(build_session_factory(engine))
    assert repository.recover_interrupted()[0].status is WorkflowStatus.INTERRUPTED
    persisted = repository.get_execution("legacy-active")
    assert persisted.interrupted_from_status is WorkflowStatus.PENDING
    assert persisted.resume_stage is WorkflowStage.LOAD_TASK


def test_interrupted_batch_link_keeps_its_position(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[2]
    database_url = f"sqlite:///{(tmp_path / 'batch-recovery.db').as_posix()}"
    alembic_config = Config(str(project_root / "alembic.ini"))
    alembic_config.set_main_option(
        "script_location", str(project_root / "src" / "prchef" / "migrations")
    )
    alembic_config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(alembic_config, "head")

    repository = WorkflowRepository(build_session_factory(build_engine(database_url)))
    tasks = [DevelopmentTask(
        id=str(index), source="trello", title=str(index), description="", url=""
    )
             for index in range(3)]
    batch = repository.create_batch("repo", "trello", tasks)
    execution = repository.create_execution("1")
    repository.record_batch_item_execution(batch.id, 1, execution.id)
    repository.recover_interrupted()
    linked = repository.get_batch(batch.id)
    assert [item.position for item in linked.items] == [0, 1, 2]
    assert [item.execution_id for item in linked.items] == [None, execution.id, None]
    repository.queue_interrupted(execution.id)
    assert repository.get_batch(batch.id).items[1].execution_id == execution.id


def test_repository_persists_execution_error_and_steps_after_alembic_upgrade(
    tmp_path: Path,
) -> None:
    project_root = Path(__file__).resolve().parents[2]
    database = tmp_path / "workflow.db"
    database_url = f"sqlite:///{database.as_posix()}"
    alembic_config = Config(str(project_root / "alembic.ini"))
    alembic_config.set_main_option(
        "script_location", str(project_root / "src" / "prchef" / "migrations")
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
    repository.record_spec_jules_session(execution.id, "spec-1", "https://example.test/spec-1")
    repository.record_review_jules_session(
        execution.id, "review-1", "https://example.test/review-1"
    )
    step_id = repository.start_step(execution.id, WorkflowStage.GENERATE_SPEC)
    repository.finish_step(step_id, StepStatus.FAILED, "provider unavailable")
    repository.record_error(execution.id, "provider unavailable")
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
    assert persisted.spec_jules_session_url == "https://example.test/spec-1"
    assert persisted.review_jules_session_id == "review-1"
    assert persisted.review_jules_session_url == "https://example.test/review-1"
    assert persisted.status is WorkflowStatus.FAILED
    assert persisted.error_message == "provider unavailable"
    assert persisted.finished_at is not None
    assert len(steps) == 1
    assert steps[0].stage is WorkflowStage.GENERATE_SPEC
    assert steps[0].status is StepStatus.FAILED
    assert steps[0].error_message == "provider unavailable"

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
        "script_location", str(project_root / "src" / "prchef" / "migrations")
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
        "script_location", str(project_root / "src" / "prchef" / "migrations")
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
    token = "ghs_" + ("FAKE_NOT_A_SECRET.AaZz09-_" * 22)[:516]
    project_root = Path(__file__).resolve().parents[2]
    database_url = f"sqlite:///{(tmp_path / 'workflow.db').as_posix()}"
    alembic_config = Config(str(project_root / "alembic.ini"))
    alembic_config.set_main_option(
        "script_location", str(project_root / "src" / "prchef" / "migrations")
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
            payload={"command": "pytest", "stderr": f"Authorization: Bearer {token}\n{token}"},
        )
    )

    restarted = WorkflowRepository(build_session_factory(build_engine(database_url)))
    diagnostics = restarted.list_diagnostics(execution.id)

    assert diagnostics[0].category is DiagnosticCategory.VALIDATION
    assert diagnostics[0].payload["command"] == "pytest"
    assert diagnostics[0].payload["stderr"] == "Authorization=[REDACTED] [REDACTED]\n[REDACTED]"


def test_diagnostic_cursor_reads_new_rows_in_id_order_after_restart(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[2]
    database_url = f"sqlite:///{(tmp_path / 'cursor.db').as_posix()}"
    alembic_config = Config(str(project_root / "alembic.ini"))
    alembic_config.set_main_option(
        "script_location", str(project_root / "src" / "prchef" / "migrations")
    )
    alembic_config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(alembic_config, "head")
    repository = WorkflowRepository(build_session_factory(build_engine(database_url)))
    first = repository.create_execution("first")
    other = repository.create_execution("other")

    def record(execution_id: str, number: int) -> ExecutionDiagnostic:
        return repository.record_diagnostic(
            ExecutionDiagnostic(
                execution_id=execution_id,
                category=DiagnosticCategory.WORKFLOW_EVENT,
                payload={"number": number},
            )
        )

    one = record(first.id, 1)
    record(other.id, 99)
    two = record(first.id, 2)
    restarted = WorkflowRepository(build_session_factory(build_engine(database_url)))
    three = restarted.record_diagnostic(
        ExecutionDiagnostic(
            execution_id=first.id,
            category=DiagnosticCategory.WORKFLOW_EVENT,
            payload={"number": 3},
        )
    )
    assert [item.id for item in restarted.list_diagnostics_after(first.id, 0)] == [
        one.id, two.id, three.id
    ]
    assert [item.id for item in restarted.list_diagnostics_after(first.id, one.id or 0)] == [
        two.id, three.id
    ]
    assert restarted.list_diagnostics_after(first.id, three.id or 0) == []


def test_migration_backfills_common_identity_for_existing_trello_execution(
    tmp_path: Path,
) -> None:
    project_root = Path(__file__).resolve().parents[2]
    database_url = f"sqlite:///{(tmp_path / 'legacy.db').as_posix()}"
    alembic_config = Config(str(project_root / "alembic.ini"))
    alembic_config.set_main_option(
        "script_location", str(project_root / "src" / "prchef" / "migrations")
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
        "script_location", str(project_root / "src" / "prchef" / "migrations")
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
        "script_location", str(project_root / "src" / "prchef" / "migrations")
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


def test_fresh_execution_redacts_repository_reference_without_losing_admission(
    tmp_path: Path,
) -> None:
    repository = _migrated_repository(tmp_path, "redacted-repository-ref.db")
    reference = "example?token=ghp_examplecredential"
    redacted = "example?token=[REDACTED]"

    previous = repository.create_execution("12", "github", repository_ref=reference)
    assert repository.get_execution(previous.id).repository_ref == redacted
    assert repository.executions_for_tasks("github", ["12"], reference)["12"].id == previous.id

    with pytest.raises(ValueError, match="PENDING") as error:
        repository.create_execution("12", "github", repository_ref=reference)
    assert "ghp_examplecredential" not in str(error.value)

    repository.update_status(previous.id, WorkflowStatus.FAILED)
    fresh = repository.create_execution("12", "github", repository_ref=reference)
    assert fresh.id != previous.id
    assert repository.get_execution(fresh.id).repository_ref == redacted

    repository.update_project(fresh.id, "/repos/example", "develop", reference)
    assert repository.get_execution(fresh.id).repository_ref == redacted


def test_repository_key_and_path_share_fresh_run_admission(tmp_path: Path) -> None:
    repository = _migrated_repository(tmp_path, "repository-aliases.db")
    project = tmp_path / "project"
    project.mkdir()
    config = tmp_path / "prchef.yml"
    config.write_text(f"repositories:\n  example:\n    path: {project.as_posix()}\n")
    aliases = ProjectConfigLoader(config).repository_identity_aliases("example")
    path = str(project.resolve())

    # A path-only CLI row has no resolved repository until LOAD_CONFIG.
    active = repository.create_execution("12", "github", repository_ref=path)
    assert repository.executions_for_tasks(
        "github", ["12"], "example", repository_aliases=aliases
    )["12"].id == active.id
    with pytest.raises(ValueError, match="PENDING"):
        repository.create_execution(
            "12", "github", repository_ref="example", repository_aliases=aliases
        )

    class Provider:
        source = "github"

        def get_task(self, task_id: str) -> DevelopmentTask:
            return DevelopmentTask(
                task_id, "github", "Current task", "Description", "https://example/12",
                repository="example",
            )

    service = WorkflowService(
        repository, Provider(), ProjectConfigLoader(config), object(),  # type: ignore[arg-type]
    )
    with pytest.raises(ValueError, match="PENDING"):
        service.submit_execution("12", "example")
    with pytest.raises(ValueError, match="PENDING"):
        service.submit_execution("12")

    repository.update_status(active.id, WorkflowStatus.FAILED)
    fresh = repository.create_execution(
        "12", "github", repository_ref="example", repository_aliases=aliases
    )
    assert fresh.id != active.id
    assert repository.executions_for_tasks(
        "github", ["12"], path, repository_aliases=aliases
    )["12"].id == fresh.id
    with pytest.raises(ValueError, match="PENDING"):
        repository.create_execution(
            "12", "github", repository_ref=path, repository_aliases=aliases
        )
    with pytest.raises(ValueError, match="PENDING"):
        service.submit_execution("12", path)


def test_concurrent_fresh_run_admission_serializes_sqlite_read_and_insert(
    tmp_path: Path,
) -> None:
    repository = _migrated_repository(tmp_path, "concurrent-fresh.db")
    prior = repository.create_execution("12", "github", repository_ref="example")
    repository.update_status(prior.id, WorkflowStatus.FAILED)
    first_read = Event()
    second_write_lock_attempt = Event()
    engine = repository._sessions().get_bind()
    assert engine is not None

    def before_cursor_execute(
        _connection: object, _cursor: object, statement: str,
        _parameters: object, _context: object, _executemany: bool,
    ) -> None:
        if statement.startswith("SELECT") and "workflow_executions" in statement:
            if not first_read.is_set():
                first_read.set()
                assert second_write_lock_attempt.wait(5)
        elif statement == "BEGIN IMMEDIATE" and first_read.is_set():
            second_write_lock_attempt.set()

    event.listen(engine, "before_cursor_execute", before_cursor_execute)
    try:
        def submit() -> str:
            try:
                return repository.create_execution(
                    "12", "github", repository_ref="example"
                ).id
            except ValueError as exc:
                return str(exc)

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(submit)
            assert first_read.wait(5)
            second = pool.submit(submit)
            results = [first.result(timeout=10), second.result(timeout=10)]
    finally:
        event.remove(engine, "before_cursor_execute", before_cursor_execute)

    assert sum("PENDING" in result for result in results) == 1
    assert repository.executions_for_tasks("github", ["12"], "example")["12"].id in results


def test_fresh_run_remains_latest_when_clock_moves_backward(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _migrated_repository(tmp_path, "fresh-clock-order.db")
    prior = repository.create_execution("12", "github", repository_ref="example")
    repository.update_status(prior.id, WorkflowStatus.FAILED)

    monkeypatch.setattr(
        workflow_repository_module, "_utcnow", lambda: datetime(2020, 1, 1, tzinfo=UTC)
    )
    fresh = repository.create_execution("12", "github", repository_ref="example")

    assert fresh.created_at > prior.created_at
    assert repository.executions_for_tasks("github", ["12"], "example")["12"].id == fresh.id
    with pytest.raises(ValueError, match="PENDING"):
        repository.create_execution("12", "github", repository_ref="example")
    assert repository.get_execution(prior.id).status == WorkflowStatus.FAILED


def test_postgresql_fresh_run_admission_locks_provider_task_before_status_read() -> None:
    session = Mock()
    session.get_bind.return_value.dialect.name = "postgresql"
    calls = Mock()
    calls.attach_mock(session.connection.return_value.exec_driver_sql, "driver")
    calls.attach_mock(session.execute, "execute")

    _lock_fresh_run_admission(session, "github", "12")

    assert calls.mock_calls[0] == call.driver("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
    assert str(calls.mock_calls[1].args[0]) == "SELECT pg_advisory_xact_lock(:key)"
    first_key = calls.mock_calls[1].args[1]["key"]
    assert -(2**63) <= first_key < 2**63
    _lock_fresh_run_admission(session, "github", "12")
    assert calls.mock_calls[3].args[1]["key"] == first_key
    _lock_fresh_run_admission(session, "trello", "12")
    assert calls.mock_calls[5].args[1]["key"] != first_key


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


@pytest.mark.parametrize(
    "latest_status",
    [WorkflowStatus.PENDING, WorkflowStatus.RUNNING, WorkflowStatus.APPROVED],
)
def test_empty_repository_override_checks_trello_task_repository_before_submission(
    tmp_path: Path, latest_status: WorkflowStatus,
) -> None:
    repository = _migrated_repository(tmp_path, f"empty-override-{latest_status.value}.db")
    prior = repository.create_execution("card-12", "trello", repository_ref="example")
    repository.update_status(prior.id, latest_status)

    class Provider:
        source = "trello"

        def get_task(self, task_id: str) -> DevelopmentTask:
            return DevelopmentTask(
                task_id, "trello", "Task", "Description", "https://example/card-12",
                metadata={"custom_fields": {"Repository": "example"}},
            )

    service = WorkflowService(
        repository, Provider(), ProjectConfigLoader(tmp_path / "missing.yml"), object(),
    )  # type: ignore[arg-type]
    with pytest.raises(ValueError, match=latest_status.value):
        service.submit_execution("card-12", "")

    assert repository.get_execution(prior.id).status == latest_status
    assert [execution.id for execution in repository.list_executions()] == [prior.id]


def test_empty_repository_override_allows_eligible_trello_fresh_run(tmp_path: Path) -> None:
    repository = _migrated_repository(tmp_path, "empty-override-eligible.db")
    prior = repository.create_execution("card-12", "trello", repository_ref="example")
    repository.update_status(prior.id, WorkflowStatus.FAILED)

    class Provider:
        source = "trello"

        def get_task(self, task_id: str) -> DevelopmentTask:
            return DevelopmentTask(
                task_id, "trello", "Task", "Edited description", "https://example/card-12",
                metadata={"custom_fields": {"Repository": "example"}},
            )

    service = WorkflowService(
        repository, Provider(), ProjectConfigLoader(tmp_path / "missing.yml"), object(),
    )  # type: ignore[arg-type]
    fresh = service.run("card-12", "")

    assert fresh.id != prior.id
    assert fresh.repository_ref == "example"
    assert fresh.task_snapshot is not None
    assert fresh.task_snapshot["description"] == "Edited description"
    assert repository.get_execution(prior.id).status == WorkflowStatus.FAILED


def test_repository_rejects_explicit_empty_admission_reference(tmp_path: Path) -> None:
    repository = _migrated_repository(tmp_path, "empty-repository-reference.db")
    with pytest.raises(ValueError, match="non-empty"):
        repository.create_execution("card-12", "trello", repository_ref="")
    assert repository.list_executions() == []


def test_repository_inferred_at_submission_uses_the_admitted_task_snapshot(
    tmp_path: Path,
) -> None:
    repository = _migrated_repository(tmp_path, "admission-snapshot.db")
    other = repository.create_execution("12", "github", repository_ref="other")
    current_repository = ["original"]
    provider_reads: list[str] = []
    ready = Event()

    class Provider:
        source = "github"

        def get_task(self, task_id: str) -> DevelopmentTask:
            provider_reads.append(current_repository[0])
            return DevelopmentTask(
                task_id, "github", "Task", "Description", "https://example/12",
                repository=current_repository[0],
            )

    class FailingConfig:
        def load(self, task: DevelopmentTask, repository_override: str | None):
            assert task.repository == "original"
            raise RuntimeError("stop after loading the admitted task")

    service = WorkflowService(
        repository, Provider(), FailingConfig(), object(),  # type: ignore[arg-type]
    )
    handle = service.submit_execution("12", admission_gate=ready.is_set)
    current_repository[0] = "other"
    ready.set()
    service._dispatcher.reevaluate()
    result = handle.result(timeout=5)

    assert result.status == WorkflowStatus.FAILED
    assert result.repository_ref == "original"
    assert result.task_snapshot is not None
    assert result.task_snapshot["repository"] == "original"
    assert provider_reads == ["original"]
    assert repository.get_execution(other.id).status == WorkflowStatus.PENDING


def test_failed_admission_read_is_recorded_without_refetching_task(tmp_path: Path) -> None:
    repository = _migrated_repository(tmp_path, "admission-read-failed.db")
    provider_reads = 0

    class Provider:
        source = "github"

        def get_task(self, task_id: str) -> DevelopmentTask:
            nonlocal provider_reads
            provider_reads += 1
            if provider_reads == 1:
                raise RuntimeError("provider unavailable")
            return DevelopmentTask(
                task_id, "github", "Task", "Description", "https://example/12",
                repository="other",
            )

    service = WorkflowService(
        repository, Provider(), object(), object(),  # type: ignore[arg-type]
    )
    result = service.run("12")

    assert result.status == WorkflowStatus.FAILED
    assert result.current_stage == WorkflowStage.LOAD_TASK
    assert result.task_snapshot is None
    assert "provider unavailable" in (result.error_message or "")
    assert provider_reads == 1


def test_failed_provider_read_infers_one_repository_from_key_and_path_history(
    tmp_path: Path,
) -> None:
    repository = _migrated_repository(tmp_path, "inferred-repository-aliases.db")
    project = tmp_path / "project"
    project.mkdir()
    config = tmp_path / "prchef.yml"
    config.write_text(f"repositories:\n  example:\n    path: {project.as_posix()}\n")

    by_key = repository.create_execution("12", "github", repository_ref="example")
    repository.update_status(by_key.id, WorkflowStatus.FAILED)
    by_path = repository.create_execution("12", "github", repository_ref=str(project.resolve()))
    provider_reads = 0

    class Provider:
        source = "github"

        def get_task(self, task_id: str) -> DevelopmentTask:
            nonlocal provider_reads
            provider_reads += 1
            raise RuntimeError("provider unavailable")

    service = WorkflowService(
        repository, Provider(), ProjectConfigLoader(config), object(),  # type: ignore[arg-type]
    )
    with pytest.raises(ValueError, match="PENDING"):
        service.submit_execution("12")
    assert repository.get_execution(by_path.id).status == WorkflowStatus.PENDING

    repository.update_status(by_path.id, WorkflowStatus.FAILED)
    result = service.run("12")

    assert result.status == WorkflowStatus.FAILED
    assert result.current_stage == WorkflowStage.LOAD_TASK
    assert "provider unavailable" in (result.error_message or "")
    assert result.id not in {by_key.id, by_path.id}
    assert provider_reads == 2
    assert repository.get_execution(by_key.id).status == WorkflowStatus.FAILED
    assert repository.get_execution(by_path.id).status == WorkflowStatus.FAILED
    aliases = ProjectConfigLoader(config).repository_identity_aliases("example")
    assert repository.executions_for_tasks(
        "github", ["12"], "example", repository_aliases=aliases
    )["12"].id == result.id


def test_fresh_runs_keep_separate_branch_and_worktree_records(tmp_path: Path) -> None:
    repository = _migrated_repository(tmp_path, "fresh-workspaces.db")
    manager = WorkspaceManager(
        object(), tmp_path / "control", WorkspaceSettings(root=tmp_path / "worktrees")
    )  # type: ignore[arg-type]
    first = repository.create_execution("12", "github", repository_ref="example")
    first_branch = WorkflowService._branch_name("prchef", "12", "Fix task", first.id)
    first_path = manager.workspace_path(first.id)
    repository.record_branch(first.id, first_branch)
    repository.record_workspace(first.id, str(first_path))
    repository.update_status(first.id, WorkflowStatus.FAILED)

    second = repository.create_execution("12", "github", repository_ref="example")
    second_branch = WorkflowService._branch_name("prchef", "12", "Fix task", second.id)
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
        "script_location", str(project_root / "src" / "prchef" / "migrations")
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
