"""Add referral attribution and allow cash debt from referral clawbacks.

Downgrade fails while any cash balance or cash ledger balance_after is negative,
or retained ledger rows use the new reasons. Do not erase accounting history.

Revision ID: d67500000001
Revises: d64000000001
"""
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from alembic import op
from app.core.config import get_settings

revision: str = 'd67500000001'
down_revision: str | None = 'd64000000001'
branch_labels: str | None = None
depends_on: str | None = None
OLD_REASONS = "'recharge', 'invite_rebate', 'signup_grant', 'admin_adjustment', 'subscription', 'subscription_return', 'qa', 'refund', 'relinquish'"


def upgrade() -> None:
    op.add_column('waitlist_entries', sa.Column('source', sa.Text(), nullable=False, server_default=sa.text("'organic'")))
    op.add_column('waitlist_entries', sa.Column('referrer_user_id', PGUUID(as_uuid=True), nullable=True))
    op.create_check_constraint(op.f('ck_waitlist_entries_source'), 'waitlist_entries', "source IN ('organic','referral')")
    op.add_column('users', sa.Column('grand_invited_by', PGUUID(as_uuid=True), nullable=True))
    op.execute(sa.text('UPDATE users SET invited_by=:root,grand_invited_by=:root').bindparams(root=UUID(get_settings().ADMIN_ID)))
    op.drop_constraint(op.f('ck_users_credit_cash_balance'), 'users', type_='check')
    op.drop_constraint(op.f('ck_credit_ledger_balance_after'), 'credit_ledger', type_='check')
    op.create_check_constraint(op.f('ck_credit_ledger_balance_after'), 'credit_ledger', "bucket = 'cash' OR balance_after >= 0")
    op.drop_constraint(op.f('ck_credit_ledger_reason'), 'credit_ledger', type_='check')
    op.create_check_constraint(op.f('ck_credit_ledger_reason'), 'credit_ledger', f"reason IN ({OLD_REASONS}, 'referral_bonus', 'referral_clawback')")


def downgrade() -> None:
    op.create_check_constraint(op.f('ck_users_credit_cash_balance'), 'users', 'credit_cash_balance >= 0')
    op.drop_constraint(op.f('ck_credit_ledger_balance_after'), 'credit_ledger', type_='check')
    op.create_check_constraint(op.f('ck_credit_ledger_balance_after'), 'credit_ledger', 'balance_after >= 0')
    op.drop_constraint(op.f('ck_credit_ledger_reason'), 'credit_ledger', type_='check')
    op.create_check_constraint(op.f('ck_credit_ledger_reason'), 'credit_ledger', f'reason IN ({OLD_REASONS})')
    op.drop_column('users', 'grand_invited_by')
    op.drop_constraint(op.f('ck_waitlist_entries_source'), 'waitlist_entries', type_='check')
    op.drop_column('waitlist_entries', 'referrer_user_id')
    op.drop_column('waitlist_entries', 'source')
