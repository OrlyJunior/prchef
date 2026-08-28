"""initial workflow schema

Revision ID: 0001
Revises:
Create Date: 2026-08-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workflow_executions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("trello_card_id", sa.String(length=128), nullable=False),
        sa.Column("trello_card_url", sa.Text(), nullable=False),
        sa.Column("repository", sa.Text(), nullable=False),
        sa.Column("base_branch", sa.String(length=255), nullable=False),
        sa.Column("working_branch", sa.String(length=255), nullable=True),
        sa.Column("commit_sha", sa.String(length=64), nullable=True),
        sa.Column("pr_url", sa.Text(), nullable=True),
        sa.Column("current_stage", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("task_snapshot_json", sa.Text(), nullable=True),
        sa.Column("spec_snapshot_json", sa.Text(), nullable=True),
        sa.Column("implementation_snapshot_json", sa.Text(), nullable=True),
        sa.Column("validation_snapshot_json", sa.Text(), nullable=True),
        sa.Column("pull_request_snapshot_json", sa.Text(), nullable=True),
        sa.Column("review_snapshot_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_workflow_executions_trello_card_id", "workflow_executions", ["trello_card_id"]
    )
    op.create_index("ix_workflow_executions_status", "workflow_executions", ["status"])
    op.create_index(
        "ix_workflow_executions_current_stage", "workflow_executions", ["current_stage"]
    )
    op.create_table(
        "workflow_steps",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("workflow_execution_id", sa.String(length=36), nullable=False),
        sa.Column("stage", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["workflow_execution_id"], ["workflow_executions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_workflow_steps_execution_id", "workflow_steps", ["workflow_execution_id"]
    )
    op.create_index(
        "ix_workflow_steps_stage_status", "workflow_steps", ["stage", "status"]
    )


def downgrade() -> None:
    op.drop_index("ix_workflow_steps_stage_status", table_name="workflow_steps")
    op.drop_index("ix_workflow_steps_execution_id", table_name="workflow_steps")
    op.drop_table("workflow_steps")
    op.drop_index("ix_workflow_executions_current_stage", table_name="workflow_executions")
    op.drop_index("ix_workflow_executions_status", table_name="workflow_executions")
    op.drop_index("ix_workflow_executions_trello_card_id", table_name="workflow_executions")
    op.drop_table("workflow_executions")
