"""add news_surfaced table

Issue #30 (H-DEBT-3): `load_news_window` selected by
`published_at > start, <= end` — a news item published inside a window but
not ingested until after that window's period_end fell through BOTH the
window it belongs to (not yet ingested when selected) and the next one
(excluded as "before this window's start"), a permanent miss. Window
selection now decouples from the watermark boundary entirely
(`published_at <= end`, no lower bound); this table is the dedup ledger that
takes over the job the lower bound used to do — a news item is excluded from
every future window once it has appeared in a report that reached
success/needs_review/skipped.

Revision ID: f1a2b3c4d5e6
Revises: ed3e81d6cccb
Create Date: 2026-08-13 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f1a2b3c4d5e6"
down_revision: Union[str, Sequence[str], None] = "ed3e81d6cccb"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "news_surfaced",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
        ),
        sa.Column("news_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("report_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "surfaced_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    # Unique on news_id: only the first surfacing matters, and it's the
    # ON CONFLICT DO NOTHING target for idempotent marking against Celery
    # redelivery (task_acks_late).
    op.create_unique_constraint("uq_news_surfaced_news_id", "news_surfaced", ["news_id"])
    op.create_index("ix_news_surfaced_report_id", "news_surfaced", ["report_id"])


def downgrade() -> None:
    op.drop_index("ix_news_surfaced_report_id", table_name="news_surfaced")
    op.drop_constraint("uq_news_surfaced_news_id", "news_surfaced", type_="unique")
    op.drop_table("news_surfaced")
