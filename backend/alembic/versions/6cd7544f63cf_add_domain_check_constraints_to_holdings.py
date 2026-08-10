"""add domain check constraints to holdings

Issue #25 (A-DEBT-1): pricing_mode/asset_type/currency were unconstrained
Text columns with only app-layer (Pydantic Literal) guarding correctness.
Adds a CHECK per column.

asset_class is in scope too even though issue #25 didn't name it — same
Text-column-with-a-closed-set-in-code shape, no reason to leave it out.

shares/avg_cost/current_value >= 0 is explicitly OUT of scope here: issue
#31/PR #111 (same day, earlier migration in this chain) encrypted those
columns to Fernet ciphertext, so a numeric CHECK is no longer possible at
the SQL level — see issue #113 and the Field(ge=0) validators added to
ParsedRow in app/schemas/holdings.py instead.

Values below are a FROZEN SNAPSHOT of VALID_PRICING_MODES/VALID_ASSET_TYPES/
VALID_CURRENCIES (app/schemas/holdings.py) and VALID_ASSET_CLASSES
(app/services/asset_class_config.py) as of this migration's authoring date —
deliberately NOT imported live (PR #114 review round 2 finding: an earlier
version of this migration imported the constants directly, so running
`alembic upgrade head` on a fresh database after those constants changed
would silently produce a DIFFERENT CHECK constraint than what an
already-migrated database has, even though both ran the identically-named
migration — migrations must be immutable historical snapshots, not
re-derived from whatever the current code state happens to be). Widening
any of these sets later is a NEW migration (ALTER the constraint), not an
edit to this file or the source constants alone.

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

# revision identifiers, used by Alembic.
revision: str = "6cd7544f63cf"
down_revision: Union[str, Sequence[str], None] = "379fdb627ee8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Frozen snapshot — see module docstring. Do not import live constants here.
_PRICING_MODES = ("auto", "manual")
_ASSET_TYPES = ("cash", "etf", "fund", "other", "stock", "wmf")
_CURRENCIES = (
    "AUD",
    "CAD",
    "CHF",
    "CNH",
    "CNY",
    "EUR",
    "GBP",
    "HKD",
    "JPY",
    "KRW",
    "MOP",
    "NZD",
    "SGD",
    "TWD",
    "USD",
)
_ASSET_CLASSES = (
    "BOND_FUND",
    "CASH_EQUIV",
    "COMMODITY",
    "ENERGY",
    "EQUITY_BROAD",
    "EQUITY_CN",
    "EQUITY_DM",
    "EQUITY_EM",
    "EQUITY_US_BROAD",
    "EQUITY_US_TECH",
    "PRECIOUS_METALS",
    "REIT",
    "STOCK",
)

_CONSTRAINTS = (
    ("pricing_mode", _PRICING_MODES),
    ("asset_type", _ASSET_TYPES),
    ("currency", _CURRENCIES),
    ("asset_class", _ASSET_CLASSES),
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
