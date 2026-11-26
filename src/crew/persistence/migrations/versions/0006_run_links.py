"""link runs to an issue and a pull request

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-26 18:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("runs") as batch_op:
        batch_op.add_column(sa.Column("issue_key", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("pull_request_url", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("runs") as batch_op:
        batch_op.drop_column("pull_request_url")
        batch_op.drop_column("issue_key")
