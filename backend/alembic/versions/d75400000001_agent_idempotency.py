"""Encrypted Agent idempotency results.

Revision ID: d75400000001
Revises: d75300000001
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "d75400000001"
down_revision = "d75300000001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_idempotency",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("operation", sa.Text(), nullable=False),
        sa.Column("key_hash", sa.Text(), nullable=False),
        sa.Column("request_hash", sa.Text(), nullable=False),
        sa.Column("response_status", sa.Integer(), nullable=False),
        sa.Column("response_json", sa.Text(), nullable=False),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "operation", "key_hash"),
        sa.CheckConstraint("operation IN ('holding_create', 'holdings_replace')", name="operation"),
    )
    op.create_index("ix_agent_idempotency_expires_at", "agent_idempotency", ["expires_at"])


def downgrade() -> None:
    op.drop_table("agent_idempotency")
