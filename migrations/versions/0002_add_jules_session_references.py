"""add jules session references

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("workflow_executions") as batch_op:
        batch_op.add_column(
            sa.Column("spec_jules_session_id", sa.String(length=255), nullable=True)
        )
        batch_op.add_column(sa.Column("spec_jules_session_url", sa.Text(), nullable=True))
        batch_op.add_column(
            sa.Column("review_jules_session_id", sa.String(length=255), nullable=True)
        )
        batch_op.add_column(sa.Column("review_jules_session_url", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("workflow_executions") as batch_op:
        batch_op.drop_column("review_jules_session_url")
        batch_op.drop_column("review_jules_session_id")
        batch_op.drop_column("spec_jules_session_url")
        batch_op.drop_column("spec_jules_session_id")
