"""add domain check constraints to holdings

Issue #25 (A-DEBT-1): pricing_mode/asset_type/currency were unconstrained
Text columns with only app-layer (Pydantic Literal) guarding correctness.
Adds a CHECK per column, closed-set values pulled from the single sources
of truth already in the codebase (VALID_ASSET_CLASSES, VALID_CURRENCIES)
so this migration can't silently drift from the app-layer validation.

asset_class is in scope too even though issue #25 didn't name it — same
Text-column-with-a-closed-set-in-code shape, no reason to leave it out.

shares/avg_cost/current_value >= 0 is explicitly OUT of scope here: issue
#31/PR #111 (same day, earlier migration in this chain) encrypted those
columns to Fernet ciphertext, so a numeric CHECK is no longer possible at
the SQL level — see issue #113 and the Field(ge=0) validators added to
ParsedRow in app/schemas/holdings.py instead.

Audited existing dev rows before writing this (2026-08-09, portfonia_dev,
22 rows): pricing_mode in {auto, manual}; asset_type in {cash, etf, fund,
stock}; currency in {CNY, HKD, USD}; asset_class in {STOCK, EQUITY_US_BROAD,
EQUITY_US_TECH, EQUITY_CN, BOND_FUND, CASH_EQUIV, PRECIOUS_METALS} — all
within the new constraints. Production has not been audited; run the same
query there before this migration runs against it:

    SELECT 'pricing_mode', pricing_mode, count(*) FROM holdings GROUP BY pricing_mode
    UNION ALL SELECT 'asset_type', asset_type, count(*) FROM holdings GROUP BY asset_type
    UNION ALL SELECT 'currency', currency, count(*) FROM holdings GROUP BY currency
    UNION ALL SELECT 'asset_class', asset_class, count(*) FROM holdings GROUP BY asset_class;

A CHECK that fails against legacy data blocks this migration outright — if
production has a value outside these sets, resolve it (backfill or widen
the set) before deploying, not after.

Revision ID: 6cd7544f63cf
Revises: 379fdb627ee8
Create Date: 2026-08-09 04:46:21.320498

"""
from typing import Sequence, Union

from alembic import op

from app.schemas.holdings import VALID_CURRENCIES
from app.services.asset_class_config import VALID_ASSET_CLASSES

# revision identifiers, used by Alembic.
revision: str = "6cd7544f63cf"
down_revision: Union[str, Sequence[str], None] = "379fdb627ee8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# pricing_mode/asset_type aren't extracted into shared constants elsewhere
# in the codebase (small, stable 2- and 6-value sets) — kept in sync by
# hand with the Literal definitions in app/schemas/holdings.py's ParsedRow.
_PRICING_MODES = ("auto", "manual")
_ASSET_TYPES = ("stock", "etf", "fund", "cash", "wmf", "other")

_CONSTRAINTS = (
    ("pricing_mode", _PRICING_MODES),
    ("asset_type", _ASSET_TYPES),
    ("currency", tuple(sorted(VALID_CURRENCIES))),
    ("asset_class", tuple(sorted(VALID_ASSET_CLASSES))),
)


def _in_list_sql(column: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"


def upgrade() -> None:
    # target_metadata's naming_convention (app/models/base.py, "ck":
    # "ck_%(table_name)s_%(constraint_name)s") applies to the anonymous
    # table op.create_check_constraint builds internally — passing the
    # bare column name here (not "ck_holdings_<column>") is what actually
    # lands as ck_holdings_<column> in the DB; a pre-rendered name doubles
    # the prefix (verified against a real run: produced
    # ck_holdings_ck_holdings_pricing_mode before this fix).
    for column, values in _CONSTRAINTS:
        op.create_check_constraint(column, "holdings", _in_list_sql(column, values))


def downgrade() -> None:
    # Same bare-token naming_convention behavior as create (see upgrade()) —
    # op.drop_constraint also renders through ck_%(table_name)s_%(constraint_name)s,
    # so passing the already-rendered "ck_holdings_<column>" here doubles the
    # prefix too (verified against a real run — same failure mode as create).
    for column, _values in _CONSTRAINTS:
        op.drop_constraint(column, "holdings", type_="check")
