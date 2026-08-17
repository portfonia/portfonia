"""add macro_event_intel table

Issue #128 A3 (Ring 1 stage A, L2 shared macro-event intel — design doc
Hermes/Portfonia/Docs/Ring 1-A design.md §5.4). Pure new-table addition, no
existing data to backfill.

One LLM inference per (event_key, trade_date, prompt_version), shared across
every user whose report touches that event that day instead of re-inferring
it once per user. `event_key` carries a source prefix (`theme:` for a
macro_detector ThemeHit, `fwd:` for a forward_events row). `analysis` is
nullable: a NULL row is an "attempted, no usable result" marker (LLM
failure, unparseable JSON, or a compliance-scan block), the same convention
`ticker_intel` uses so a failing event is not re-attempted by every user in
the fan-out.

Revision ID: b4c5d6e7f8a9
Revises: a3b4c5d6e7f8
Create Date: 2026-08-16 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "b4c5d6e7f8a9"
down_revision: str | Sequence[str] | None = "a3b4c5d6e7f8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "macro_event_intel",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
        ),
        sa.Column("event_key", sa.Text(), nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("prompt_version", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("analysis", sa.Text(), nullable=True),
        sa.Column("affected_asset_classes", postgresql.JSONB(), nullable=False),
        sa.Column("affected_sectors", postgresql.JSONB(), nullable=False),
        sa.Column("facts", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_unique_constraint(
        "uq_macro_event_intel_key_date_version",
        "macro_event_intel",
        ["event_key", "trade_date", "prompt_version"],
    )
    op.create_index("ix_macro_event_intel_event_key", "macro_event_intel", ["event_key"])
    op.create_index("ix_macro_event_intel_trade_date", "macro_event_intel", ["trade_date"])


def downgrade() -> None:
    op.drop_table("macro_event_intel")
