"""persist optional polish outcomes

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("workflow_executions") as batch_op:
        batch_op.add_column(sa.Column("polish_outcome", sa.String(length=32), nullable=True))
        batch_op.add_column(
            sa.Column(
                "polish_skip_comment_attempted",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("workflow_executions") as batch_op:
        batch_op.drop_column("polish_skip_comment_attempted")
        batch_op.drop_column("polish_outcome")
