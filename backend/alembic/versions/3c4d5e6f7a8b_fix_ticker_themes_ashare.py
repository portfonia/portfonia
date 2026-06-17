"""fix A-share ticker suffixes in ticker_themes + asset_class backfill

The original seed used bare codes (518660, 518800, 513650) but A-share ETFs are
stored with .SS suffix in both holdings.ticker and price_snapshots.ticker (yfinance
convention for Shanghai-listed ETFs).  This migration replaces those rows with the
correct .SS-suffixed keys so theme-aggregation and asset_class lookup both work.

Also corrects 513650.SS theme: the holding is named "南方标普" (South China Asset
Mgmt S&P 500 ETF), so it belongs to sp500, not nasdaq_100.

Asset_class fixes on existing holding rows (migration backfill missed .SS tickers):
  518660.SS  COMMODITY
  518800.SS  COMMODITY
  513650.SS  EQUITY_BROAD

Revision ID: 3c4d5e6f7a8b
Revises: 2b3c4d5e6f7a
Create Date: 2026-06-17
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "3c4d5e6f7a8b"
down_revision: Union[str, Sequence[str], None] = "2b3c4d5e6f7a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Rows to remove (bare codes, now replaced by .SS-suffixed versions).
_REMOVE = ("518660", "518800", "513650")

# Replacement rows: (ticker, theme, theme_label_zh, theme_label_en, asset_class)
_ADD = [
    ("518660.SS", "gold",  "黄金",  "Gold",    "COMMODITY"),
    ("518800.SS", "gold",  "黄金",  "Gold",    "COMMODITY"),
    ("513650.SS", "sp500", "标普500", "S&P 500", "EQUITY_BROAD"),
]


def upgrade() -> None:
    conn = op.get_bind()

    # Remove bare-code rows.
    conn.execute(
        sa.text("DELETE FROM ticker_themes WHERE ticker = ANY(:tickers)"),
        {"tickers": list(_REMOVE)},
    )

    # Insert .SS-suffixed rows.
    conn.execute(
        sa.text(
            "INSERT INTO ticker_themes "
            "(ticker, theme, theme_label_zh, theme_label_en, asset_class) "
            "VALUES (:ticker, :theme, :zh, :en, :cls) "
            "ON CONFLICT (ticker) DO UPDATE "
            "SET theme=EXCLUDED.theme, theme_label_zh=EXCLUDED.theme_label_zh, "
            "    theme_label_en=EXCLUDED.theme_label_en, asset_class=EXCLUDED.asset_class"
        ),
        [{"ticker": t, "theme": th, "zh": zh, "en": en, "cls": cls} for t, th, zh, en, cls in _ADD],
    )

    # Fix holdings.asset_class for rows the original backfill missed.
    conn.execute(
        sa.text(
            "UPDATE holdings SET asset_class = :cls WHERE UPPER(ticker) = :ticker"
        ),
        [
            {"cls": "COMMODITY",     "ticker": "518660.SS"},
            {"cls": "COMMODITY",     "ticker": "518800.SS"},
            {"cls": "EQUITY_BROAD",  "ticker": "513650.SS"},
        ],
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text("DELETE FROM ticker_themes WHERE ticker = ANY(:tickers)"),
        {"tickers": [t for t, *_ in _ADD]},
    )
    # Restore bare-code rows (seed values from 2b3c4d5e6f7a).
    conn.execute(
        sa.text(
            "INSERT INTO ticker_themes "
            "(ticker, theme, theme_label_zh, theme_label_en, asset_class) VALUES "
            "('518660','gold','黄金','Gold','COMMODITY'), "
            "('518800','gold','黄金','Gold','COMMODITY'), "
            "('513650','nasdaq_100','纳斯达克100','Nasdaq 100','EQUITY_BROAD') "
            "ON CONFLICT DO NOTHING"
        )
    )
