"""URL-free article bodies and paid usage.

Revision ID: d62100000001
Revises: d62000000001
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

from alembic import op

revision = "d62100000001"
down_revision = "d62000000001"
branch_labels = None
depends_on = None


def _id() -> sa.Column:
    return sa.Column(
        "id", pg.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
    )


def upgrade() -> None:
    op.create_table(
        "intel_articles",
        _id(),
        sa.Column("slot_run_id", pg.UUID(), sa.ForeignKey("intel_slot_runs.id"), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("url_key", sa.Text(), nullable=False),
        sa.Column("news_id", pg.UUID(), sa.ForeignKey("news.id", ondelete="SET NULL")),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("reject_reason", sa.Text()),
        sa.Column("record", pg.JSONB(none_as_null=True)),
        sa.Column("body_chars", sa.Integer()),
        sa.Column("fetched_at", pg.TIMESTAMP(timezone=True), nullable=False),
        sa.UniqueConstraint("url_key", "provider", name="uq_intel_articles_key_provider"),
        sa.CheckConstraint(
            "provider IN ('tavily','parallel')", name=op.f("ck_intel_articles_provider")
        ),
        sa.CheckConstraint(
            "status IN ('accepted','rejected','failed')", name=op.f("ck_intel_articles_status")
        ),
        sa.CheckConstraint(
            "(status = 'accepted') = (record IS NOT NULL)",
            name=op.f("ck_intel_articles_record_status"),
        ),
    )
    op.create_index(
        "ix_intel_articles_status_fetched_at", "intel_articles", ["status", "fetched_at"]
    )
    op.create_table(
        "intel_article_links",
        _id(),
        sa.Column(
            "article_id",
            pg.UUID(),
            sa.ForeignKey("intel_articles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("identifier", sa.Text()),
        sa.Column("theme", sa.Text()),
        sa.Column("role", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "(identifier IS NOT NULL) <> (theme IS NOT NULL)",
            name=op.f("ck_intel_article_links_owner"),
        ),
        sa.CheckConstraint(
            "role IN ('mover','quiet','macro')", name=op.f("ck_intel_article_links_role")
        ),
        sa.UniqueConstraint(
            "article_id",
            "identifier",
            "theme",
            name="uq_intel_article_links_key",
            postgresql_nulls_not_distinct=True,
        ),
    )
    op.create_table(
        "paid_api_usage",
        _id(),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column(
            "slot_run_id", pg.UUID(), sa.ForeignKey("intel_slot_runs.id", ondelete="SET NULL")
        ),
        sa.Column("operation", sa.Text(), nullable=False),
        sa.Column("units", sa.Numeric(10, 3), nullable=False),
        sa.Column("cost_usd", sa.Numeric(10, 4), nullable=False),
        sa.Column("http_status", sa.Integer()),
        sa.Column(
            "created_at", pg.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "provider IN ('tavily','parallel')", name=op.f("ck_paid_api_usage_provider")
        ),
        sa.CheckConstraint(
            "operation IN ('search','extract')", name=op.f("ck_paid_api_usage_operation")
        ),
    )
    op.create_index(
        "ix_paid_api_usage_provider_created_at", "paid_api_usage", ["provider", "created_at"]
    )


def downgrade() -> None:
    op.drop_table("paid_api_usage")
    op.drop_table("intel_article_links")
    op.drop_table("intel_articles")
