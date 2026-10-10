"""Per-window Jade historical stress data.

Revision ID: d72000000001
Revises: d71400000001
"""
from alembic import op
import sqlalchemy as sa

revision = "d72000000001"
down_revision = "d71400000001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "jade_scenario_series",
        sa.Column("scenario_id", sa.Text(), primary_key=True),
        sa.Column("series_key", sa.Text(), primary_key=True),
        sa.Column("last_attempt_on", sa.Date()),
        sa.Column("last_success_on", sa.Date()),
        sa.Column("unusable_reason", sa.Text()),
    )
    op.create_table(
        "jade_scenario_points",
        sa.Column("scenario_id", sa.Text(), primary_key=True),
        sa.Column("series_key", sa.Text(), primary_key=True),
        sa.Column("trade_date", sa.Date(), primary_key=True),
        sa.Column("close", sa.Numeric(), nullable=False),
        sa.Column("raw_close", sa.Numeric()),
        sa.ForeignKeyConstraint(["scenario_id", "series_key"], ["jade_scenario_series.scenario_id", "jade_scenario_series.series_key"], ondelete="CASCADE"),
    )


def downgrade() -> None:
    op.drop_table("jade_scenario_points")
    op.drop_table("jade_scenario_series")
