"""Track the one-time Jade free upgrade and discounted Jade periods (#816).

Revision ID: j81600000001
Revises: d81200000001
"""

import sqlalchemy as sa

from alembic import op

revision = "j81600000001"
down_revision = "d81200000001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("jade_free_upgrade_used", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "users",
        sa.Column("jade_discount_periods_used", sa.SmallInteger(), nullable=False, server_default=sa.text("0")),
    )
    op.create_check_constraint(
        op.f("ck_users_jade_discount_periods_used"), "users", "jade_discount_periods_used BETWEEN 0 AND 3"
    )
    # Anyone who already subscribed to Jade is ineligible for the free period.
    op.execute(
        "UPDATE users SET jade_free_upgrade_used = true WHERE subscription_type = 'jade' "
        "OR id IN (SELECT user_id FROM credit_ledger WHERE reason = 'subscription' AND reference = 'jade')"
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_users_jade_discount_periods_used"), "users", type_="check")
    op.drop_column("users", "jade_discount_periods_used")
    op.drop_column("users", "jade_free_upgrade_used")
