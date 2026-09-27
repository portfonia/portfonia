"""Add credit balances and append-only ledger.

Revision ID: d4b8c6e91a20
Revises: ecb653d8cd13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "d4b8c6e91a20"
down_revision: str | None = "ecb653d8cd13"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("credit_cash_balance", sa.Numeric(12, 2), nullable=False, server_default=sa.text("0")))
    op.add_column("users", sa.Column("credit_gift_balance", sa.Numeric(12, 2), nullable=False, server_default=sa.text("0")))
    op.create_check_constraint("credit_cash_balance", "users", "credit_cash_balance >= 0")
    op.create_check_constraint("credit_gift_balance", "users", "credit_gift_balance >= 0")
    op.create_table(
        "credit_ledger",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("bucket", sa.Text(), nullable=False),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("balance_after", sa.Numeric(12, 2), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor_type", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.Text()),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("note", sa.Text()),
        sa.Column("reference", sa.Text()),
        sa.Column("user_deleted_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("bucket IN ('cash', 'gift')", name="bucket"),
        sa.CheckConstraint("amount <> 0", name="amount_nonzero"),
        sa.CheckConstraint("balance_after >= 0", name="balance_after"),
        sa.CheckConstraint("reason IN ('recharge', 'invite_rebate', 'signup_grant', 'admin_adjustment', 'subscription', 'qa')", name="reason"),
        sa.CheckConstraint("actor_type IN ('system', 'admin')", name="actor_type"),
        sa.UniqueConstraint("idempotency_key", "bucket"),
    )
    op.create_index("ix_credit_ledger_user_id_id", "credit_ledger", ["user_id", "id"])


def downgrade() -> None:
    op.drop_index("ix_credit_ledger_user_id_id", table_name="credit_ledger")
    op.drop_table("credit_ledger")
    op.drop_constraint("credit_gift_balance", "users", type_="check")
    op.drop_constraint("credit_cash_balance", "users", type_="check")
    op.drop_column("users", "credit_gift_balance")
    op.drop_column("users", "credit_cash_balance")
