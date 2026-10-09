"""Allow the Jade subscription type without changing data."""
from alembic import op
revision: str = "d71000000001"
down_revision: str | None = "d68100000001"
branch_labels: str | None = None
depends_on: str | None = None


def _check(plans: str) -> None:
    op.drop_constraint(op.f("ck_users_subscription_type"), "users", type_="check")
    op.create_check_constraint(op.f("ck_users_subscription_type"), "users", f"subscription_type IS NULL OR subscription_type IN ({plans})")


def upgrade() -> None:
    _check("'daily', 'jade', 'mwf', 'weekly'")


def downgrade() -> None:
    # Existing Jade rows deliberately prevent downgrade.
    _check("'daily', 'mwf', 'weekly'")
