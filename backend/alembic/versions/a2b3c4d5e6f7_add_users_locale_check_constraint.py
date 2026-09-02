"""add users.locale check constraint

Issue #308: users.locale was a free Text column with no CheckConstraint —
same gap report_cadence had before e1f2a3b4c5d6. Adds a CHECK enumerating
the two report languages this issue actually wires up end to end (en, zh);
fr/es/Traditional Chinese have zero backend/config/i18n_glossary.yml
coverage and are deliberately not pre-enumerated (issue #308 "Out of
scope").

Values below are a FROZEN SNAPSHOT of VALID_REPORT_LANGUAGES
(app/models/user.py) as of this migration's authoring date — deliberately
NOT imported live, matching e1f2a3b4c5d6's precedent (itself following
6cd7544f63cf, holdings domain CHECK constraints): a migration must be an
immutable historical record, not re-derived from whatever the current code
state happens to be. Widening this set later is a NEW migration, not an
edit to this file.

No data backfill needed: every existing row already has locale = 'zh'
(hardcoded at signup before this issue), which is inside this constraint.

Revision ID: a2b3c4d5e6f7
Revises: b7c8d9e0f1a2
Create Date: 2026-09-01 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a2b3c4d5e6f7"
down_revision: Union[str, Sequence[str], None] = "b7c8d9e0f1a2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Frozen snapshot — see module docstring. Do not import live constants here.
_REPORT_LANGUAGES = ("en", "zh")


def _in_list_sql(column: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"


def upgrade() -> None:
    # Bare column name (not "ck_users_locale") — target_metadata's
    # naming_convention (app/models/base.py) applies the ck_%(table_name)s_
    # %(constraint_name)s prefix; a pre-rendered name doubles it (same
    # gotcha documented in 6cd7544f63cf and e1f2a3b4c5d6).
    op.create_check_constraint("locale", "users", _in_list_sql("locale", _REPORT_LANGUAGES))


def downgrade() -> None:
    op.drop_constraint("locale", "users", type_="check")
