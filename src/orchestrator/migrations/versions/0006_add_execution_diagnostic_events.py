"""add sanitized execution diagnostic events

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "execution_diagnostics",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("workflow_execution_id", sa.String(length=36), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("stage", sa.String(length=32), nullable=True),
        sa.Column("role", sa.String(length=32), nullable=True),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["workflow_execution_id"], ["workflow_executions.id"], ondelete="CASCADE"
        ),
    )
    op.create_index(
        "ix_execution_diagnostics_execution_id",
        "execution_diagnostics",
        ["workflow_execution_id", "id"],
    )
    op.create_index(
        "ix_execution_diagnostics_category",
        "execution_diagnostics",
        ["workflow_execution_id", "category"],
    )


def downgrade() -> None:
    op.drop_index("ix_execution_diagnostics_category", table_name="execution_diagnostics")
    op.drop_index("ix_execution_diagnostics_execution_id", table_name="execution_diagnostics")
    op.drop_table("execution_diagnostics")
