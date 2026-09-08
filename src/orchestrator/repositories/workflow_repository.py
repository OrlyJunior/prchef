from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.models.agent import AgentRole
from orchestrator.models.code_review import CodeReview
from orchestrator.models.development_task import DevelopmentTask
from orchestrator.models.execution_diagnostic import DiagnosticCategory, ExecutionDiagnostic
from orchestrator.models.human_interaction import HumanInteraction
from orchestrator.models.implementation_result import ImplementationResult
from orchestrator.models.pull_request import PullRequest
from orchestrator.models.repository_spec import RepositorySpec
from orchestrator.models.validation_result import ValidationResult
from orchestrator.models.workflow_execution import WorkflowExecution
from orchestrator.models.workflow_status import (
    PolishOutcome,
    StepStatus,
    WorkflowStage,
    WorkflowStatus,
)
from orchestrator.models.workflow_step import WorkflowStep
from orchestrator.repositories.orm_models import (
    ExecutionDiagnosticEntity,
    HumanInteractionEntity,
    WorkflowExecutionEntity,
    WorkflowStepEntity,
)
from orchestrator.runtime_logging import redact_payload, redact_text


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _dump(value: Any) -> str:
    return json.dumps(redact_payload(asdict(value)), ensure_ascii=False, default=str)


def _load(value: str | None) -> dict[str, Any] | None:
    if value is None:
        return None
    parsed = json.loads(value)
    return parsed if isinstance(parsed, dict) else None


class WorkflowRepository:
    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self._sessions = session_factory

    def create_execution(self, task_id: str, task_source: str = "trello") -> WorkflowExecution:
        now = _utcnow()
        legacy_trello_id = task_id if task_source == "trello" else ""
        entity = WorkflowExecutionEntity(
            id=str(uuid.uuid4()),
            task_source=task_source,
            task_id=task_id,
            task_url="",
            trello_card_id=legacy_trello_id,
            trello_card_url="",
            repository="",
            repository_ref="",
            base_branch="",
            current_stage=WorkflowStage.LOAD_TASK.value,
            status=WorkflowStatus.PENDING.value,
            created_at=now,
            updated_at=now,
        )
        with self._sessions() as session, session.begin():
            session.add(entity)
            # Build the detached domain object while the session is still
            # available.  The execution's interactions relationship is lazy
            # loaded and a newly committed entity is expired on session exit.
            session.flush()
            return self._to_domain(entity)

    def update_task(self, execution_id: str, task: DevelopmentTask) -> None:
        def mutate(entity: WorkflowExecutionEntity) -> None:
            entity.task_source = task.source
            entity.task_id = task.id
            entity.task_url = task.url
            if task.source == "trello":
                entity.trello_card_id = task.id
                entity.trello_card_url = task.url
            entity.task_snapshot_json = _dump(task)

        self._mutate(execution_id, mutate)

    def update_project(
        self,
        execution_id: str,
        repository: str,
        base_branch: str,
        repository_ref: str = "",
    ) -> None:
        def mutate(entity: WorkflowExecutionEntity) -> None:
            entity.repository = repository
            entity.base_branch = base_branch
            entity.repository_ref = repository_ref

        self._mutate(execution_id, mutate)

    def record_workspace(self, execution_id: str, workspace_path: str) -> None:
        self._mutate(
            execution_id,
            lambda entity: setattr(entity, "workspace_path", workspace_path),
        )

    def update_stage(self, execution_id: str, stage: WorkflowStage) -> None:
        self._mutate(execution_id, lambda entity: setattr(entity, "current_stage", stage.value))

    def update_status(self, execution_id: str, status: WorkflowStatus) -> None:
        self._mutate(execution_id, lambda entity: setattr(entity, "status", status.value))

    def request_cancellation(self, execution_id: str) -> bool:
        """Durably request cooperative cancellation of a running execution.

        The request deliberately does not discard workspace or artifact fields.
        A running service observes this marker and performs the terminal transition
        at a safe stage boundary.
        """
        with self._sessions() as session, session.begin():
            entity = session.get(WorkflowExecutionEntity, execution_id)
            if entity is None:
                raise KeyError(f"Workflow execution not found: {execution_id}")
            if entity.status != WorkflowStatus.RUNNING.value:
                return False
            if entity.cancellation_requested_at is None:
                entity.cancellation_requested_at = _utcnow()
                entity.updated_at = _utcnow()
            return True

    def cancellation_requested(self, execution_id: str) -> bool:
        with self._sessions() as session:
            entity = session.get(WorkflowExecutionEntity, execution_id)
            if entity is None:
                raise KeyError(f"Workflow execution not found: {execution_id}")
            return (
                entity.status == WorkflowStatus.RUNNING.value
                and entity.cancellation_requested_at is not None
            )

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

    def record_polish_outcome(self, execution_id: str, outcome: PolishOutcome) -> None:
        self._mutate(execution_id, lambda entity: setattr(entity, "polish_outcome", outcome.value))

    def record_polish_skip_comment_attempt(self, execution_id: str) -> None:
        self._mutate(
            execution_id,
            lambda entity: setattr(entity, "polish_skip_comment_attempted", True),
        )

    def record_review_jules_session(
        self, execution_id: str, session_id: str, session_url: str | None
    ) -> None:
        def mutate(entity: WorkflowExecutionEntity) -> None:
            entity.review_jules_session_id = session_id
            entity.review_jules_session_url = session_url

        self._mutate(execution_id, mutate)

    def record_error(self, execution_id: str, message: str) -> None:
        self._mutate(
            execution_id, lambda entity: setattr(entity, "error_message", redact_text(message))
        )

    def record_diagnostic(self, diagnostic: ExecutionDiagnostic) -> ExecutionDiagnostic:
        payload = redact_payload(diagnostic.payload)
        if not isinstance(payload, dict):
            payload = {"value": payload}
        entity = ExecutionDiagnosticEntity(
            workflow_execution_id=diagnostic.execution_id,
            category=diagnostic.category.value,
            stage=diagnostic.stage.value if diagnostic.stage else None,
            role=diagnostic.role.value if diagnostic.role else None,
            payload_json=json.dumps(payload, ensure_ascii=False, default=str),
            occurred_at=diagnostic.occurred_at,
        )
        with self._sessions() as session, session.begin():
            session.add(entity)
            session.flush()
            return self._diagnostic_to_domain(entity)

    def list_diagnostics(self, execution_id: str) -> list[ExecutionDiagnostic]:
        with self._sessions() as session:
            entities = session.scalars(
                select(ExecutionDiagnosticEntity)
                .where(ExecutionDiagnosticEntity.workflow_execution_id == execution_id)
                .order_by(ExecutionDiagnosticEntity.id)
            ).all()
            return [self._diagnostic_to_domain(entity) for entity in entities]

    def request_human_input(
        self, execution_id: str, stage: WorkflowStage, prompt: str
    ) -> HumanInteraction:
        entity = HumanInteractionEntity(
            workflow_execution_id=execution_id,
            requesting_stage=stage.value,
            prompt=redact_text(prompt),
            requested_at=_utcnow(),
        )
        with self._sessions() as session, session.begin():
            session.add(entity)
        return self._interaction_to_domain(entity)

    def respond_to_human_input(self, execution_id: str, response: str) -> HumanInteraction:
        response = response.strip()
        if not response:
            raise ValueError("Human response must not be empty")
        with self._sessions() as session, session.begin():
            entity = session.scalars(
                select(HumanInteractionEntity)
                .where(
                    HumanInteractionEntity.workflow_execution_id == execution_id,
                    HumanInteractionEntity.response.is_(None),
                )
                .order_by(HumanInteractionEntity.id.desc())
            ).first()
            if entity is None:
                raise ValueError(f"Execution {execution_id} is not awaiting human input")
            entity.response = redact_text(response)
            entity.responded_at = _utcnow()
            execution = session.get(WorkflowExecutionEntity, execution_id)
            if execution is None:
                raise KeyError(f"Workflow execution not found: {execution_id}")
            execution.updated_at = _utcnow()
            return self._interaction_to_domain(entity)

    def resume_execution(self, execution_id: str) -> None:
        def mutate(entity: WorkflowExecutionEntity) -> None:
            entity.status = WorkflowStatus.RUNNING.value
            entity.error_message = None
            entity.finished_at = None

        self._mutate(execution_id, mutate)

    def list_human_interactions(self, execution_id: str) -> list[HumanInteraction]:
        with self._sessions() as session:
            entities = session.scalars(
                select(HumanInteractionEntity)
                .where(HumanInteractionEntity.workflow_execution_id == execution_id)
                .order_by(HumanInteractionEntity.id)
            ).all()
            return [self._interaction_to_domain(entity) for entity in entities]

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

    def list_executions(self) -> list[WorkflowExecution]:
        """Return persisted executions in creation order for operational inspection."""
        with self._sessions() as session:
            entities = session.scalars(
                select(WorkflowExecutionEntity).order_by(WorkflowExecutionEntity.created_at)
            ).all()
            return [self._to_domain(entity) for entity in entities]

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
            task_source=entity.task_source,
            task_id=entity.task_id,
            task_url=entity.task_url,
            trello_card_id=entity.trello_card_id,
            trello_card_url=entity.trello_card_url,
            repository=entity.repository,
            repository_ref=entity.repository_ref,
            base_branch=entity.base_branch,
            workspace_path=entity.workspace_path,
            working_branch=entity.working_branch,
            commit_sha=entity.commit_sha,
            pr_url=entity.pr_url,
            current_stage=WorkflowStage(entity.current_stage),
            status=WorkflowStatus(entity.status),
            error_message=entity.error_message,
            created_at=entity.created_at,
            updated_at=entity.updated_at,
            finished_at=entity.finished_at,
            cancellation_requested_at=entity.cancellation_requested_at,
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
            polish_outcome=(
                PolishOutcome(entity.polish_outcome) if entity.polish_outcome is not None else None
            ),
            polish_skip_comment_attempted=entity.polish_skip_comment_attempted,
            human_interactions=[
                WorkflowRepository._interaction_to_domain(interaction)
                for interaction in entity.human_interactions
            ],
        )

    @staticmethod
    def _interaction_to_domain(entity: HumanInteractionEntity) -> HumanInteraction:
        return HumanInteraction(
            id=entity.id,
            workflow_execution_id=entity.workflow_execution_id,
            requesting_stage=WorkflowStage(entity.requesting_stage),
            prompt=entity.prompt,
            response=entity.response,
            requested_at=entity.requested_at,
            responded_at=entity.responded_at,
        )

    @staticmethod
    def _diagnostic_to_domain(entity: ExecutionDiagnosticEntity) -> ExecutionDiagnostic:
        payload = json.loads(entity.payload_json)
        return ExecutionDiagnostic(
            id=entity.id,
            execution_id=entity.workflow_execution_id,
            category=DiagnosticCategory(entity.category),
            stage=WorkflowStage(entity.stage) if entity.stage else None,
            role=AgentRole(entity.role) if entity.role else None,
            payload=payload if isinstance(payload, dict) else {"value": payload},
            occurred_at=entity.occurred_at,
        )
