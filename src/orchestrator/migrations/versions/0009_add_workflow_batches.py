"""add durable workflow batches

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workflow_batches",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("repository", sa.Text(), nullable=False),
        sa.Column("task_source", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_workflow_batches_created_at", "workflow_batches", ["created_at"], unique=False
    )
    op.create_table(
        "workflow_batch_items",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("batch_id", sa.String(length=36), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.String(length=128), nullable=False),
        sa.Column("task_title", sa.Text(), nullable=False),
        sa.Column("execution_id", sa.String(length=36), nullable=True),
        sa.ForeignKeyConstraint(["batch_id"], ["workflow_batches.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["execution_id"], ["workflow_executions.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("batch_id", "position", name="uq_workflow_batch_items_position"),
    )
    op.create_index(
        "ix_workflow_batch_items_batch_id", "workflow_batch_items", ["batch_id"], unique=False
    )
    op.create_index(
        "ix_workflow_batch_items_execution_id",
        "workflow_batch_items",
        ["execution_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_workflow_batch_items_execution_id", table_name="workflow_batch_items")
    op.drop_index("ix_workflow_batch_items_batch_id", table_name="workflow_batch_items")
    op.drop_table("workflow_batch_items")
    op.drop_index("ix_workflow_batches_created_at", table_name="workflow_batches")
    op.drop_table("workflow_batches")
