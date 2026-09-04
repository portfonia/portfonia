"""add bounce/complaint handling: report recipient columns + auto_revoked

Issue #104 (Ring 1-Email Validation design doc, 2026-09-03 section §三):

- `reports.recipient_email` / `reports.recipient_purpose`: the REAL
  address/purpose a send actually used, written atomically alongside
  `email_sent_at`/`provider_message_id` — not re-derived after the fact
  from `recipient_email_with_purpose()`, which only reads the user's
  *current* address.
- `email_verifications.status` widened with `auto_revoked` (system-detected
  hard bounce/complaint), kept separate from the existing user-initiated
  `revoked` (issue #257).
- `email_verifications.revoke_reason`: only populated on an `auto_revoked`
  row, holding the raw Resend `last_event` (bounced/complained/failed/
  suppressed) that triggered it.

Revision ID: d3e4f5a6b7c8
Revises: c7d8e9f0a1b2
Create Date: 2026-09-03 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d3e4f5a6b7c8"
down_revision: str | Sequence[str] | None = "c7d8e9f0a1b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# FROZEN SNAPSHOTS as of this file's authoring date — deliberately NOT
# imported from the model modules, matching every prior migration in this
# chain (86b7be7f1fe5 / b7c8d9e0f1a2 / 6cd7544f63cf).
_PREVIOUS_EMAIL_VERIFICATION_STATUSES = (
    "pending",
    "verified",
    "expired",
    "superseded",
    "undeliverable",
    "revoked",
)
_NEW_EMAIL_VERIFICATION_STATUSES = (*_PREVIOUS_EMAIL_VERIFICATION_STATUSES, "auto_revoked")
_REPORT_RECIPIENT_PURPOSES = ("account_email", "delivery_email")


def _in_list_sql(column: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{v}'" for v in sorted(values))
    return f"{column} IN ({quoted})"


def upgrade() -> None:
    op.add_column("reports", sa.Column("recipient_email", sa.Text(), nullable=True))
    op.add_column("reports", sa.Column("recipient_purpose", sa.Text(), nullable=True))
    op.create_check_constraint(
        "recipient_purpose",
        "reports",
        _in_list_sql("recipient_purpose", _REPORT_RECIPIENT_PURPOSES),
    )

    op.add_column("email_verifications", sa.Column("revoke_reason", sa.Text(), nullable=True))
    op.drop_constraint("status", "email_verifications", type_="check")
    op.create_check_constraint(
        "status", "email_verifications", _in_list_sql("status", _NEW_EMAIL_VERIFICATION_STATUSES)
    )


def downgrade() -> None:
    op.drop_constraint("status", "email_verifications", type_="check")
    op.create_check_constraint(
        "status",
        "email_verifications",
        _in_list_sql("status", _PREVIOUS_EMAIL_VERIFICATION_STATUSES),
    )
    op.drop_column("email_verifications", "revoke_reason")

    op.drop_constraint("recipient_purpose", "reports", type_="check")
    op.drop_column("reports", "recipient_purpose")
    op.drop_column("reports", "recipient_email")
