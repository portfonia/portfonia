"""add ticker_themes table

Persists the mapping from individual tickers (or fund codes used as tickers)
to an underlying economic theme (e.g. nasdaq_100, gold, sp500).  A theme groups
holdings that represent the same underlying exposure for aggregated anomaly
reporting in §4.2.

Ring 0: seeded from the holdings discussed in project setup.
Ring 1: an admin UI will let the user manage rows; the table is the authoritative
source — code-side dicts are no longer needed once this table is in place.

Revision ID: 2b3c4d5e6f7a
Revises: 1a2b3c4d5e6f
Create Date: 2026-06-16
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "2b3c4d5e6f7a"
down_revision: Union[str, Sequence[str], None] = "1a2b3c4d5e6f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_SEED: list[tuple[str, str, str, str, str]] = [
    # (ticker, theme, theme_label_zh, theme_label_en, asset_class)
    ("QQQM",   "nasdaq_100",   "纳斯达克100", "Nasdaq 100",   "EQUITY_BROAD"),
    ("513650",  "nasdaq_100",   "纳斯达克100", "Nasdaq 100",   "EQUITY_BROAD"),
    ("VOO",    "sp500",        "标普500",     "S&P 500",      "EQUITY_BROAD"),
    ("EWJ",    "japan_equity", "日本股票",    "Japan Equity", "EQUITY_REGION"),
    ("SGOL",   "gold",         "黄金",        "Gold",         "COMMODITY"),
    ("518660", "gold",         "黄金",        "Gold",         "COMMODITY"),
    ("518800", "gold",         "黄金",        "Gold",         "COMMODITY"),
    ("BOXX",   "tbill",        "短期国债",    "T-Bill",       "BOND_FUND"),
]


def upgrade() -> None:
    op.create_table(
        "ticker_themes",
        sa.Column("ticker", sa.Text(), primary_key=True),
        sa.Column("theme", sa.Text(), nullable=False),
        sa.Column("theme_label_zh", sa.Text(), nullable=False),
        sa.Column("theme_label_en", sa.Text(), nullable=False),
        sa.Column("asset_class", sa.Text(), nullable=False),
        sa.Column(
            "updated_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_ticker_themes_theme", "ticker_themes", ["theme"])

    conn = op.get_bind()
    conn.execute(
        sa.text(
            """
            INSERT INTO ticker_themes
                (ticker, theme, theme_label_zh, theme_label_en, asset_class)
            VALUES
                (:ticker, :theme, :zh, :en, :cls)
            ON CONFLICT (ticker) DO NOTHING
            """
        ),
        [
            {"ticker": t, "theme": th, "zh": zh, "en": en, "cls": cls}
            for t, th, zh, en, cls in _SEED
        ],
    )


def downgrade() -> None:
    op.drop_index("ix_ticker_themes_theme", table_name="ticker_themes")
    op.drop_table("ticker_themes")
