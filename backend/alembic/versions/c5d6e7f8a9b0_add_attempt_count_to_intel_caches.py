"""add attempt_count to ticker_intel and macro_event_intel

Issue #160. Both L1 (`ticker_intel`) and L2 (`macro_event_intel`) wrote a
null-analysis marker row on EVERY failure, which makes that key final for the
rest of the trade_date. That is correct for a failure an identical call
reproduces (bad key, malformed request) and for a compliance block, but wrong
for a transient one: a single connection reset during the first user's report
starved every later user in the same fan-out — and every manual re-run that
day — of that key's intel.

`attempt_count` is how many times the system as a whole has attempted the key
today. A marker is final only at `_MAX_ATTEMPTS_PER_KEY` (3); a retryable
failure (`llm_errors.is_retryable`) leaves budget for a later caller, while a
non-retryable one or a compliance block writes the cap value directly and
locks the key on the spot. One integer expresses both states, so there is no
second "permanent" flag column to drift out of sync with it.

Backfill: `server_default='1'` is exactly right for existing rows — every row
that exists was written by one attempt, successful or not. No data migration
beyond that default is needed, and no row is re-openable retroactively (a
pre-existing marker at 1 attempt becomes retryable, which is the intended new
behavior, not a defect).

Revision ID: c5d6e7f8a9b0
Revises: b4c5d6e7f8a9
Create Date: 2026-08-17 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c5d6e7f8a9b0"
down_revision: str | Sequence[str] | None = "b4c5d6e7f8a9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = ("ticker_intel", "macro_event_intel")


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(
            table,
            sa.Column(
                "attempt_count",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("1"),
            ),
        )


def downgrade() -> None:
    for table in _TABLES:
        op.drop_column(table, "attempt_count")
