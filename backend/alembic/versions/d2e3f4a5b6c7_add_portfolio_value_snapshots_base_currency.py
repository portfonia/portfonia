"""add portfolio_value_snapshots.base_currency

Issue #367 review finding A (blacktomb42, review 5563537095): the reader
(`portfolio_performance.py`) assumed every row for a user was denominated
in that user's CURRENT `users.base_currency` preference, but the writer
(`write_user_snapshot`) actually used whatever preference was live AT
CAPTURE TIME for each day. A user changing `PATCH /me/report-currency`
between two capture days made two numerically different `market_value_base`
values look like a real market move (or vice versa) — a real unit
mismatch, not the already-accepted small FX-conversion-timing
approximation documented in the mechanism doc.

This column records each row's own capture-time currency. Existing rows
predate this fix and have no historical preference-change log to recover
from — backfilled here from the user's CURRENT `users.base_currency` as
the best available approximation (every production row so far was written
under a single, never-yet-changed preference per the #367 review's own
finding: this is a previously-undetected latent defect, not one already
manifesting in current data). Going forward every new row is tagged
correctly at write time.

Revision ID: d2e3f4a5b6c7
Revises: c1d2e3f4a5b6
Create Date: 2026-09-08

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d2e3f4a5b6c7"
down_revision: str | Sequence[str] | None = "c1d2e3f4a5b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("portfolio_value_snapshots", sa.Column("base_currency", sa.Text(), nullable=True))
    op.execute(
        """
        UPDATE portfolio_value_snapshots pvs
        SET base_currency = u.base_currency
        FROM users u
        WHERE pvs.user_id = u.id AND pvs.base_currency IS NULL
        """
    )
    # A row whose user has since been purged (FK is ON DELETE CASCADE, so
    # this should be unreachable) has no join match above — fall back to
    # USD rather than leave a NULL for the NOT NULL constraint below.
    op.execute(
        "UPDATE portfolio_value_snapshots SET base_currency = 'USD' WHERE base_currency IS NULL"
    )
    op.alter_column("portfolio_value_snapshots", "base_currency", nullable=False)


def downgrade() -> None:
    op.drop_column("portfolio_value_snapshots", "base_currency")
