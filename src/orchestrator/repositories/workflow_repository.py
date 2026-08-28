from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.models.code_review import CodeReview
from orchestrator.models.development_task import DevelopmentTask
from orchestrator.models.implementation_result import ImplementationResult
from orchestrator.models.pull_request import PullRequest
from orchestrator.models.repository_spec import RepositorySpec
from orchestrator.models.validation_result import ValidationResult
from orchestrator.models.workflow_execution import WorkflowExecution
from orchestrator.models.workflow_status import StepStatus, WorkflowStage, WorkflowStatus
from orchestrator.models.workflow_step import WorkflowStep
from orchestrator.repositories.orm_models import WorkflowExecutionEntity, WorkflowStepEntity


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _dump(value: Any) -> str:
    return json.dumps(asdict(value), ensure_ascii=False, default=str)


def _load(value: str | None) -> dict[str, Any] | None:
    if value is None:
        return None
    parsed = json.loads(value)
    return parsed if isinstance(parsed, dict) else None


class WorkflowRepository:
    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self._sessions = session_factory

    def create_execution(self, trello_card_id: str) -> WorkflowExecution:
        now = _utcnow()
        entity = WorkflowExecutionEntity(
            id=str(uuid.uuid4()),
            trello_card_id=trello_card_id,
            trello_card_url="",
            repository="",
            base_branch="",
            current_stage=WorkflowStage.LOAD_TASK.value,
            status=WorkflowStatus.PENDING.value,
            created_at=now,
            updated_at=now,
        )
        with self._sessions() as session, session.begin():
            session.add(entity)
        return self._to_domain(entity)

    def update_task(self, execution_id: str, task: DevelopmentTask) -> None:
        def mutate(entity: WorkflowExecutionEntity) -> None:
            entity.trello_card_url = task.url
            entity.task_snapshot_json = _dump(task)

        self._mutate(execution_id, mutate)

    def update_project(self, execution_id: str, repository: str, base_branch: str) -> None:
        def mutate(entity: WorkflowExecutionEntity) -> None:
            entity.repository = repository
            entity.base_branch = base_branch

        self._mutate(execution_id, mutate)

    def update_stage(self, execution_id: str, stage: WorkflowStage) -> None:
        self._mutate(execution_id, lambda entity: setattr(entity, "current_stage", stage.value))

    def update_status(self, execution_id: str, status: WorkflowStatus) -> None:
        self._mutate(execution_id, lambda entity: setattr(entity, "status", status.value))

    def record_spec(self, execution_id: str, spec: RepositorySpec) -> None:
        self._mutate(
            execution_id, lambda entity: setattr(entity, "spec_snapshot_json", _dump(spec))
        )

    def record_spec_jules_session(
        self, execution_id: str, session_id: str, session_url: str | None
    ) -> None:
        def mutate(entity: WorkflowExecutionEntity) -> None:
            entity.spec_jules_session_id = session_id
            entity.spec_jules_session_url = session_url

        self._mutate(execution_id, mutate)

    def record_branch(self, execution_id: str, branch: str) -> None:
        self._mutate(execution_id, lambda entity: setattr(entity, "working_branch", branch))

    def record_implementation(self, execution_id: str, result: ImplementationResult) -> None:
        self._mutate(
            execution_id,
            lambda entity: setattr(entity, "implementation_snapshot_json", _dump(result)),
        )

    def record_validation(self, execution_id: str, result: ValidationResult) -> None:
        self._mutate(
            execution_id, lambda entity: setattr(entity, "validation_snapshot_json", _dump(result))
        )

    def record_commit(self, execution_id: str, commit_sha: str) -> None:
        self._mutate(execution_id, lambda entity: setattr(entity, "commit_sha", commit_sha))

    def record_pull_request(self, execution_id: str, pull_request: PullRequest) -> None:
        def mutate(entity: WorkflowExecutionEntity) -> None:
            entity.pr_url = pull_request.url
            entity.pull_request_snapshot_json = _dump(pull_request)

        self._mutate(execution_id, mutate)

    def record_review(self, execution_id: str, review: CodeReview) -> None:
        self._mutate(
            execution_id, lambda entity: setattr(entity, "review_snapshot_json", _dump(review))
        )

    def record_review_jules_session(
        self, execution_id: str, session_id: str, session_url: str | None
    ) -> None:
        def mutate(entity: WorkflowExecutionEntity) -> None:
            entity.review_jules_session_id = session_id
            entity.review_jules_session_url = session_url

        self._mutate(execution_id, mutate)

    def record_error(self, execution_id: str, message: str) -> None:
        self._mutate(execution_id, lambda entity: setattr(entity, "error_message", message))

    def finish(self, execution_id: str, status: WorkflowStatus) -> None:
        def mutate(entity: WorkflowExecutionEntity) -> None:
            entity.status = status.value
            if status in {WorkflowStatus.APPROVED, WorkflowStatus.CHANGES_REQUESTED}:
                entity.current_stage = WorkflowStage.COMPLETED.value
            entity.finished_at = _utcnow()

        self._mutate(execution_id, mutate)

    def start_step(self, execution_id: str, stage: WorkflowStage) -> int:
        entity = WorkflowStepEntity(
            workflow_execution_id=execution_id,
            stage=stage.value,
            status=StepStatus.RUNNING.value,
            started_at=_utcnow(),
        )
        with self._sessions() as session, session.begin():
            session.add(entity)
        if entity.id is None:
            raise RuntimeError("Workflow step id was not generated")
        return entity.id

    def finish_step(
        self,
        step_id: int,
        status: StepStatus,
        error_message: str | None = None,
    ) -> None:
        with self._sessions() as session, session.begin():
            entity = session.get(WorkflowStepEntity, step_id)
            if entity is None:
                raise KeyError(f"Workflow step not found: {step_id}")
            entity.status = status.value
            entity.error_message = error_message
            entity.finished_at = _utcnow()

    def get_execution(self, execution_id: str) -> WorkflowExecution:
        with self._sessions() as session:
            entity = session.get(WorkflowExecutionEntity, execution_id)
            if entity is None:
                raise KeyError(f"Workflow execution not found: {execution_id}")
            return self._to_domain(entity)

    def list_steps(self, execution_id: str) -> list[WorkflowStep]:
        with self._sessions() as session:
            entities = session.scalars(
                select(WorkflowStepEntity)
                .where(WorkflowStepEntity.workflow_execution_id == execution_id)
                .order_by(WorkflowStepEntity.id)
            ).all()
            return [
                WorkflowStep(
                    id=entity.id,
                    workflow_execution_id=entity.workflow_execution_id,
                    stage=WorkflowStage(entity.stage),
                    status=StepStatus(entity.status),
                    started_at=entity.started_at,
                    finished_at=entity.finished_at,
                    error_message=entity.error_message,
                )
                for entity in entities
            ]

    def _mutate(
        self,
        execution_id: str,
        operation: Callable[[WorkflowExecutionEntity], None],
    ) -> None:
        with self._sessions() as session, session.begin():
            entity = session.get(WorkflowExecutionEntity, execution_id)
            if entity is None:
                raise KeyError(f"Workflow execution not found: {execution_id}")
            operation(entity)
            entity.updated_at = _utcnow()

    @staticmethod
    def _to_domain(entity: WorkflowExecutionEntity) -> WorkflowExecution:
        return WorkflowExecution(
            id=entity.id,
            trello_card_id=entity.trello_card_id,
            trello_card_url=entity.trello_card_url,
            repository=entity.repository,
            base_branch=entity.base_branch,
            working_branch=entity.working_branch,
            commit_sha=entity.commit_sha,
            pr_url=entity.pr_url,
            current_stage=WorkflowStage(entity.current_stage),
            status=WorkflowStatus(entity.status),
            error_message=entity.error_message,
            created_at=entity.created_at,
            updated_at=entity.updated_at,
            finished_at=entity.finished_at,
            task_snapshot=_load(entity.task_snapshot_json),
            spec_snapshot=_load(entity.spec_snapshot_json),
            implementation_snapshot=_load(entity.implementation_snapshot_json),
            validation_snapshot=_load(entity.validation_snapshot_json),
            pull_request_snapshot=_load(entity.pull_request_snapshot_json),
            spec_jules_session_id=entity.spec_jules_session_id,
            spec_jules_session_url=entity.spec_jules_session_url,
            review_jules_session_id=entity.review_jules_session_id,
            review_jules_session_url=entity.review_jules_session_url,
            review_snapshot=_load(entity.review_snapshot_json),
        )
