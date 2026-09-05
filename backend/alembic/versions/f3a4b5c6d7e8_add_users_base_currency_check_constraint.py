"""add users.base_currency check constraint

Issue #350 item 1: users.base_currency was a free Text column with no
CheckConstraint — same gap users.locale had before a2b3c4d5e6f7. Adds a
CHECK enumerating the full VALID_CURRENCIES set (app/schemas/holdings.py,
15 entries) rather than a narrower subset: unlike report language (only
en/zh have i18n_glossary.yml coverage), currency has no such content-
coverage gap — every VALID_CURRENCIES entry is already fully supported by
fx_fetcher.py's FX-pair coverage (issue #204/#253).

Values below are a FROZEN SNAPSHOT of VALID_CURRENCIES as of this
migration's authoring date — deliberately NOT imported live, matching
a2b3c4d5e6f7 and 6cd7544f63cf's precedent: a migration must be an
immutable historical record. Widening this set later is a NEW migration,
not an edit to this file.

No data backfill needed: every existing row already has base_currency =
'USD' (hardcoded at signup before this issue), which is inside this
constraint.

Revision ID: f3a4b5c6d7e8
Revises: 6c1e9826acbf
Create Date: 2026-09-04 00:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f3a4b5c6d7e8"
down_revision: str | Sequence[str] | None = "6c1e9826acbf"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen snapshot — see module docstring. Do not import live constants here.
_CURRENCIES = (
    "USD",
    "CNY",
    "CNH",
    "HKD",
    "GBP",
    "EUR",
    "JPY",
    "SGD",
    "AUD",
    "CAD",
    "CHF",
    "KRW",
    "TWD",
    "MOP",
    "NZD",
)


def _in_list_sql(column: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"


def upgrade() -> None:
    # Bare column name (not "ck_users_base_currency") — target_metadata's
    # naming_convention (app/models/base.py) applies the ck_%(table_name)s_
    # %(constraint_name)s prefix; a pre-rendered name doubles it (same
    # gotcha documented in 6cd7544f63cf, e1f2a3b4c5d6, and a2b3c4d5e6f7).
    op.create_check_constraint("base_currency", "users", _in_list_sql("base_currency", _CURRENCIES))


def downgrade() -> None:
    op.drop_constraint("base_currency", "users", type_="check")
