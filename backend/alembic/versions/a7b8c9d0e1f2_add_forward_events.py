"""add forward_events table

Forward-calendar layer (#1): US macro release dates (FRED) + FOMC + held-company
earnings dates. Idempotent on (event_type, name, ticker, scheduled_date); ticker
defaults to '' (not NULL) so the unique key dedups macro rows.

Revision ID: a7b8c9d0e1f2
Revises: f6a7b8c9d0e1
Create Date: 2026-06-09 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a7b8c9d0e1f2"
down_revision: Union[str, Sequence[str], None] = "f6a7b8c9d0e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "forward_events",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
        ),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("ticker", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("scheduled_date", sa.Date(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column(
            "captured_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "event_type", "name", "ticker", "scheduled_date", name="uq_forward_events_key"
        ),
    )
    op.create_index(
        "ix_forward_events_scheduled_date", "forward_events", ["scheduled_date"]
    )


def downgrade() -> None:
    op.drop_index("ix_forward_events_scheduled_date", table_name="forward_events")
    op.drop_table("forward_events")
