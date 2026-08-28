"""add report_cadence check constraint to users

Issue #191: users.report_cadence was a free Text column with no
CheckConstraint, unlike status/auth_provider on the same model. Adds a CHECK
enumerating the two cadences that actually exist in code today (mwf, weekly)
— monthly/daily_brief are speculative Ring 1 extensions with no Beat row and
no way to be set yet, deliberately not pre-enumerated (see Ring 1-B Cadence
design doc, decision point 3).

Values below are a FROZEN SNAPSHOT of VALID_REPORT_CADENCES
(app/models/user.py) as of this migration's authoring date — deliberately
NOT imported live, matching the precedent set by 6cd7544f63cf (holdings
domain CHECK constraints): a migration must be an immutable historical
record, not re-derived from whatever the current code state happens to be.
Widening this set later is a NEW migration, not an edit to this file.

Production audited before writing this (2026-08-28, read-only SELECT):
4 users, report_cadence in {mwf, weekly} — all within this constraint, no
data fixup needed.

Revision ID: e1f2a3b4c5d6
Revises: 4edf69bf41ab
Create Date: 2026-08-28 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e1f2a3b4c5d6"
down_revision: Union[str, Sequence[str], None] = "4edf69bf41ab"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Frozen snapshot — see module docstring. Do not import live constants here.
_REPORT_CADENCES = ("mwf", "weekly")


def _in_list_sql(column: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"


def upgrade() -> None:
    # Bare column name (not "ck_users_report_cadence") — target_metadata's
    # naming_convention (app/models/base.py) applies the ck_%(table_name)s_
    # %(constraint_name)s prefix; a pre-rendered name doubles it (same gotcha
    # documented in 6cd7544f63cf).
    op.create_check_constraint(
        "report_cadence", "users", _in_list_sql("report_cadence", _REPORT_CADENCES)
    )


def downgrade() -> None:
    op.drop_constraint("report_cadence", "users", type_="check")
