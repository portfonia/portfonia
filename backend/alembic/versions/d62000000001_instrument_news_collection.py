"""Standard news records and collection runs; deletes old news irreversibly.

Revision ID: d62000000001
Revises: s59500000001
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg
from alembic import op

revision = "d62000000001"
down_revision = "s59500000001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DELETE FROM news WHERE published_at < now() - interval '30 days'")
    op.add_column(
        "news", sa.Column("origin", sa.Text(), nullable=False, server_default="pool")
    )
    op.add_column(
        "news", sa.Column("kind", sa.Text(), nullable=False, server_default="article")
    )
    op.add_column("news", sa.Column("intel_label", sa.Text()))
    op.add_column("news", sa.Column("record", pg.JSONB()))
    op.execute(
        """UPDATE news SET record=jsonb_build_object('v',1,'kind','article','title',title,'summary',summary,'published_at',to_char(published_at AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),'collected_at',to_char(fetched_at AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),'label',NULL,'filing_form',NULL)"""
    )
    op.alter_column("news", "record", nullable=False)
    for c in ["title", "summary", "source", "url"]:
        op.drop_column("news", c)
    for c, expr in [
        ("origin", "origin IN ('pool','instrument')"),
        ("kind", "kind IN ('article','filing')"),
        ("intel_label", "intel_label IN ('keep','mention')"),
    ]:
        op.create_check_constraint(op.f("ck_news_" + c), "news", expr)
    op.create_index("ix_news_origin_published_at", "news", ["origin", "published_at"])
    # Frozen schema definitions: never import live ORM models into migrations.
    stamp = lambda n, nullable=True: sa.Column(
        n, pg.TIMESTAMP(timezone=True), nullable=nullable
    )
    ident = lambda: sa.Column(
        "id",
        pg.UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
    )
    op.create_table(
        "intel_slot_runs",
        ident(),
        sa.Column("slot", sa.Text(), nullable=False),
        sa.Column("run_date", sa.Date(), nullable=False),
        stamp("started_at", False),
        stamp("finished_at"),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("details", pg.JSONB(), nullable=False, server_default="{}"),
        stamp("digest_sent_at"),
        sa.UniqueConstraint("slot", "run_date", name="uq_intel_slot_key"),
        sa.CheckConstraint(
            "slot IN ('pre_open','post_close')", name=op.f("ck_intel_slot_runs_slot")
        ),
        sa.CheckConstraint(
            "status IN ('running','ok','partial','failed')",
            name=op.f("ck_intel_slot_runs_status"),
        ),
    )
    op.create_table(
        "intel_collection_runs",
        ident(),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("slot_run_id", pg.UUID(), sa.ForeignKey("intel_slot_runs.id")),
        sa.Column("node", sa.Text()),
        stamp("started_at", False),
        stamp("finished_at"),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("instruments_total", sa.Integer()),
        sa.Column("instruments_processed", sa.Integer()),
        sa.Column("stats", pg.JSONB(), nullable=False, server_default="{}"),
        sa.Column("errors", pg.JSONB(), nullable=False, server_default="[]"),
        sa.CheckConstraint(
            "kind IN ('rss','instrument')", name=op.f("ck_intel_collection_runs_kind")
        ),
        sa.CheckConstraint(
            "status IN ('running','ok','partial','failed')",
            name=op.f("ck_intel_collection_runs_status"),
        ),
    )
    op.create_table(
        "instrument_profiles",
        sa.Column("identifier", sa.Text(), primary_key=True),
        sa.Column("market", sa.Text(), nullable=False),
        sa.Column("name_en", sa.Text()),
        sa.Column("name_zh", sa.Text()),
        sa.Column("aliases", pg.JSONB(), nullable=False, server_default="[]"),
        sa.Column("name_source", sa.Text()),
        stamp("name_resolved_at"),
        stamp("news_collected_at"),
        sa.Column(
            "updated_at",
            pg.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_table(
        "news_instruments",
        ident(),
        sa.Column(
            "news_id",
            pg.UUID(),
            sa.ForeignKey("news.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("identifier", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            pg.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("news_id", "identifier", name="uq_news_instruments_key"),
    )
    op.create_index(
        "ix_news_instruments_identifier_created_at",
        "news_instruments",
        ["identifier", "created_at"],
    )


def downgrade() -> None:
    for t in [
        "news_instruments",
        "instrument_profiles",
        "intel_collection_runs",
        "intel_slot_runs",
    ]:
        op.drop_table(t)
    for c in ["title", "summary", "source", "url"]:
        op.add_column(
            "news",
            sa.Column(
                c,
                sa.Text(),
                nullable=c == "summary",
                server_default=None if c == "summary" else "",
            ),
        )
    op.execute("UPDATE news SET title=record->>'title', summary=record->>'summary'")
    op.drop_index("ix_news_origin_published_at", table_name="news")
    for c in ["origin", "kind", "intel_label"]:
        op.drop_constraint(op.f("ck_news_" + c), "news", type_="check")
    for c in ["origin", "kind", "intel_label", "record"]:
        op.drop_column("news", c)
