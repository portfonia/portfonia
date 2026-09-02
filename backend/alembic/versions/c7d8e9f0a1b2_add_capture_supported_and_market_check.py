"""add capture_supported and holdings.market check

Issue #311: Holding.market becomes a closed set (US/HK/A-Share/UK/Europe/
Japan/Korea plus Other as the explicit fallback) and a separate
capture_supported boolean marks rows whose ticker does not resolve into a
scheduled capture bucket. Other is a legitimate stored value — it is not a
rejection flag; capture / section 1 / Pass 2 key off capture_supported.

Values below are a FROZEN SNAPSHOT of VALID_HOLDING_MARKETS
(app/services/markets.py) as of this migration's authoring date —
deliberately NOT imported live, matching 6cd7544f63cf: a migration must be
an immutable historical record. Widening the set later is a NEW migration.

Existing rows: unknown free-text market values are coerced to Other before
the CHECK is applied. capture_supported defaults True; ticker is encrypted
so this migration cannot infer the flag from suffix. Parse/confirm set it
going forward. Cash/wmf rows stored as Other stay capture_supported=True
(manual pricing) via the default.

Revision ID: c7d8e9f0a1b2
Revises: a2b3c4d5e6f7
Create Date: 2026-09-02 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c7d8e9f0a1b2"
down_revision: Union[str, Sequence[str], None] = "a2b3c4d5e6f7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Frozen snapshot — see module docstring. Do not import live constants here.
_MARKETS = (
    "A-Share",
    "Europe",
    "HK",
    "Japan",
    "Korea",
    "Other",
    "UK",
    "US",
)


def _market_sql() -> str:
    quoted = ", ".join(f"'{v}'" for v in _MARKETS)
    return f"(market IS NULL) OR (market IN ({quoted}))"


def upgrade() -> None:
    op.add_column(
        "holdings",
        sa.Column(
            "capture_supported",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )
    op.execute(
        sa.text(
            "UPDATE holdings SET market = 'Other' "
            "WHERE market IS NOT NULL AND market NOT IN "
            "('A-Share', 'Europe', 'HK', 'Japan', 'Korea', 'Other', 'UK', 'US')"
        )
    )
    # Bare token "market" — Base naming_convention renders ck_holdings_market.
    op.create_check_constraint("market", "holdings", _market_sql())


def downgrade() -> None:
    op.drop_constraint("market", "holdings", type_="check")
    op.drop_column("holdings", "capture_supported")
