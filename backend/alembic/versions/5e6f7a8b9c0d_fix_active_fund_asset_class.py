"""fix asset_class for active equity fund holdings

Active equity funds (fund_code holdings not indexed to a specific commodity/bond
theme) should be EQUITY_BROAD, not the STOCK default. The original
asset_class migration seeded STOCK as the default for all rows before
backfilling known tickers — fund holdings with a fund_code but no ticker
were not covered by that backfill.

Correction: 110011 (易方达蓝筹精选混合) STOCK → EQUITY_BROAD.

Revision ID: 5e6f7a8b9c0d
Revises: 4d5e6f7a8b9c
Create Date: 2026-06-16
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "5e6f7a8b9c0d"
down_revision: Union[str, Sequence[str], None] = "4d5e6f7a8b9c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "UPDATE holdings SET asset_class = 'EQUITY_BROAD' "
            "WHERE fund_code = '110011' AND asset_class = 'STOCK'"
        )
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "UPDATE holdings SET asset_class = 'STOCK' "
            "WHERE fund_code = '110011' AND asset_class = 'EQUITY_BROAD'"
        )
    )
