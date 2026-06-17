"""add fund holdings to ticker_themes + asset_class backfill

Adds 019547 (招商纳斯达克100指数基金 → nasdaq_100, EQUITY_BROAD) and
008142 (工银黄金ETF联接基金 → gold, COMMODITY) to ticker_themes so these
fund holdings participate in theme-level anomaly aggregation alongside their
exchange-traded equivalents (QQQM, 518660.SS, 518800.SS).

Also backfills holdings.asset_class for these two rows which were unclassified
(defaulted to STOCK from the initial asset_class migration).

Revision ID: 4d5e6f7a8b9c
Revises: 3c4d5e6f7a8b
Create Date: 2026-06-16
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "4d5e6f7a8b9c"
down_revision: Union[str, Sequence[str], None] = "3c4d5e6f7a8b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ADD = [
    ("019547", "nasdaq_100", "纳斯达克100", "Nasdaq 100", "EQUITY_BROAD"),
    ("008142", "gold",       "黄金",        "Gold",       "COMMODITY"),
]


def upgrade() -> None:
    conn = op.get_bind()

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

    conn.execute(
        sa.text("UPDATE holdings SET asset_class = :cls WHERE fund_code = :fc"),
        [
            {"cls": "EQUITY_BROAD", "fc": "019547"},
            {"cls": "COMMODITY",    "fc": "008142"},
        ],
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text("DELETE FROM ticker_themes WHERE ticker = ANY(:tickers)"),
        {"tickers": [t for t, *_ in _ADD]},
    )
