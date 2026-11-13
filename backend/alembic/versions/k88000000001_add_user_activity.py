"""Add 90-day account activity records (#880)."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "k88000000001"
down_revision = "k84200000001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_activity",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=True),
        sa.Column("event", sa.Text(), nullable=False),
        sa.Column("detail", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("client_ip", sa.Text(), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
    )
    op.create_index("ix_user_activity_user_occurred", "user_activity", ["user_id", "occurred_at"])
    op.create_index("ix_user_activity_occurred", "user_activity", ["occurred_at"])


def downgrade() -> None:
    op.drop_table("user_activity")
