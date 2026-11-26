"""record token usage, turns and model per agent execution

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-26 17:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("agent_executions") as batch_op:
        batch_op.add_column(sa.Column("num_turns", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("model", sa.String(length=120), nullable=True))
        batch_op.add_column(sa.Column("usage_json", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("agent_executions") as batch_op:
        batch_op.drop_column("usage_json")
        batch_op.drop_column("model")
        batch_op.drop_column("num_turns")
