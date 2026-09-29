from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from orchestrator.config.database import Base


class WorkflowExecutionEntity(Base):
    __tablename__ = "workflow_executions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_source: Mapped[str] = mapped_column(String(32), nullable=False, default="trello")
    task_id: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    task_url: Mapped[str] = mapped_column(Text, nullable=False, default="")
    trello_card_id: Mapped[str] = mapped_column(String(128), nullable=False)
    trello_card_url: Mapped[str] = mapped_column(Text, nullable=False, default="")
    repository: Mapped[str] = mapped_column(Text, nullable=False, default="")
    repository_ref: Mapped[str] = mapped_column(Text, nullable=False, default="")
    base_branch: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    base_commit: Mapped[str | None] = mapped_column(String(64))
    workspace_path: Mapped[str | None] = mapped_column(Text)
    working_branch: Mapped[str | None] = mapped_column(String(255))
    commit_sha: Mapped[str | None] = mapped_column(String(64))
    pr_url: Mapped[str | None] = mapped_column(Text)
    current_stage: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    interrupted_from_status: Mapped[str | None] = mapped_column(String(32))
    resume_stage: Mapped[str | None] = mapped_column(String(32))
    resume_mode: Mapped[str | None] = mapped_column(String(32))
    review_retry_attempt: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    validation_retry_attempt: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    error_message: Mapped[str | None] = mapped_column(Text)
    task_snapshot_json: Mapped[str | None] = mapped_column(Text)
    spec_snapshot_json: Mapped[str | None] = mapped_column(Text)
    implementation_snapshot_json: Mapped[str | None] = mapped_column(Text)
    validation_snapshot_json: Mapped[str | None] = mapped_column(Text)
    pull_request_snapshot_json: Mapped[str | None] = mapped_column(Text)
    spec_jules_session_id: Mapped[str | None] = mapped_column(String(255))
    spec_jules_session_url: Mapped[str | None] = mapped_column(Text)
    review_jules_session_id: Mapped[str | None] = mapped_column(String(255))
    review_jules_session_url: Mapped[str | None] = mapped_column(Text)
    review_snapshot_json: Mapped[str | None] = mapped_column(Text)
    review_step_id: Mapped[int | None] = mapped_column(Integer)
    review_publication_pending: Mapped[bool] = mapped_column(
        nullable=False, default=False, server_default="0"
    )
    polish_outcome: Mapped[str | None] = mapped_column(String(32))
    polish_skip_comment_attempted: Mapped[bool] = mapped_column(
        nullable=False, default=False, server_default="0"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancellation_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    steps: Mapped[list["WorkflowStepEntity"]] = relationship(
        back_populates="execution", cascade="all, delete-orphan"
    )
    human_interactions: Mapped[list["HumanInteractionEntity"]] = relationship(
        back_populates="execution",
        cascade="all, delete-orphan",
        order_by="HumanInteractionEntity.id",
    )
    diagnostics: Mapped[list["ExecutionDiagnosticEntity"]] = relationship(
        back_populates="execution",
        cascade="all, delete-orphan",
        order_by="ExecutionDiagnosticEntity.id",
    )

    __table_args__ = (
        Index("ix_workflow_executions_task_source_id", "task_source", "task_id"),
        Index("ix_workflow_executions_trello_card_id", "trello_card_id"),
        Index("ix_workflow_executions_status", "status"),
        Index("ix_workflow_executions_current_stage", "current_stage"),
    )


class WorkflowBatchEntity(Base):
    __tablename__ = "workflow_batches"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    repository: Mapped[str] = mapped_column(Text, nullable=False)
    task_source: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    items: Mapped[list["WorkflowBatchItemEntity"]] = relationship(
        back_populates="batch",
        cascade="all, delete-orphan",
        order_by="WorkflowBatchItemEntity.position",
    )

    __table_args__ = (Index("ix_workflow_batches_created_at", "created_at"),)


class WorkflowBatchItemEntity(Base):
    __tablename__ = "workflow_batch_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    batch_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("workflow_batches.id", ondelete="CASCADE"), nullable=False
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    task_id: Mapped[str] = mapped_column(String(128), nullable=False)
    task_title: Mapped[str] = mapped_column(Text, nullable=False)
    execution_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("workflow_executions.id", ondelete="SET NULL")
    )

    batch: Mapped[WorkflowBatchEntity] = relationship(back_populates="items")

    __table_args__ = (
        UniqueConstraint("batch_id", "position", name="uq_workflow_batch_items_position"),
        Index("ix_workflow_batch_items_batch_id", "batch_id"),
        Index("ix_workflow_batch_items_execution_id", "execution_id"),
    )


class WorkflowStepEntity(Base):
    __tablename__ = "workflow_steps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workflow_execution_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("workflow_executions.id", ondelete="CASCADE"),
        nullable=False,
    )
    stage: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_message: Mapped[str | None] = mapped_column(Text)

    execution: Mapped[WorkflowExecutionEntity] = relationship(back_populates="steps")

    __table_args__ = (
        Index("ix_workflow_steps_execution_id", "workflow_execution_id"),
        Index("ix_workflow_steps_stage_status", "stage", "status"),
    )


class HumanInteractionEntity(Base):
    __tablename__ = "human_interactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workflow_execution_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("workflow_executions.id", ondelete="CASCADE"),
        nullable=False,
    )
    requesting_stage: Mapped[str] = mapped_column(String(32), nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    response: Mapped[str | None] = mapped_column(Text)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    execution: Mapped[WorkflowExecutionEntity] = relationship(back_populates="human_interactions")

    __table_args__ = (
        Index("ix_human_interactions_execution_id", "workflow_execution_id"),
        Index("ix_human_interactions_response", "workflow_execution_id", "response"),
    )


class ExecutionDiagnosticEntity(Base):
    __tablename__ = "execution_diagnostics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workflow_execution_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("workflow_executions.id", ondelete="CASCADE"), nullable=False
    )
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    stage: Mapped[str | None] = mapped_column(String(32))
    role: Mapped[str | None] = mapped_column(String(32))
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    execution: Mapped[WorkflowExecutionEntity] = relationship(back_populates="diagnostics")

    __table_args__ = (
        Index("ix_execution_diagnostics_execution_id", "workflow_execution_id", "id"),
        Index("ix_execution_diagnostics_category", "workflow_execution_id", "category"),
    )
