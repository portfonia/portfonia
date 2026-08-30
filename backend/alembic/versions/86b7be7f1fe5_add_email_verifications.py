"""add email verifications

Issue #260 (Ring 1-Email Validation design doc §3.2): a generic, reusable
email-verification mechanism. Adds the `email_verifications` table (the
append-only verification/history record) plus two denormalized timestamp
columns on `users` that the report-send hot path will read directly once
that consumer lands (out of scope here — see the issue).

`purpose` and `status` are FROZEN SNAPSHOTS of the enums this issue's code
actually reaches (deliberately NOT importing the model's live tuples,
matching the precedent set by 6cd7544f63cf and e1f2a3b4c5d6): widening
either set later is a NEW migration, not an edit to this file.

- `purpose`: account_email, delivery_email, ops_manual. The Vigil-reuse
  placeholders (`vigil_account_confirmation`/`vigil_backup_confirmation`)
  from the design doc are deliberately NOT included — no code creates them
  yet.
- `status`: pending, verified, expired, superseded, undeliverable. `revoked`
  (design doc §3.7, email-embedded unsubscribe) is deliberately NOT
  included — that mechanism isn't implemented yet.

Revision ID: 86b7be7f1fe5
Revises: e1f2a3b4c5d6
Create Date: 2026-08-29 20:29:57.343562

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '86b7be7f1fe5'
down_revision: Union[str, Sequence[str], None] = 'e1f2a3b4c5d6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_VALID_PURPOSES = ("account_email", "delivery_email", "ops_manual")
_VALID_STATUSES = ("pending", "verified", "expired", "superseded", "undeliverable")


def _in_list_sql(column: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{v}'" for v in sorted(values))
    return f"{column} IN ({quoted})"


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "users",
        sa.Column("email_verified_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column(
            "delivery_email_verified_at", postgresql.TIMESTAMP(timezone=True), nullable=True
        ),
    )

    op.create_table(
        "email_verifications",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False, unique=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column(
            "requested_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("expires_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("verified_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_sent_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("resend_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("provider_message_id", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(_in_list_sql("purpose", _VALID_PURPOSES), name="purpose"),
        sa.CheckConstraint(_in_list_sql("status", _VALID_STATUSES), name="status"),
    )
    op.create_index(
        "ix_email_verifications_user_id_purpose", "email_verifications", ["user_id", "purpose"]
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_email_verifications_user_id_purpose", table_name="email_verifications")
    op.drop_table("email_verifications")
    op.drop_column("users", "delivery_email_verified_at")
    op.drop_column("users", "email_verified_at")
