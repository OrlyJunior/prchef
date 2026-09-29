"""Record recoverable workflow interruption and restart checkpoint.

Revision ID: 0011
Revises: 0010
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("workflow_executions", sa.Column("interrupted_from_status", sa.String(32)))
    op.add_column("workflow_executions", sa.Column("resume_stage", sa.String(32)))
    op.add_column("workflow_executions", sa.Column("resume_mode", sa.String(32)))
    op.add_column("workflow_executions", sa.Column(
        "review_retry_attempt", sa.Integer(), nullable=False, server_default="0"
    ))
    op.add_column("workflow_executions", sa.Column(
        "validation_retry_attempt", sa.Integer(), nullable=False, server_default="0"
    ))
    op.add_column("workflow_executions", sa.Column(
        "review_publication_pending", sa.Boolean(), nullable=False, server_default="0"
    ))


def downgrade() -> None:
    op.drop_column("workflow_executions", "validation_retry_attempt")
    op.drop_column("workflow_executions", "review_retry_attempt")
    op.drop_column("workflow_executions", "resume_mode")
    op.drop_column("workflow_executions", "review_publication_pending")
    op.drop_column("workflow_executions", "resume_stage")
    op.drop_column("workflow_executions", "interrupted_from_status")
