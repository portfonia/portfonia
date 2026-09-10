"""add portfolio_snapshot_outbox

Issue #373 Scenario 2: durable outbox of the intended per-(user, day) capture
payload, so a day that was computed but never applied to
`portfolio_value_snapshots` + `portfolio_snapshot_batches` can be replayed
instead of recomputed from current holdings.

`user_id` is ON DELETE CASCADE — derived capture evidence, not an audited
financial record that should block purge (c1d2e3f4a5b6 / f5a6b7c8d9e0
precedent).

`payload` is plain `text` at the SQL level; the Fernet encryption is applied
by the app-side `EncryptedString` TypeDecorator over the JSON payload, same
mechanism the live row's `ticker`/`broker`/`account` columns use.

Revision ID: 9d2f4b7c1e05
Revises: f5a6b7c8d9e0
Create Date: 2026-09-10

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "9d2f4b7c1e05"
down_revision: str | Sequence[str] | None = "f5a6b7c8d9e0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "portfolio_snapshot_outbox",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("snapshot_date", sa.Date(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'computed'")),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("checksum", sa.Text(), nullable=False),
        sa.Column(
            "computed_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("applied_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "user_id", "snapshot_date", name="uq_portfolio_snapshot_outbox_user_date"
        ),
    )
    op.create_foreign_key(
        "fk_portfolio_snapshot_outbox_user_id_users",
        "portfolio_snapshot_outbox",
        "users",
        ["user_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "ix_portfolio_snapshot_outbox_status_date",
        "portfolio_snapshot_outbox",
        ["status", "snapshot_date"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_portfolio_snapshot_outbox_status_date", table_name="portfolio_snapshot_outbox"
    )
    op.drop_table("portfolio_snapshot_outbox")
