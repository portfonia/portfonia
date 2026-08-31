"""add revoked to email_verifications status

Issue #257 (Ring 1-Email Validation design doc §3.7): report-email
unsubscribe appends a `status=revoked` audit row rather than mutating the
historical `verified` row. `86b7be7f1fe5_add_email_verifications.py`
already flagged this as deferred:

    `status`: pending, verified, expired, superseded, undeliverable. `revoked`
    (design doc §3.7, email-embedded unsubscribe) is deliberately NOT
    included — that mechanism isn't implemented yet.

This migration is that widening: drop and recreate the `status`
CheckConstraint. Values below are a FROZEN SNAPSHOT of the closed set as of
this file's authoring date — deliberately NOT imported from
`VALID_EMAIL_VERIFICATION_STATUSES`, matching 86b7be7f1fe5 / 6cd7544f63cf.

Revision ID: b7c8d9e0f1a2
Revises: 86b7be7f1fe5
Create Date: 2026-08-31 00:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

revision: str = "b7c8d9e0f1a2"
down_revision: str | Sequence[str] | None = "86b7be7f1fe5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PREVIOUS_STATUSES = ("pending", "verified", "expired", "superseded", "undeliverable")
_NEW_STATUSES = (*_PREVIOUS_STATUSES, "revoked")


def _in_list_sql(column: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{v}'" for v in sorted(values))
    return f"{column} IN ({quoted})"


def upgrade() -> None:
    # Bare column name (not "ck_email_verifications_status") — target_metadata's
    # naming_convention (app/models/base.py) applies ck_%(table_name)s_
    # %(constraint_name)s; a pre-rendered name doubles it (same gotcha
    # documented in 6cd7544f63cf and 86b7be7f1fe5's original constraint).
    op.drop_constraint("status", "email_verifications", type_="check")
    op.create_check_constraint(
        "status", "email_verifications", _in_list_sql("status", _NEW_STATUSES)
    )


def downgrade() -> None:
    op.drop_constraint("status", "email_verifications", type_="check")
    op.create_check_constraint(
        "status", "email_verifications", _in_list_sql("status", _PREVIOUS_STATUSES)
    )
