"""Allow the Daily subscription and report cadence.

Revision ID: d64100000001
Revises: d62200000001
"""
from alembic import op

revision: str = "d64100000001"
down_revision: str | None = "d62200000001"
branch_labels: str | None = None
depends_on: str | None = None


def _checks(cadences: str, plans: str) -> None:
    op.drop_constraint(op.f("ck_users_report_cadence"), "users", type_="check")
    op.create_check_constraint(op.f("ck_users_report_cadence"), "users", f"report_cadence IN ({cadences})")
    op.drop_constraint(op.f("ck_users_subscription_type"), "users", type_="check")
    op.create_check_constraint(op.f("ck_users_subscription_type"), "users", f"subscription_type IS NULL OR subscription_type IN ({plans})")


def upgrade() -> None:
    _checks("'daily', 'mwf', 'none', 'weekly'", "'daily', 'mwf', 'weekly'")


def downgrade() -> None:
    # Existing Daily rows deliberately prevent downgrade.
    _checks("'mwf', 'none', 'weekly'", "'mwf', 'weekly'")
