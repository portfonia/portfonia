"""Add subscription core, preserving existing users' cadence.

Revision ID: s59500000001
Revises: d58200000001
"""
import sqlalchemy as sa

from alembic import op

revision: str = "s59500000001"
down_revision: str | None = "d58200000001"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("subscription_status", sa.Text(), nullable=False, server_default="inactive"))
    op.add_column("users", sa.Column("subscription_type", sa.Text(), nullable=True))
    op.add_column("users", sa.Column("subscription_expires_on", sa.Date(), nullable=True))
    op.add_column("users", sa.Column("subscription_period_start", sa.Date(), nullable=True))
    op.add_column("users", sa.Column("subscription_anchor_day", sa.SmallInteger(), nullable=True))
    op.add_column("users", sa.Column("subscription_cancel_pending", sa.Boolean(), nullable=False, server_default=sa.text("false")))
    op.add_column("users", sa.Column("subscription_adjusted_on", sa.Date(), nullable=True))
    op.create_check_constraint(op.f("ck_users_subscription_status"), "users", "subscription_status IN ('active', 'cancelled', 'expired', 'inactive')")
    op.create_check_constraint(op.f("ck_users_subscription_type"), "users", "subscription_type IS NULL OR subscription_type IN ('mwf', 'weekly')")
    op.create_check_constraint(op.f("ck_users_subscription_anchor_day"), "users", "subscription_anchor_day BETWEEN 1 AND 31")
    op.drop_constraint(op.f("ck_users_report_cadence"), "users", type_="check")
    op.create_check_constraint(op.f("ck_users_report_cadence"), "users", "report_cadence IN ('mwf', 'none', 'weekly')")
    op.drop_constraint(op.f("ck_credit_ledger_reason"), "credit_ledger", type_="check")
    op.create_check_constraint(op.f("ck_credit_ledger_reason"), "credit_ledger", "reason IN ('recharge', 'invite_rebate', 'signup_grant', 'admin_adjustment', 'subscription', 'qa', 'refund', 'subscription_return')")


def downgrade() -> None:
    op.drop_constraint(op.f("ck_credit_ledger_reason"), "credit_ledger", type_="check")
    op.create_check_constraint(op.f("ck_credit_ledger_reason"), "credit_ledger", "reason IN ('recharge', 'invite_rebate', 'signup_grant', 'admin_adjustment', 'subscription', 'qa', 'refund')")
    op.drop_constraint(op.f("ck_users_report_cadence"), "users", type_="check")
    op.create_check_constraint(op.f("ck_users_report_cadence"), "users", "report_cadence IN ('mwf', 'weekly')")
    for name in ("subscription_anchor_day", "subscription_type", "subscription_status"):
        op.drop_constraint(op.f("ck_users_" + name), "users", type_="check")
    for name in ("subscription_adjusted_on", "subscription_cancel_pending", "subscription_anchor_day", "subscription_period_start", "subscription_expires_on", "subscription_type", "subscription_status"):
        op.drop_column("users", name)
