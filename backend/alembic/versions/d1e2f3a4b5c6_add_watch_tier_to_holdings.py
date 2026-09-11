"""add watch_tier to holdings

Issue #421: a user can mark any holding with one of three watch tiers
(watch/focus/critical), independent of its actual position size, so a
zero/near-zero-value holding they're tracking for a reason other than a
real position (a thesis, a prospective buy, a competitor) can still be
analyzable in §3 — see report_generator.py's _build_holding_check_inputs,
which substitutes a config-driven target weight (watch_tier_weights.yml)
for this holding's real (near-zero) weight when watch_tier is set.

NULL = not watched (default, no backfill needed — every existing row is
implicitly unwatched). watch_tier never feeds real portfolio math (§1
distribution, by_asset_class/by_broker/by_currency, P&L%, §4.1
concentration) — those stay driven purely by actual position value
regardless of this column, by construction (portfolio_calculator.py never
reads it).

Follows the `market` CHECK's "(col IS NULL) OR IN (...)" shape (migration
c7d8e9f0a1b2), not `capture_supported`'s boolean pattern — one tiered
field, not a plain flag (issue #421 Design item 1, superseding the
issue's own first-draft sketch).

Values below are a FROZEN SNAPSHOT of VALID_WATCH_TIERS
(app/schemas/holdings.py's `WatchTier` Literal) as of this migration's
authoring date — deliberately NOT imported live, matching 6cd7544f63cf/
c7d8e9f0a1b2: a migration must be an immutable historical record.
Widening the set later is a NEW migration.

Revision ID: d1e2f3a4b5c6
Revises: c4d5e6f7a8b9
Create Date: 2026-09-10 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d1e2f3a4b5c6"
down_revision: Union[str, Sequence[str], None] = "c4d5e6f7a8b9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Frozen snapshot — see module docstring. Do not import live constants here.
_WATCH_TIERS = ("critical", "focus", "watch")


def _watch_tier_sql() -> str:
    quoted = ", ".join(f"'{v}'" for v in _WATCH_TIERS)
    return f"(watch_tier IS NULL) OR (watch_tier IN ({quoted}))"


def upgrade() -> None:
    op.add_column("holdings", sa.Column("watch_tier", sa.String(), nullable=True))
    # Bare token "watch_tier" — Base naming_convention renders ck_holdings_watch_tier.
    op.create_check_constraint("watch_tier", "holdings", _watch_tier_sql())


def downgrade() -> None:
    op.drop_constraint("watch_tier", "holdings", type_="check")
    op.drop_column("holdings", "watch_tier")
