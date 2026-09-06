"""add persisted human interaction records

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("workflow_executions") as batch_op:
        batch_op.add_column(
            sa.Column("repository_ref", sa.Text(), nullable=False, server_default="")
        )
    op.create_table(
        "human_interactions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("workflow_execution_id", sa.String(length=36), nullable=False),
        sa.Column("requesting_stage", sa.String(length=32), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("response", sa.Text(), nullable=True),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["workflow_execution_id"], ["workflow_executions.id"], ondelete="CASCADE"
        ),
    )
    op.create_index(
        "ix_human_interactions_execution_id", "human_interactions", ["workflow_execution_id"]
    )
    op.create_index(
        "ix_human_interactions_response",
        "human_interactions",
        ["workflow_execution_id", "response"],
    )


def downgrade() -> None:
    op.drop_index("ix_human_interactions_response", table_name="human_interactions")
    op.drop_index("ix_human_interactions_execution_id", table_name="human_interactions")
    op.drop_table("human_interactions")
    with op.batch_alter_table("workflow_executions") as batch_op:
        batch_op.drop_column("repository_ref")
