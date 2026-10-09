"""Shared Jade total-return cache.

Revision ID: d71400000001
Revises: d71000000001
"""
from alembic import op
import sqlalchemy as sa
revision = "d71400000001"
down_revision = "d71000000001"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.create_table("jade_price_series", sa.Column("series_key",sa.Text(),primary_key=True), sa.Column("last_attempt_on",sa.Date()), sa.Column("last_success_on",sa.Date()), sa.Column("unusable_reason",sa.Text()))
    op.create_table("jade_price_points", sa.Column("series_key",sa.Text(),sa.ForeignKey("jade_price_series.series_key",ondelete="CASCADE"),primary_key=True),sa.Column("trade_date",sa.Date(),primary_key=True),sa.Column("close",sa.Numeric(),nullable=False),sa.Column("raw_close",sa.Numeric()))

def downgrade() -> None:
    op.drop_table("jade_price_points")
    op.drop_table("jade_price_series")
