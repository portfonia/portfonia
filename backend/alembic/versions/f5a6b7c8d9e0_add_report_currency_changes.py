"""add report_currency_changes

Issue #372 slice A: append-only audit of users.base_currency preference
changes (self-service PATCH /me/report-currency and ops
POST /admin/users/by-email/report-currency). Does not rewrite
portfolio_value_snapshots.base_currency.

`user_id` is ON DELETE CASCADE — preference audit, not a financial record
that should block purge (c1d2e3f4a5b6 snapshot precedent).
`actor_user_id` is ON DELETE SET NULL.

Currency and source CHECKs are FROZEN SNAPSHOTS of VALID_CURRENCIES and
VALID_REPORT_CURRENCY_CHANGE_SOURCES as of this migration's authoring
date — deliberately NOT imported live (f3a4b5c6d7e8 / e3f4a5b6c7d8
precedent). Widening later is a NEW migration.

Revision ID: f5a6b7c8d9e0
Revises: e3f4a5b6c7d8
Create Date: 2026-09-09

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "f5a6b7c8d9e0"
down_revision: str | Sequence[str] | None = "e3f4a5b6c7d8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen snapshot — see module docstring. Do not import live constants here.
_CURRENCIES = (
    "USD",
    "CNY",
    "CNH",
    "HKD",
    "GBP",
    "EUR",
    "JPY",
    "SGD",
    "AUD",
    "CAD",
    "CHF",
    "KRW",
    "TWD",
    "MOP",
    "NZD",
)
_SOURCES = ("admin", "self")


def _in_list_sql(column: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"


def upgrade() -> None:
    op.create_table(
        "report_currency_changes",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("old_currency", sa.Text(), nullable=False),
        sa.Column("new_currency", sa.Text(), nullable=False),
        sa.Column(
            "changed_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("clock_timestamp()"),
        ),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(_in_list_sql("old_currency", _CURRENCIES), name="old_currency"),
        sa.CheckConstraint(_in_list_sql("new_currency", _CURRENCIES), name="new_currency"),
        sa.CheckConstraint(_in_list_sql("source", _SOURCES), name="source"),
    )
    op.create_foreign_key(
        "fk_report_currency_changes_user_id_users",
        "report_currency_changes",
        "users",
        ["user_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_report_currency_changes_actor_user_id_users",
        "report_currency_changes",
        "users",
        ["actor_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_report_currency_changes_user_id_changed_at",
        "report_currency_changes",
        ["user_id", "changed_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_report_currency_changes_user_id_changed_at",
        table_name="report_currency_changes",
    )
    op.drop_table("report_currency_changes")
