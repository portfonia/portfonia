"""Add invitation letter delivery and unsubscribe fields.

Revision ID: b56900000001
Revises: a56600000001
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b56900000001"
down_revision: str | None = "a56600000001"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("invites", sa.Column("letter_sent_at", sa.TIMESTAMP(timezone=True)))
    op.add_column("invites", sa.Column("letter_provider_message_id", sa.Text()))
    op.add_column("invites", sa.Column("letter_delivery_event", sa.Text()))
    op.add_column("invites", sa.Column("letter_unsubscribed_at", sa.TIMESTAMP(timezone=True)))


def downgrade() -> None:
    op.drop_column("invites", "letter_unsubscribed_at")
    op.drop_column("invites", "letter_delivery_event")
    op.drop_column("invites", "letter_provider_message_id")
    op.drop_column("invites", "letter_sent_at")
