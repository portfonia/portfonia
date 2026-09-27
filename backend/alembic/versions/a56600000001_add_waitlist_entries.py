"""Add waitlist entries and the invite source marker.

Revision ID: a56600000001
Revises: d4b8c6e91a20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "a56600000001"
down_revision: str | None = "d4b8c6e91a20"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "waitlist_entries",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("email", sa.Text(), nullable=False, unique=True),
        sa.Column("locale", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("link_sent_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("status_changed_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("locale IN ('en', 'zh-Hans', 'zh-Hant')", name="locale"),
        sa.CheckConstraint("status IN ('pending', 'invited', 'rejected')", name="status"),
    )
    op.add_column("invites", sa.Column("waitlist_entry_id", postgresql.UUID(as_uuid=True)))
    op.create_foreign_key(
        "fk_invites_waitlist_entry_id_waitlist_entries", "invites", "waitlist_entries",
        ["waitlist_entry_id"], ["id"],
    )
    op.create_index("ix_invites_waitlist_entry_id", "invites", ["waitlist_entry_id"])


def downgrade() -> None:
    op.drop_index("ix_invites_waitlist_entry_id", table_name="invites")
    op.drop_constraint("fk_invites_waitlist_entry_id_waitlist_entries", "invites", type_="foreignkey")
    op.drop_column("invites", "waitlist_entry_id")
    op.drop_table("waitlist_entries")
