"""split PRECIOUS_METALS/ENERGY out of COMMODITY

asset_class taxonomy adds REIT, PRECIOUS_METALS, and ENERGY
(config/asset_class_thresholds.yml, 2026-06-20). holding_parser.py's ticker
map was updated to classify gold tickers as PRECIOUS_METALS going forward,
but existing rows already classified COMMODITY need a one-time backfill —
otherwise they'd silently fall under the new (re-tuned, tighter) generic
COMMODITY thresholds intended for energy/agriculture/industrial metals
instead of the gold-calibrated PRECIOUS_METALS tier.

No ENERGY backfill: no existing holding was ever classified as an energy
commodity (all current COMMODITY rows are gold).

Revision ID: 8c9d0e1f2a3b
Revises: 6f7a8b9c0d1e
Create Date: 2026-06-20
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "8c9d0e1f2a3b"
down_revision: Union[str, Sequence[str], None] = "6f7a8b9c0d1e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_GOLD_TICKERS = ("SGOL", "GLD", "IAU", "518660", "518660.SS", "518800", "518800.SS")
_GOLD_FUND_CODES = ("008142",)


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "UPDATE holdings SET asset_class = 'PRECIOUS_METALS' "
            "WHERE asset_class = 'COMMODITY' "
            "AND (ticker = ANY(:tickers) OR fund_code = ANY(:fund_codes))"
        ),
        {"tickers": list(_GOLD_TICKERS), "fund_codes": list(_GOLD_FUND_CODES)},
    )
    conn.execute(
        sa.text(
            "UPDATE ticker_themes SET asset_class = 'PRECIOUS_METALS' WHERE theme = 'gold'"
        )
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "UPDATE holdings SET asset_class = 'COMMODITY' "
            "WHERE asset_class = 'PRECIOUS_METALS' "
            "AND (ticker = ANY(:tickers) OR fund_code = ANY(:fund_codes))"
        ),
        {"tickers": list(_GOLD_TICKERS), "fund_codes": list(_GOLD_FUND_CODES)},
    )
    conn.execute(
        sa.text("UPDATE ticker_themes SET asset_class = 'COMMODITY' WHERE theme = 'gold'")
    )
