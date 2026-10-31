"""Record application-instance ownership and heartbeat liveness.

Revision ID: 0013
Revises: 0012
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "execution_owners",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
    )
    with op.batch_alter_table("workflow_executions") as batch:
        batch.add_column(sa.Column("owner_id", sa.String(36)))
        batch.create_foreign_key(
            "fk_workflow_executions_owner_id", "execution_owners", ["owner_id"], ["id"]
        )


def downgrade() -> None:
    with op.batch_alter_table("workflow_executions") as batch:
        batch.drop_constraint("fk_workflow_executions_owner_id", type_="foreignkey")
        batch.drop_column("owner_id")
    op.drop_table("execution_owners")
