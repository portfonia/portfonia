"""Add holding rollback history metadata.

Revision ID: d78300000001
Revises: d76900000001
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "d78300000001"
down_revision = "d76900000001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("source", "holding_change_groups", type_="check")
    op.create_check_constraint(
        "source", "holding_change_groups", "source IN ('web', 'file', 'agent', 'rollback')"
    )
    op.add_column(
        "holding_change_groups",
        sa.Column("rollback_to", postgresql.TIMESTAMP(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.execute("DELETE FROM holding_change_groups WHERE source = 'rollback'")
    op.drop_column("holding_change_groups", "rollback_to")
    op.drop_constraint("source", "holding_change_groups", type_="check")
    op.create_check_constraint(
        "source", "holding_change_groups", "source IN ('web', 'file', 'agent')"
    )
