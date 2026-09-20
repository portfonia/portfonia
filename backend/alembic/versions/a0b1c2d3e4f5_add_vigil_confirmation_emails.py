"""Add reusable owner-scoped Vigil confirmation emails.

Revision ID: a0b1c2d3e4f5
Revises: 700aa7d9e13e
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "a0b1c2d3e4f5"
down_revision: str | None = "700aa7d9e13e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "vigil_confirmation_emails",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("owner_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("address_cipher", sa.Text(), nullable=False),
        sa.Column("address_fingerprint", sa.Text(), nullable=False),
        sa.Column("verified_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.id"],
            name=op.f("fk_vigil_confirmation_emails_owner_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_vigil_confirmation_emails")),
        sa.UniqueConstraint(
            "owner_user_id",
            "address_fingerprint",
            name="uq_vigil_confirmation_emails_owner_user_id_address_fingerprint",
        ),
    )

    op.drop_constraint(op.f("ck_vigil_outbox_purpose"), "vigil_outbox", type_="check")
    op.alter_column("vigil_outbox", "config_id", existing_type=postgresql.UUID(), nullable=True)
    op.alter_column("vigil_outbox", "object_id", existing_type=postgresql.UUID(), nullable=True)
    op.create_check_constraint(
        op.f("ck_vigil_outbox_purpose"),
        "vigil_outbox",
        "purpose IN ('challenge', 'drill', 'email_verify', 'owner_notice', 'release')",
    )
    op.create_check_constraint(
        op.f("ck_vigil_outbox_purpose_context"),
        "vigil_outbox",
        "(purpose = 'email_verify' AND config_id IS NULL AND object_id IS NULL) OR "
        "(purpose <> 'email_verify' AND config_id IS NOT NULL AND object_id IS NOT NULL)",
    )

    op.drop_constraint(
        op.f("ck_vigil_action_tokens_purpose"), "vigil_action_tokens", type_="check"
    )
    op.alter_column(
        "vigil_action_tokens", "config_id", existing_type=postgresql.UUID(), nullable=True
    )
    op.alter_column(
        "vigil_action_tokens", "object_id", existing_type=postgresql.UUID(), nullable=True
    )
    op.add_column(
        "vigil_action_tokens",
        sa.Column("confirmation_email_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        op.f("fk_vigil_action_tokens_confirmation_email_id_vigil_confirmation_emails"),
        "vigil_action_tokens",
        "vigil_confirmation_emails",
        ["confirmation_email_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        op.f("ck_vigil_action_tokens_purpose"),
        "vigil_action_tokens",
        "purpose IN ('cycle_confirm', 'drill', 'email_verify', 'owner_revoke')",
    )
    op.create_check_constraint(
        op.f("ck_vigil_action_tokens_purpose_context"),
        "vigil_action_tokens",
        "(purpose = 'email_verify' AND confirmation_email_id IS NOT NULL "
        "AND config_id IS NULL AND object_id IS NULL) OR "
        "(purpose <> 'email_verify' AND confirmation_email_id IS NULL "
        "AND config_id IS NOT NULL AND object_id IS NOT NULL)",
    )


def downgrade() -> None:
    op.execute("DELETE FROM vigil_outbox WHERE purpose = 'email_verify'")
    op.execute("DELETE FROM vigil_action_tokens WHERE purpose = 'email_verify'")
    op.drop_constraint(
        op.f("ck_vigil_action_tokens_purpose_context"), "vigil_action_tokens", type_="check"
    )
    op.drop_constraint(
        op.f("fk_vigil_action_tokens_confirmation_email_id_vigil_confirmation_emails"),
        "vigil_action_tokens",
        type_="foreignkey",
    )
    op.drop_column("vigil_action_tokens", "confirmation_email_id")
    op.drop_constraint(
        op.f("ck_vigil_action_tokens_purpose"), "vigil_action_tokens", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_vigil_action_tokens_purpose"),
        "vigil_action_tokens",
        "purpose IN ('cycle_confirm', 'drill', 'owner_revoke')",
    )
    op.alter_column(
        "vigil_action_tokens", "object_id", existing_type=postgresql.UUID(), nullable=False
    )
    op.alter_column(
        "vigil_action_tokens", "config_id", existing_type=postgresql.UUID(), nullable=False
    )

    op.drop_constraint(
        op.f("ck_vigil_outbox_purpose_context"), "vigil_outbox", type_="check"
    )
    op.drop_constraint(op.f("ck_vigil_outbox_purpose"), "vigil_outbox", type_="check")
    op.create_check_constraint(
        op.f("ck_vigil_outbox_purpose"),
        "vigil_outbox",
        "purpose IN ('challenge', 'drill', 'owner_notice', 'release')",
    )
    op.alter_column("vigil_outbox", "object_id", existing_type=postgresql.UUID(), nullable=False)
    op.alter_column("vigil_outbox", "config_id", existing_type=postgresql.UUID(), nullable=False)
    op.drop_table("vigil_confirmation_emails")
