"""Associate reviewer output with the stage attempt that produced it.

Revision ID: 0012
Revises: 0011
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("workflow_executions", sa.Column("review_step_id", sa.Integer()))


def downgrade() -> None:
    op.drop_column("workflow_executions", "review_step_id")
