"""record agent process creation time

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-26 14:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("agent_executions") as batch_op:
        batch_op.add_column(sa.Column("process_started_at", sa.Float(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("agent_executions") as batch_op:
        batch_op.drop_column("process_started_at")
