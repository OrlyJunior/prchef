"""persist workflow cancellation requests

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("workflow_executions") as batch_op:
        batch_op.add_column(sa.Column("cancellation_requested_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    with op.batch_alter_table("workflow_executions") as batch_op:
        batch_op.drop_column("cancellation_requested_at")
