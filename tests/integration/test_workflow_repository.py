from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import text

from orchestrator.config.database import build_engine, build_session_factory
from orchestrator.models import (
    DevelopmentTask,
    StepStatus,
    WorkflowStage,
    WorkflowStatus,
)
from orchestrator.repositories.workflow_repository import WorkflowRepository


def test_repository_persists_execution_error_and_steps_after_alembic_upgrade(
    tmp_path: Path,
) -> None:
    project_root = Path(__file__).resolve().parents[2]
    database = tmp_path / "workflow.db"
    database_url = f"sqlite:///{database.as_posix()}"
    alembic_config = Config(str(project_root / "alembic.ini"))
    alembic_config.set_main_option("script_location", str(project_root / "migrations"))
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


def test_migration_backfills_common_identity_for_existing_trello_execution(
    tmp_path: Path,
) -> None:
    project_root = Path(__file__).resolve().parents[2]
    database_url = f"sqlite:///{(tmp_path / 'legacy.db').as_posix()}"
    alembic_config = Config(str(project_root / "alembic.ini"))
    alembic_config.set_main_option("script_location", str(project_root / "migrations"))
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
