"""add users and invites tables

Ring 1 stage B checkpoint B4 (issue #129). Two new tables; existing
holdings/reports/upload_jobs/news_surfaced rows are bound to a users row
whose id equals Settings.DEV_USER_ID — only when that id is the sole
distinct user_id across those four tables. Unexpected ids abort the
upgrade rather than invent emails for leftover UAT rows.

Revision ID: e8f9a0b1c2d3
Revises: d6e7f8a9b0c1
Create Date: 2026-08-23

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "e8f9a0b1c2d3"
down_revision: str | Sequence[str] | None = "d6e7f8a9b0c1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES_WITH_USER_ID = ("holdings", "reports", "upload_jobs", "news_surfaced")


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
        ),
        sa.Column("auth_provider", sa.Text(), nullable=False),
        sa.Column("auth_subject", sa.Text(), nullable=True),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("is_admin", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("display_name", sa.Text(), nullable=True),
        sa.Column("locale", sa.Text(), nullable=False),
        sa.Column("base_currency", sa.Text(), nullable=False),
        sa.Column("report_cadence", sa.Text(), nullable=False),
        sa.Column("delivery_email", sa.Text(), nullable=True),
        sa.Column("invited_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("last_login_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("auth_subject", name="uq_users_auth_subject"),
        sa.UniqueConstraint("email", name="uq_users_email"),
        sa.CheckConstraint(
            "status IN ('active', 'deleted', 'suspended')",
            name="status",
        ),
        sa.CheckConstraint("auth_provider IN ('supabase')", name="auth_provider"),
    )
    op.create_table(
        "invites",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
        ),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("email", sa.Text(), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("expires_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("used_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("used_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("revoked_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("token_hash", name="uq_invites_token_hash"),
    )

    from uuid import UUID

    from app.core.config import get_settings

    settings = get_settings()
    expected = UUID(settings.DEV_USER_ID)
    conn = op.get_bind()
    found: set[UUID] = set()
    for table in _TABLES_WITH_USER_ID:
        for (uid,) in conn.execute(sa.text(f"SELECT DISTINCT user_id FROM {table}")):
            if uid is not None:
                found.add(uid if isinstance(uid, UUID) else UUID(str(uid)))
    unexpected = found - {expected}
    if unexpected:
        raise RuntimeError(
            "refusing users migration: unexpected user_id values "
            f"{sorted(str(u) for u in unexpected)} — expected only {expected}"
        )
    if expected in found:
        conn.execute(
            sa.text(
                "INSERT INTO users (id, auth_provider, email, status, locale, "
                "base_currency, report_cadence, is_admin) "
                "VALUES (:id, 'supabase', :email, 'active', :locale, 'USD', 'mwf', true)"
            ),
            {
                "id": expected,
                "email": settings.DEV_USER_EMAIL.strip().lower(),
                "locale": settings.OUTPUT_LANG or "zh",
            },
        )


def downgrade() -> None:
    op.drop_table("invites")
    op.drop_table("users")
