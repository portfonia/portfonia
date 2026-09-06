"""add portfolio_value_snapshots, portfolio_snapshot_batches, benchmark_prices

Issue #360 Phase 1 (backend only, no UI) — data model for the Portfolio
Performance chart. Three new, independent tables; no change to any existing
table.

- `portfolio_value_snapshots`: one row per holding per user per day,
  denormalized (no FK to the live `holdings` row — see the model's module
  docstring). `user_id` is `ON DELETE CASCADE`, not `RESTRICT` like
  holdings/reports/accounts (issue #129 B7) — this is derived historical
  time-series data, not an audited financial record, so a user purge must
  not be blocked by it and needs no new step in
  `app/services/user_purge.py`.
- `portfolio_snapshot_batches`: per-(user, day) completeness marker so the
  read API never exposes a day whose price/FX dependencies were incomplete
  when the daily snapshot task ran.
- `benchmark_prices`: daily close for sp500/dow30/nasdaq (Nasdaq Composite,
  D9), unrelated to any user's holdings.

Revision ID: c1d2e3f4a5b6
Revises: f3a4b5c6d7e8
Create Date: 2026-09-05

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "c1d2e3f4a5b6"
down_revision: str | Sequence[str] | None = "f3a4b5c6d7e8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "portfolio_value_snapshots",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("snapshot_date", sa.Date(), nullable=False),
        sa.Column("holding_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("ticker", sa.Text(), nullable=True),
        sa.Column("fund_code", sa.Text(), nullable=True),
        sa.Column("market", sa.Text(), nullable=True),
        sa.Column("broker", sa.Text(), nullable=True),
        sa.Column("account", sa.Text(), nullable=True),
        sa.Column("portfolio", sa.Text(), nullable=True),
        sa.Column("asset_class", sa.Text(), nullable=True),
        sa.Column("pricing_mode", sa.Text(), nullable=True),
        sa.Column(
            "capture_supported", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column("currency", sa.Text(), nullable=False),
        sa.Column("shares", sa.Text(), nullable=True),
        sa.Column("current_value", sa.Text(), nullable=True),
        sa.Column("market_value", sa.Numeric(), nullable=True),
        sa.Column("market_value_base", sa.Numeric(), nullable=True),
        sa.Column("cost_basis_base", sa.Numeric(), nullable=True),
        sa.Column("fx_rate_used", sa.Numeric(), nullable=True),
        sa.Column("price_as_of", sa.Date(), nullable=True),
        sa.Column("fx_as_of", sa.Date(), nullable=True),
        sa.Column(
            "is_backfilled", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column(
            "is_fx_fallback", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("data_quality", sa.Text(), nullable=False, server_default=sa.text("'ok'")),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_foreign_key(
        "fk_portfolio_value_snapshots_user_id_users",
        "portfolio_value_snapshots",
        "users",
        ["user_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_unique_constraint(
        "uq_portfolio_value_snapshots_holding",
        "portfolio_value_snapshots",
        ["user_id", "snapshot_date", "holding_id"],
    )
    op.create_index(
        "ix_portfolio_value_snapshots_user_id_snapshot_date",
        "portfolio_value_snapshots",
        ["user_id", "snapshot_date"],
    )

    op.create_table(
        "portfolio_snapshot_batches",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("snapshot_date", sa.Date(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'pending'")),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_foreign_key(
        "fk_portfolio_snapshot_batches_user_id_users",
        "portfolio_snapshot_batches",
        "users",
        ["user_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_unique_constraint(
        "uq_portfolio_snapshot_batches_user_date",
        "portfolio_snapshot_batches",
        ["user_id", "snapshot_date"],
    )
    op.create_check_constraint(
        "status",
        "portfolio_snapshot_batches",
        "status IN ('pending', 'complete', 'skipped_deps')",
    )

    op.create_table(
        "benchmark_prices",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("index_code", sa.Text(), nullable=False),
        sa.Column("price_date", sa.Date(), nullable=False),
        sa.Column("close_price", sa.Numeric(), nullable=False),
        sa.Column("currency", sa.Text(), nullable=False, server_default=sa.text("'USD'")),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_unique_constraint(
        "uq_benchmark_prices_index_date", "benchmark_prices", ["index_code", "price_date"]
    )
    op.create_check_constraint(
        "index_code", "benchmark_prices", "index_code IN ('dow30', 'nasdaq', 'sp500')"
    )


def downgrade() -> None:
    op.drop_table("benchmark_prices")
    op.drop_constraint(
        "fk_portfolio_snapshot_batches_user_id_users",
        "portfolio_snapshot_batches",
        type_="foreignkey",
    )
    op.drop_table("portfolio_snapshot_batches")
    op.drop_constraint(
        "fk_portfolio_value_snapshots_user_id_users",
        "portfolio_value_snapshots",
        type_="foreignkey",
    )
    op.drop_table("portfolio_value_snapshots")
