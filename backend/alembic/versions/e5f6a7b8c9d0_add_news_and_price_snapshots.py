"""add news and price_snapshots tables

ADR-002 capture layer: persistent news knowledge base + historical price
snapshots, so incremental reports can query a window from storage instead of a
live RSS pull (RSS only carries ~2 days).

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-06-07 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e5f6a7b8c9d0"
down_revision: Union[str, Sequence[str], None] = "d4e5f6a7b8c9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "news",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
        ),
        sa.Column("url_hash", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("published_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column(
            "fetched_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("url_hash", name="uq_news_url_hash"),
    )
    # Window queries select by published_at; index it.
    op.create_index("ix_news_published_at", "news", ["published_at"])

    op.create_table(
        "price_snapshots",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
        ),
        sa.Column("ticker", sa.Text(), nullable=False),
        sa.Column("market", sa.Text(), nullable=False),
        sa.Column("session_node", sa.Text(), nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("open", sa.Numeric(), nullable=True),
        sa.Column("high", sa.Numeric(), nullable=True),
        sa.Column("low", sa.Numeric(), nullable=True),
        sa.Column("close", sa.Numeric(), nullable=True),
        sa.Column("last", sa.Numeric(), nullable=True),
        sa.Column("volume", sa.Numeric(), nullable=True),
        sa.Column("source", sa.Text(), server_default=sa.text("'yfinance'"), nullable=False),
        sa.Column(
            "captured_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "ticker", "market", "session_node", "trade_date", name="uq_price_snapshots_key"
        ),
    )
    op.create_index(
        "ix_price_snapshots_ticker_trade_date", "price_snapshots", ["ticker", "trade_date"]
    )


def downgrade() -> None:
    op.drop_index("ix_price_snapshots_ticker_trade_date", table_name="price_snapshots")
    op.drop_table("price_snapshots")
    op.drop_index("ix_news_published_at", table_name="news")
    op.drop_table("news")
