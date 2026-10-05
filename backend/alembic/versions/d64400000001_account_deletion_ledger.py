"""Allow voluntary user relinquishment at account deletion.

Downgrade fails if any row uses reason 'relinquish' or actor_type 'user'.
Retained accounting history must not be deleted to force a downgrade.

Revision ID: d64400000001
Revises: d64200000001
"""
from alembic import op

revision: str = "d64400000001"
down_revision: str | None = "d64200000001"
branch_labels: str | None = None
depends_on: str | None = None

OLD_REASONS = "'recharge', 'invite_rebate', 'signup_grant', 'admin_adjustment', 'subscription', 'subscription_return', 'qa', 'refund'"


def upgrade() -> None:
    op.drop_constraint(op.f("ck_credit_ledger_reason"), "credit_ledger", type_="check")
    op.create_check_constraint(op.f("ck_credit_ledger_reason"), "credit_ledger", f"reason IN ({OLD_REASONS}, 'relinquish')")
    op.drop_constraint(op.f("ck_credit_ledger_actor_type"), "credit_ledger", type_="check")
    op.create_check_constraint(op.f("ck_credit_ledger_actor_type"), "credit_ledger", "actor_type IN ('system', 'admin', 'user')")


def downgrade() -> None:
    op.drop_constraint(op.f("ck_credit_ledger_reason"), "credit_ledger", type_="check")
    op.create_check_constraint(op.f("ck_credit_ledger_reason"), "credit_ledger", f"reason IN ({OLD_REASONS})")
    op.drop_constraint(op.f("ck_credit_ledger_actor_type"), "credit_ledger", type_="check")
    op.create_check_constraint(op.f("ck_credit_ledger_actor_type"), "credit_ledger", "actor_type IN ('system', 'admin')")
