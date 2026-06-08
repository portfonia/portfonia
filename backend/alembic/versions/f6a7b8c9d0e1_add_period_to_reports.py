"""add period_start and period_end to reports

ADR-002 report layer: each report records the window it covered. The next
report's window starts at the previous report's period_end (watermark derived
as max(period_end) per user), so deleting a report rolls the watermark back.

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-06-07 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f6a7b8c9d0e1"
down_revision: Union[str, Sequence[str], None] = "e5f6a7b8c9d0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "reports", sa.Column("period_start", postgresql.TIMESTAMP(timezone=True), nullable=True)
    )
    op.add_column(
        "reports", sa.Column("period_end", postgresql.TIMESTAMP(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("reports", "period_end")
    op.drop_column("reports", "period_start")
