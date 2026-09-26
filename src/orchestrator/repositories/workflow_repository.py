from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Sequence
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any, cast

from sqlalchemy import case, or_, select, text, update
from sqlalchemy.engine import CursorResult
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
from orchestrator.models.workflow_batch import WorkflowBatch, WorkflowBatchItem
from orchestrator.models.workflow_execution import WorkflowExecution
from orchestrator.models.workflow_status import (
    ACTIVE_WORKFLOW_STATUSES,
    PolishOutcome,
    StepStatus,
    WorkflowStage,
    WorkflowStatus,
    can_start_fresh_run,
)
from orchestrator.models.workflow_step import WorkflowStep
from orchestrator.repositories.orm_models import (
    ExecutionDiagnosticEntity,
    HumanInteractionEntity,
    WorkflowBatchEntity,
    WorkflowBatchItemEntity,
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


# Queued, provider-blocked, and genuinely running work are all "active": they
# have not reached a terminal status, so they remain cancellable. The column is
# string-backed, so compare persisted values rather than enum members.
_ACTIVE_STATUS_VALUES = frozenset(status.value for status in ACTIVE_WORKFLOW_STATUSES)


def _lock_fresh_run_admission(session: Session, task_source: str, task_id: str) -> None:
    """Serialize the latest-status read and insert for a provider task.

    Locking by source and ID (across repositories) also covers submissions
    using different aliases for the same configured repository.
    """

    bind = session.get_bind()
    if bind.dialect.name == "sqlite":
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")
    elif bind.dialect.name == "postgresql":
        # READ COMMITTED gives the status query a new snapshot after a
        # competing transaction releases its advisory lock and commits.
        session.connection().exec_driver_sql("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
        lock_key = int.from_bytes(
            sha256(f"{task_source}\0{task_id}".encode()).digest()[:8], "big", signed=True
        )
        session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key})
    else:
        raise RuntimeError(
            f"Fresh-run admission requires a database lock for {bind.dialect.name}"
        )


class WorkflowRepository:
    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self._sessions = session_factory

    def create_execution(
        self, task_id: str, task_source: str = "trello", *,
        repository_ref: str | None = None,
        repository_aliases: Sequence[str] = (),
    ) -> WorkflowExecution:
        with self._sessions() as session, session.begin():
            latest = None
            if repository_ref:
                _lock_fresh_run_admission(session, task_source, task_id)
                references = tuple(dict.fromkeys((repository_ref, *repository_aliases)))
                latest = session.scalars(
                    select(WorkflowExecutionEntity)
                    .where(
                        WorkflowExecutionEntity.task_source == task_source,
                        WorkflowExecutionEntity.task_id == task_id,
                        or_(
                            WorkflowExecutionEntity.repository_ref.in_(references),
                            WorkflowExecutionEntity.repository.in_(references),
                        ),
                    )
                    .order_by(
                        WorkflowExecutionEntity.created_at.desc(),
                        WorkflowExecutionEntity.id.desc(),
                    )
                    .limit(1)
                ).first()
                if latest is not None and not can_start_fresh_run(WorkflowStatus(latest.status)):
                    raise ValueError(
                        f"Task {task_id} already has a {latest.status} execution "
                        f"in repository {repository_ref}"
                    )
            # Assign the ordering timestamp inside the admission transaction, after
            # the locked latest-run read. A queued caller must not insert a new
            # PENDING row with a timestamp older than a run it just observed.
            now = _utcnow()
            if latest is not None:
                previous = latest.created_at
                if previous.tzinfo is None:
                    previous = previous.replace(tzinfo=UTC)
                now = max(now, previous + timedelta(microseconds=1))
            legacy_trello_id = task_id if task_source == "trello" else ""
            entity = WorkflowExecutionEntity(
                id=str(uuid.uuid4()),
                task_source=task_source,
                task_id=task_id,
                task_url="",
                trello_card_id=legacy_trello_id,
                trello_card_url="",
                repository="",
                repository_ref=repository_ref or "",
                base_branch="",
                current_stage=WorkflowStage.LOAD_TASK.value,
                status=WorkflowStatus.PENDING.value,
                created_at=now,
                updated_at=now,
            )
            session.add(entity)
            # Build the detached domain object while the session is still
            # available.  The execution's interactions relationship is lazy
            # loaded and a newly committed entity is expired on session exit.
            session.flush()
            return self._to_domain(entity)

    def create_batch(
        self,
        repository: str,
        task_source: str,
        tasks: list[DevelopmentTask],
    ) -> WorkflowBatch:
        """Persist a batch and its ordered task snapshots in one transaction."""

        entity = WorkflowBatchEntity(
            id=str(uuid.uuid4()),
            repository=redact_text(repository),
            task_source=redact_text(task_source),
            created_at=_utcnow(),
            items=[
                WorkflowBatchItemEntity(
                    position=position,
                    task_id=redact_text(task.id),
                    task_title=redact_text(task.title or "(untitled)"),
                )
                for position, task in enumerate(tasks)
            ],
        )
        with self._sessions() as session, session.begin():
            session.add(entity)
            session.flush()
            return self._batch_to_domain(entity)

    def record_batch_item_execution(
        self, batch_id: str, position: int, execution_id: str
    ) -> None:
        with self._sessions() as session, session.begin():
            entity = session.scalars(
                select(WorkflowBatchItemEntity).where(
                    WorkflowBatchItemEntity.batch_id == batch_id,
                    WorkflowBatchItemEntity.position == position,
                )
            ).one_or_none()
            if entity is None:
                raise KeyError(f"Workflow batch item not found: {batch_id}[{position}]")
            entity.execution_id = execution_id

    def get_batch(self, batch_id: str) -> WorkflowBatch:
        with self._sessions() as session:
            entity = session.get(WorkflowBatchEntity, batch_id)
            if entity is None:
                raise KeyError(f"Workflow batch not found: {batch_id}")
            return self._batch_to_domain(entity)

    def list_batches(self) -> list[WorkflowBatch]:
        with self._sessions() as session:
            entities = session.scalars(
                select(WorkflowBatchEntity).order_by(WorkflowBatchEntity.created_at)
            ).unique().all()
            return [self._batch_to_domain(entity) for entity in entities]

    def delete_batch(self, batch_id: str) -> None:
        """Delete batch metadata; linked executions deliberately remain intact."""

        with self._sessions() as session, session.begin():
            entity = session.get(WorkflowBatchEntity, batch_id)
            if entity is None:
                raise KeyError(f"Workflow batch not found: {batch_id}")
            session.delete(entity)

    def executions_for_tasks(
        self,
        task_source: str,
        task_ids: list[str],
        repository: str,
        repository_aliases: Sequence[str] = (),
    ) -> dict[str, WorkflowExecution]:
        """Return the newest execution for each repository-scoped task identity."""

        if not task_ids:
            return {}
        references = tuple(dict.fromkeys((repository, *repository_aliases)))
        with self._sessions() as session:
            entities = session.scalars(
                select(WorkflowExecutionEntity)
                .where(
                    WorkflowExecutionEntity.task_source == task_source,
                    WorkflowExecutionEntity.task_id.in_(task_ids),
                    or_(
                        WorkflowExecutionEntity.repository_ref.in_(references),
                        WorkflowExecutionEntity.repository.in_(references),
                    ),
                )
                .order_by(
                    WorkflowExecutionEntity.created_at.desc(),
                    WorkflowExecutionEntity.id.desc(),
                )
            ).unique().all()
            latest: dict[str, WorkflowExecution] = {}
            for entity in entities:
                latest.setdefault(entity.task_id, self._to_domain(entity))
            return latest

    def inferred_repository_for_task(
        self,
        task_source: str,
        task_id: str,
        identity_aliases: Callable[[str], Sequence[str]] | None = None,
    ) -> str | None:
        """Recover an unambiguous prior repository if task loading is unavailable."""

        with self._sessions() as session:
            rows = session.execute(
                select(
                    WorkflowExecutionEntity.repository_ref,
                    WorkflowExecutionEntity.repository,
                ).where(
                    WorkflowExecutionEntity.task_source == task_source,
                    WorkflowExecutionEntity.task_id == task_id,
                )
            ).all()
        references = sorted({
            reference or repository for reference, repository in rows
            if reference or repository
        })
        identities = [
            set(identity_aliases(reference)) | {reference} if identity_aliases else {reference}
            for reference in references
        ]
        if identities and any(
            not identities[0].intersection(identity) for identity in identities[1:]
        ):
            raise ValueError(
                f"Cannot resolve repository for {task_source} task {task_id} while "
                "the task provider is unavailable; specify the repository"
            )
        return references[0] if references else None

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

    def record_submission_repository(self, execution_id: str, repository_ref: str) -> None:
        """Scope duplicate detection before task/config loading can fail."""

        self._mutate(
            execution_id,
            lambda entity: setattr(entity, "repository_ref", redact_text(repository_ref)),
        )

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
        """Cancel a human-blocked execution or request cooperative cancellation.

        Queued (``PENDING``) and provider-blocked (``WAITING``) work is
        cancellable too: it has not reached a terminal status and its user is
        entitled to stop it before it consumes any shared capacity. The request
        deliberately does not discard workspace or artifact fields. A running
        service observes this marker and performs the terminal transition at a
        safe stage or provider boundary.
        """
        with self._sessions() as session, session.begin():
            now = _utcnow()
            # A conditional database write arbitrates with an answer being
            # queued by another session or process. A prior ORM read here
            # would allow a stale NEEDS_HUMAN value to overwrite that queue.
            cancelled = cast(
                CursorResult[Any],
                session.execute(
                    update(WorkflowExecutionEntity)
                    .where(
                        WorkflowExecutionEntity.id == execution_id,
                        WorkflowExecutionEntity.status == WorkflowStatus.NEEDS_HUMAN.value,
                    )
                    .values(
                        cancellation_requested_at=now,
                        status=WorkflowStatus.CANCELLED.value,
                        finished_at=now,
                        updated_at=now,
                    )
                ),
            )
            if cancelled.rowcount:
                return True
            requested = cast(
                CursorResult[Any],
                session.execute(
                    update(WorkflowExecutionEntity)
                    .where(
                        WorkflowExecutionEntity.id == execution_id,
                        WorkflowExecutionEntity.status.in_(_ACTIVE_STATUS_VALUES),
                        WorkflowExecutionEntity.cancellation_requested_at.is_(None),
                    )
                    .values(cancellation_requested_at=now, updated_at=now)
                ),
            )
            if requested.rowcount:
                return True
            entity = session.get(WorkflowExecutionEntity, execution_id)
            if entity is None:
                raise KeyError(f"Workflow execution not found: {execution_id}")
            return entity.status in _ACTIVE_STATUS_VALUES

    def cancellation_requested(self, execution_id: str) -> bool:
        with self._sessions() as session:
            entity = session.get(WorkflowExecutionEntity, execution_id)
            if entity is None:
                raise KeyError(f"Workflow execution not found: {execution_id}")
            return (
                entity.status in _ACTIVE_STATUS_VALUES
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
            now = _utcnow()
            queued = cast(
                CursorResult[Any],
                session.execute(
                    update(WorkflowExecutionEntity)
                    .where(
                        WorkflowExecutionEntity.id == execution_id,
                        WorkflowExecutionEntity.status == WorkflowStatus.NEEDS_HUMAN.value,
                        WorkflowExecutionEntity.cancellation_requested_at.is_(None),
                    )
                    .values(
                        status=WorkflowStatus.PENDING.value,
                        error_message=None,
                        finished_at=None,
                        updated_at=now,
                    )
                ),
            )
            if not queued.rowcount:
                raise ValueError(f"Execution {execution_id} is not awaiting human input")
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
            entity.responded_at = now
            return self._interaction_to_domain(entity)

    def resume_execution(self, execution_id: str) -> None:
        def mutate(entity: WorkflowExecutionEntity) -> None:
            entity.status = WorkflowStatus.RUNNING.value
            entity.error_message = None
            entity.finished_at = None

        self._mutate(execution_id, mutate)

    def queue_execution(self, execution_id: str) -> None:
        """Return a resumed or continued execution to the queued state.

        Resumes and continuations compete for the same bounded capacity as
        fresh runs, so they wait in ``PENDING`` rather than claiming ``RUNNING``
        before a worker actually starts them.
        """

        def mutate(entity: WorkflowExecutionEntity) -> None:
            if (
                entity.status == WorkflowStatus.CANCELLED.value
                or entity.cancellation_requested_at is not None
            ):
                raise ValueError(f"Execution {execution_id} has been cancelled")
            entity.status = WorkflowStatus.PENDING.value
            entity.error_message = None
            entity.finished_at = None
            entity.cancellation_requested_at = None

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
        # Decide the terminal status in the same write that finishes the row.
        # A cancellation can arrive after the service's last safe-boundary
        # check; an ORM read followed by an unconditional write would lose it.
        cancelled = WorkflowExecutionEntity.cancellation_requested_at.is_not(None)
        now = _utcnow()
        values: dict[str, Any] = {
            "status": case((cancelled, WorkflowStatus.CANCELLED.value), else_=status.value),
            "finished_at": now,
            "updated_at": now,
        }
        if status in {WorkflowStatus.APPROVED, WorkflowStatus.CHANGES_REQUESTED}:
            values["current_stage"] = case(
                (cancelled, WorkflowExecutionEntity.current_stage),
                else_=WorkflowStage.COMPLETED.value,
            )
        with self._sessions() as session, session.begin():
            result = cast(
                CursorResult[Any],
                session.execute(
                    update(WorkflowExecutionEntity)
                    .where(WorkflowExecutionEntity.id == execution_id)
                    .values(**values)
                ),
            )
            if not result.rowcount:
                raise KeyError(f"Workflow execution not found: {execution_id}")

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

    def execution_statuses(self, execution_ids: list[str]) -> dict[str, WorkflowStatus]:
        """Read multiple execution statuses in one repository round trip."""

        if not execution_ids:
            return {}
        with self._sessions() as session:
            rows = session.execute(
                select(WorkflowExecutionEntity.id, WorkflowExecutionEntity.status).where(
                    WorkflowExecutionEntity.id.in_(execution_ids)
                )
            ).all()
            return {execution_id: WorkflowStatus(status) for execution_id, status in rows}

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

    @staticmethod
    def _batch_to_domain(entity: WorkflowBatchEntity) -> WorkflowBatch:
        return WorkflowBatch(
            id=entity.id,
            repository=entity.repository,
            task_source=entity.task_source,
            created_at=entity.created_at,
            items=tuple(
                WorkflowBatchItem(
                    position=item.position,
                    task_id=item.task_id,
                    task_title=item.task_title,
                    execution_id=item.execution_id,
                )
                for item in entity.items
            ),
        )
