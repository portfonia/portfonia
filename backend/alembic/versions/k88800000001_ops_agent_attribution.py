"""Attribute Ops writes to named agents (#888)."""
from alembic import op
import sqlalchemy as sa

revision = "k88800000001"
down_revision = "k88600000001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("report_currency_changes", "invites", "ticker_leverage_overrides"):
        op.add_column(table, sa.Column("ops_agent", sa.Text(), nullable=True))


def downgrade() -> None:
    for table in ("ticker_leverage_overrides", "invites", "report_currency_changes"):
        op.drop_column(table, "ops_agent")
