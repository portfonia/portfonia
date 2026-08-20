"""add cross_name_intel table

Issue #128 quality gate (Ring 1 stage A, L3 day-level cross-identifier
synthesis — design doc Hermes/Portfonia/Docs/Ring 1-A design.md §6.7 item 1).
Pure new-table addition, no existing data to backfill.

One LLM inference per (trade_date, prompt_version, input_fingerprint), shared
across every user whose report runs that day, expressing the one thing L1 (per
identifier) and L2 (per event) structurally cannot: which identifiers moved
together today for one mechanism.

`clusters` is nullable with the same convention `ticker_intel.analysis` and
`macro_event_intel.analysis` use — a NULL row is an "attempted, no usable
result" marker (LLM failure, unparseable JSON, or a compliance-scan block) so a
failing day is not re-attempted once per user in the fan-out; `attempt_count`
bounds that per issue #160.

`input_fingerprint` is part of the unique key so a later user's newly-written
L1 rows produce a fresh synthesis instead of reading one that structurally
cannot mention any of their names. It is a hash over `ticker_intel`
identifiers, which carry no user_id — the key stays global.

Revision ID: d6e7f8a9b0c1
Revises: c5d6e7f8a9b0
Create Date: 2026-08-20 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "d6e7f8a9b0c1"
down_revision: str | Sequence[str] | None = "c5d6e7f8a9b0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "cross_name_intel",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
        ),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("prompt_version", sa.Text(), nullable=False),
        sa.Column("input_fingerprint", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("clusters", postgresql.JSONB(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("facts", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_unique_constraint(
        "uq_cross_name_intel_date_version_fingerprint",
        "cross_name_intel",
        ["trade_date", "prompt_version", "input_fingerprint"],
    )
    op.create_index("ix_cross_name_intel_trade_date", "cross_name_intel", ["trade_date"])


def downgrade() -> None:
    op.drop_table("cross_name_intel")
