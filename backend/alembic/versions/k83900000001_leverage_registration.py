"""Leverage registration classification and underlying earnings (#839).

Revision ID: k83900000001
Revises: j81600000001
"""
from alembic import op
import sqlalchemy as sa
from app.models.holding import _in_list_sql
from app.services.asset_class_config import VALID_ASSET_CLASSES

revision = "k83900000001"
down_revision = "j81600000001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ticker_leverage_overrides", sa.Column("asset_class", sa.Text(), nullable=True))
    op.add_column("ticker_leverage_overrides", sa.Column("underlying_ticker", sa.Text(), nullable=True))
    op.create_check_constraint("ck_ticker_leverage_overrides_asset_class", "ticker_leverage_overrides",
        "asset_class IS NULL OR " + _in_list_sql("asset_class", tuple(sorted(VALID_ASSET_CLASSES))))


def downgrade() -> None:
    op.drop_constraint("ck_ticker_leverage_overrides_asset_class", "ticker_leverage_overrides", type_="check")
    op.drop_column("ticker_leverage_overrides", "underlying_ticker")
    op.drop_column("ticker_leverage_overrides", "asset_class")
