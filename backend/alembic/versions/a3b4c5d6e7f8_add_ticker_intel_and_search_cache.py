"""add ticker_intel and search_cache tables

Issue #128 A2 (Ring 1 stage A, L1 shared ticker intel + Tavily search cache —
design doc Hermes/Portfonia/Docs/Ring 1-A design.md §4.4). Both tables are
pure new-table additions with no existing data to backfill:

- `ticker_intel`: one LLM analysis per (identifier, trade_date,
  prompt_version), shared across every user who holds the identifier that
  day instead of re-analyzing it once per user.
- `search_cache`: one Tavily result set per (query_hash, trade_date), shared
  across every report that proposes the same query that day. This table is
  also the new source of truth for the daily Tavily spend count
  (app.services.report_search._tavily_used_today) — previously that count
  summed proposed queries per report_inputs, which double-counted a query
  that hit cache and made no real API call.

Revision ID: a3b4c5d6e7f8
Revises: f1a2b3c4d5e6
Create Date: 2026-08-15 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "a3b4c5d6e7f8"
down_revision: str | Sequence[str] | None = "f1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ticker_intel",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
        ),
        sa.Column("identifier", sa.Text(), nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("prompt_version", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("analysis", sa.Text(), nullable=False),
        sa.Column("facts", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_unique_constraint(
        "uq_ticker_intel_identifier_date_version",
        "ticker_intel",
        ["identifier", "trade_date", "prompt_version"],
    )
    op.create_index("ix_ticker_intel_identifier", "ticker_intel", ["identifier"])
    op.create_index("ix_ticker_intel_trade_date", "ticker_intel", ["trade_date"])

    op.create_table(
        "search_cache",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
        ),
        sa.Column("query_hash", sa.Text(), nullable=False),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("results", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_unique_constraint(
        "uq_search_cache_query_date", "search_cache", ["query_hash", "trade_date"]
    )
    op.create_index("ix_search_cache_query_hash", "search_cache", ["query_hash"])
    op.create_index("ix_search_cache_trade_date", "search_cache", ["trade_date"])


def downgrade() -> None:
    op.drop_table("search_cache")
    op.drop_table("ticker_intel")
