"""Encrypted holding change history.

Revision ID: d76900000001
Revises: d75400000001
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "d76900000001"
down_revision = "d75400000001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "holding_change_groups",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("token_name", sa.Text(), nullable=True),
        sa.Column("token_prefix", sa.Text(), nullable=True),
        sa.Column("changes", sa.Text(), nullable=False),
        sa.CheckConstraint("source IN ('web', 'file', 'agent')", name="source"),
    )
    op.create_index("ix_holding_change_groups_user_id_created_at", "holding_change_groups", ["user_id", "created_at"])


def downgrade() -> None:
    op.drop_table("holding_change_groups")
