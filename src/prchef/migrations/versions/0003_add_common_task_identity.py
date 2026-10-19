"""add common task identity

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("workflow_executions") as batch_op:
        batch_op.add_column(sa.Column("task_source", sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column("task_id", sa.String(length=128), nullable=True))
        batch_op.add_column(sa.Column("task_url", sa.Text(), nullable=True))

    op.execute(
        "UPDATE workflow_executions "
        "SET task_source = 'trello', task_id = trello_card_id, task_url = trello_card_url"
    )

    with op.batch_alter_table("workflow_executions") as batch_op:
        batch_op.alter_column("task_source", existing_type=sa.String(length=32), nullable=False)
        batch_op.alter_column("task_id", existing_type=sa.String(length=128), nullable=False)
        batch_op.alter_column("task_url", existing_type=sa.Text(), nullable=False)
        batch_op.create_index(
            "ix_workflow_executions_task_source_id", ["task_source", "task_id"]
        )


def downgrade() -> None:
    with op.batch_alter_table("workflow_executions") as batch_op:
        batch_op.drop_index("ix_workflow_executions_task_source_id")
        batch_op.drop_column("task_url")
        batch_op.drop_column("task_id")
        batch_op.drop_column("task_source")
