"""refine equity asset_class: US-broad / US-tech / DM / CN / EM

Replaces the coarse EQUITY_BROAD / EQUITY_REGION buckets with a
geography-first taxonomy classified by underlying market exposure, not
listing location.  A-share ETFs tracking S&P 500 (513650.SS) or Nasdaq
100 (019547) go into EQUITY_US_* alongside their US-listed equivalents.

New values added:
  EQUITY_US_BROAD  S&P 500 / US total market
  EQUITY_US_TECH   Nasdaq 100 / US tech
  EQUITY_DM        Non-US developed markets (Japan, Europe, …)
  EQUITY_CN        China equity (A-share, HK Chinese, China QDII)
  EQUITY_EM        Emerging markets ex-China  (no current holdings)

EQUITY_BROAD is kept as a catch-all fallback for unidentified global funds.
EQUITY_REGION is kept for backward compat; existing rows are migrated.

Revision ID: 6f7a8b9c0d1e
Revises: 5e6f7a8b9c0d
Create Date: 2026-06-16
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "6f7a8b9c0d1e"
down_revision: Union[str, Sequence[str], None] = "5e6f7a8b9c0d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Holdings migrations: (new_class, SQL condition on holdings table)
_HOLDING_UPDATES = [
    # US tech / Nasdaq 100
    ("EQUITY_US_TECH",  "UPPER(ticker) IN ('QQQM','QQQ') OR fund_code = '019547'"),
    # US broad / S&P 500 / total market
    ("EQUITY_US_BROAD", "UPPER(ticker) IN ('VOO','VTI','SPY','IVV','513650','513650.SS')"),
    # Developed markets ex-US
    ("EQUITY_DM",       "UPPER(ticker) = 'EWJ'"),
    # China equity
    ("EQUITY_CN",       "UPPER(ticker) IN ('FXI','KWEB') OR fund_code = '110011'"),
]

# ticker_themes migrations: (new_class, theme key)
_THEME_UPDATES = [
    ("EQUITY_US_TECH",  "nasdaq_100"),
    ("EQUITY_US_BROAD", "sp500"),
    ("EQUITY_DM",       "japan_equity"),
]


def upgrade() -> None:
    conn = op.get_bind()

    for new_class, condition in _HOLDING_UPDATES:
        conn.execute(
            sa.text(f"UPDATE holdings SET asset_class = :cls WHERE {condition}"),
            {"cls": new_class},
        )

    for new_class, theme in _THEME_UPDATES:
        conn.execute(
            sa.text("UPDATE ticker_themes SET asset_class = :cls WHERE theme = :theme"),
            {"cls": new_class, "theme": theme},
        )


def downgrade() -> None:
    conn = op.get_bind()

    # Reverse holdings: US-specific → EQUITY_BROAD; DM/CN → EQUITY_REGION
    conn.execute(
        sa.text(
            "UPDATE holdings SET asset_class = 'EQUITY_BROAD' "
            "WHERE asset_class IN ('EQUITY_US_BROAD','EQUITY_US_TECH')"
        )
    )
    conn.execute(
        sa.text(
            "UPDATE holdings SET asset_class = 'EQUITY_REGION' "
            "WHERE asset_class IN ('EQUITY_DM','EQUITY_CN','EQUITY_EM')"
        )
    )

    for orig_class, theme in [
        ("EQUITY_BROAD",  "nasdaq_100"),
        ("EQUITY_BROAD",  "sp500"),
        ("EQUITY_REGION", "japan_equity"),
    ]:
        conn.execute(
            sa.text("UPDATE ticker_themes SET asset_class = :cls WHERE theme = :theme"),
            {"cls": orig_class, "theme": theme},
        )
