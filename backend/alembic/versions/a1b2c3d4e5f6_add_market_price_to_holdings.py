"""add market_price and price_fetched_at to holdings

Revision ID: a1b2c3d4e5f6
Revises: 93ec5cf25995
Create Date: 2026-06-04 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = "93ec5cf25995"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("holdings", sa.Column("market_price", sa.Numeric(), nullable=True))
    op.add_column(
        "holdings",
        sa.Column(
            "price_fetched_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("holdings", "price_fetched_at")
    op.drop_column("holdings", "market_price")
