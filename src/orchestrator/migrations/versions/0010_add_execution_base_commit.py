"""record the fetched base commit for each workflow execution

Revision ID: 0010
Revises: 0009
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("workflow_executions", sa.Column("base_commit", sa.String(length=64)))


def downgrade() -> None:
    op.drop_column("workflow_executions", "base_commit")
