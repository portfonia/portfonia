"""add provider_message_id to reports

Issue #45: send_report_email logs the Resend response's message id but never
persisted it, so a delivered report can't be cross-referenced against
Resend's own delivery/bounce/complaint webhooks or dashboard after the fact.

Revision ID: ed3e81d6cccb
Revises: 6cd7544f63cf
Create Date: 2026-08-09 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "ed3e81d6cccb"
down_revision: str | Sequence[str] | None = "6cd7544f63cf"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("reports", sa.Column("provider_message_id", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("reports", "provider_message_id")
