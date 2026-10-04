"""Add personal API tokens and agent request audit metadata.

Revision ID: d64200000001
Revises: d64100000001
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg
from alembic import op

revision: str = "d64200000001"
down_revision: str | None = "d64100000001"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table("api_tokens",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", pg.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False, unique=True),
        sa.Column("token_prefix", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("last_used_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_by", sa.Text()),
        sa.CheckConstraint("length(trim(name)) BETWEEN 1 AND 50", name=op.f("ck_api_tokens_name")),
        sa.CheckConstraint("revoked_by IN ('user', 'email_link', 'ops')", name=op.f("ck_api_tokens_revoked_by")),
    )
    op.create_index("ix_api_tokens_user_id", "api_tokens", ["user_id"])
    op.create_table("api_audit_log",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("user_id", pg.UUID(as_uuid=True), sa.ForeignKey("users.id")),
        sa.Column("token_id", pg.UUID(as_uuid=True), sa.ForeignKey("api_tokens.id")),
        sa.Column("token_prefix", sa.Text()),
        sa.Column("endpoint", sa.Text(), nullable=False),
        sa.Column("params", pg.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("status_code", sa.SmallInteger(), nullable=False),
        sa.Column("item_count", sa.Integer()),
        sa.Column("client_ip", sa.Text(), nullable=False),
        sa.Column("user_agent", sa.Text()),
    )
    op.create_index("ix_api_audit_log_user_occurred", "api_audit_log", ["user_id", "occurred_at"])


def downgrade() -> None:
    op.drop_table("api_audit_log")
    op.drop_table("api_tokens")
