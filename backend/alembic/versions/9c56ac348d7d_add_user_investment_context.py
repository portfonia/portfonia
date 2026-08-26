"""add user_investment_context

Issue #129 checkpoint B6 (Ring 1-B design.md §8.4). One row per user, no
history table (Concept §4.2 — re-answering overwrites). `questionnaire` is a
single JSONB blob (8 closed-enum keys), so the CHECK constraints below
express the same three-layer domain-validation pattern as
6cd7544f63cf_add_domain_check_constraints_to_holdings.py, just against JSONB
keys instead of plain columns.

Values below are a FROZEN SNAPSHOT of app/services/questionnaire_taxonomy.py
as of this migration's authoring date — deliberately NOT imported live, same
rationale as 6cd7544f63cf's docstring: a migration must stay an immutable
historical snapshot, or `alembic upgrade head` on a fresh database could
silently produce a different constraint than an already-migrated one after
the taxonomy widens. Widening any of these sets later is a NEW migration.

`sectors_of_interest` mirrors `sector_taxonomy.VALID_SECTORS` as it stood at
authoring time (12 unified classes + Other) — same frozen-snapshot rule.

Revision ID: 9c56ac348d7d
Revises: e8f9a0b1c2d3
Create Date: 2026-08-25

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "9c56ac348d7d"
down_revision: str | Sequence[str] | None = "e8f9a0b1c2d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen snapshot — see module docstring. Do not import live constants here.
_ASSET_SCALES = ("100K_500K", "500K_2M", "OVER_2M", "UNDER_100K")
_MARKETS = ("A-Share", "HK", "Other", "US")
_STYLES = ("GROWTH", "INDEX", "MIXED", "VALUE")
_HORIZONS = ("LONG", "MEDIUM", "SHORT")
_RISK_APPETITES = ("AGGRESSIVE", "BALANCED", "CONSERVATIVE")
_OBJECTIVES = ("GROWTH", "INCOME", "PRESERVATION")
_INTEL_FOCUSES = ("BALANCED", "FUNDAMENTALS", "GEOPOLITICS", "MACRO")
_SECTORS = (
    "Communication",
    "Consumer Discretionary",
    "Consumer Staples",
    "Energy",
    "Financials",
    "Healthcare",
    "Industrials",
    "Materials",
    "Other",
    "Real Estate",
    "Technology",
    "Utilities",
)

# (jsonb key, allowed values) for the six single-select dimensions.
_SINGLE_SELECT_CONSTRAINTS = (
    ("asset_scale", _ASSET_SCALES),
    ("style", _STYLES),
    ("horizon", _HORIZONS),
    ("risk_appetite", _RISK_APPETITES),
    ("objective", _OBJECTIVES),
    ("intel_focus", _INTEL_FOCUSES),
)
# (jsonb key, allowed values) for the two multi-select dimensions.
_MULTI_SELECT_CONSTRAINTS = (
    ("markets", _MARKETS),
    ("sectors_of_interest", _SECTORS),
)


def _single_select_sql(key: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"questionnaire ? '{key}' AND questionnaire->>'{key}' IN ({quoted})"


def _multi_select_sql(key: str, values: tuple[str, ...]) -> str:
    # Postgres CHECK constraints cannot contain a subquery (verified against
    # a real run: "cannot use subquery in check constraint" — an earlier
    # version of this migration used
    # `NOT EXISTS (SELECT ... FROM jsonb_array_elements_text(...))`, which
    # fails at `alembic upgrade head`, not silently at query time). The `<@`
    # jsonb containment operator expresses the same "every element of this
    # array is in the allowed set" check as a single non-subquery expression:
    # for two arrays of scalars, `a <@ b` holds iff every element of `a`
    # appears in `b` (order-independent, tolerates duplicates).
    allowed_json = "[" + ", ".join(f'"{v}"' for v in values) + "]"
    return (
        f"questionnaire ? '{key}' "
        f"AND jsonb_typeof(questionnaire->'{key}') = 'array' "
        f"AND questionnaire->'{key}' <@ '{allowed_json}'::jsonb"
    )


def upgrade() -> None:
    op.create_table(
        "user_investment_context",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("questionnaire", postgresql.JSONB(), nullable=False),
        sa.Column("questionnaire_version", sa.Text(), nullable=False),
        sa.Column("free_text", sa.Text(), nullable=True),
        sa.Column(
            "updated_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    for key, values in _SINGLE_SELECT_CONSTRAINTS:
        op.create_check_constraint(key, "user_investment_context", _single_select_sql(key, values))
    for key, values in _MULTI_SELECT_CONSTRAINTS:
        op.create_check_constraint(key, "user_investment_context", _multi_select_sql(key, values))


def downgrade() -> None:
    op.drop_table("user_investment_context")
