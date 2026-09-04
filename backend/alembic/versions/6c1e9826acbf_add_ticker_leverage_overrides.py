"""add ticker_leverage_overrides table

System-wide (not per-user) ticker -> leverage_multiple lookup, keyed by the
same normalized+uppercased ticker form the FX-pair/asset_class lookups use
(issue #204 mechanism note). Read-time join only: window_data.py widens
anomaly per_day/cumulative_cap by leverage_multiple, portfolio_calculator.py
tightens §4.1 single-holding concentration watch/high by the same factor.
Holding.asset_class is never modified by this table (issue #87).

Revision ID: 6c1e9826acbf
Revises: d3e4f5a6b7c8
Create Date: 2026-09-03
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "6c1e9826acbf"
down_revision: Union[str, Sequence[str], None] = "d3e4f5a6b7c8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ticker_leverage_overrides",
        sa.Column("ticker", sa.Text(), primary_key=True),
        sa.Column("leverage_multiple", sa.Numeric(), nullable=False),
        sa.Column("direction", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "leverage_multiple > 0", name="ck_ticker_leverage_overrides_leverage_multiple_positive"
        ),
        sa.CheckConstraint(
            "direction IS NULL OR direction IN ('bear', 'bull')",
            name="ck_ticker_leverage_overrides_direction",
        ),
    )


def downgrade() -> None:
    op.drop_table("ticker_leverage_overrides")
