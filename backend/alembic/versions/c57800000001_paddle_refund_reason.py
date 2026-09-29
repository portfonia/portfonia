"""Allow refund reason in credit ledger.

Revision ID: c57800000001
Revises: b56900000001
"""

from alembic import op

revision = "c57800000001"
down_revision = "b56900000001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(op.f("ck_credit_ledger_reason"), "credit_ledger", type_="check")
    op.create_check_constraint(
        "reason", "credit_ledger",
        "reason IN ('recharge', 'invite_rebate', 'signup_grant', 'admin_adjustment', 'subscription', 'qa', 'refund')",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_credit_ledger_reason"), "credit_ledger", type_="check")
    op.create_check_constraint(
        "reason", "credit_ledger",
        "reason IN ('recharge', 'invite_rebate', 'signup_grant', 'admin_adjustment', 'subscription', 'qa')",
    )
