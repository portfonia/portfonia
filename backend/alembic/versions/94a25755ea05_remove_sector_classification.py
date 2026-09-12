"""remove GICS sector classification (issue #435)

Drops `holdings.sector` and `macro_event_intel.affected_sectors`. Both are
derived/regenerable classification data, not user-entered — no data
migration needed. `sector_taxonomy.VALID_SECTORS` (used by the unrelated
`sectors_of_interest` questionnaire field) is unaffected; only the
yfinance-derived GICS classification and its consumers are removed.

Revision ID: 94a25755ea05
Revises: d1e2f3a4b5c6
Create Date: 2026-09-11 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "94a25755ea05"
down_revision: Union[str, Sequence[str], None] = "d1e2f3a4b5c6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column("holdings", "sector")
    op.drop_column("macro_event_intel", "affected_sectors")


def downgrade() -> None:
    op.add_column(
        "macro_event_intel",
        sa.Column(
            "affected_sectors",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
    )
    op.add_column("holdings", sa.Column("sector", sa.Text(), nullable=True))
