"""Rename doubled-prefix leverage CHECK constraints (#842).

Revision ID: k84200000001
Revises: k83900000001
"""

from alembic import op

revision = "k84200000001"
down_revision = "k83900000001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE ticker_leverage_overrides RENAME CONSTRAINT "
        "ck_ticker_leverage_overrides_ck_ticker_leverage_overrid_4d1b "
        "TO ck_ticker_leverage_overrides_leverage_multiple_positive"
    )
    op.execute(
        "ALTER TABLE ticker_leverage_overrides RENAME CONSTRAINT "
        "ck_ticker_leverage_overrides_ck_ticker_leverage_overrid_28ef "
        "TO ck_ticker_leverage_overrides_direction"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE ticker_leverage_overrides RENAME CONSTRAINT "
        "ck_ticker_leverage_overrides_leverage_multiple_positive "
        "TO ck_ticker_leverage_overrides_ck_ticker_leverage_overrid_4d1b"
    )
    op.execute(
        "ALTER TABLE ticker_leverage_overrides RENAME CONSTRAINT "
        "ck_ticker_leverage_overrides_direction "
        "TO ck_ticker_leverage_overrides_ck_ticker_leverage_overrid_28ef"
    )
