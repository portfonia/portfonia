"""add vigil action tokens and consumed nonces

Issue #458 (Vigil R0 P2.3): adds vigil_action_tokens and
vigil_consumed_nonces. Field/constraint contract is #450 Design section 4
(Appendix A action_tokens / consumed_nonces rows), incorporated by
reference. This checkpoint's writers only use purpose=drill; cycle_id and
batch_id are nullable UUID columns without FKs until #459/#460 create
those tables.

Revision ID: e7a1c4d9b218
Revises: c8e4a1b7d902
Create Date: 2026-09-18

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "e7a1c4d9b218"
down_revision: str | None = "c8e4a1b7d902"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_VALID_PURPOSES = ("drill", "cycle_confirm", "owner_revoke")


def upgrade() -> None:
    op.create_table(
        "vigil_action_tokens",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("vault_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("config_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("object_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("cycle_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("batch_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("invalidated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_vigil_action_tokens")),
        sa.ForeignKeyConstraint(
            ["vault_id"],
            ["vigil_vaults.id"],
            name=op.f("fk_vigil_action_tokens_vault_id_vigil_vaults"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["config_id"],
            ["vigil_configurations.id"],
            name=op.f("fk_vigil_action_tokens_config_id_vigil_configurations"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["object_id"],
            ["vigil_objects.id"],
            name=op.f("fk_vigil_action_tokens_object_id_vigil_objects"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("token_hash", name=op.f("uq_vigil_action_tokens_token_hash")),
        sa.CheckConstraint(
            "purpose IN (" + ", ".join(f"'{p}'" for p in _VALID_PURPOSES) + ")",
            name=op.f("ck_vigil_action_tokens_purpose"),
        ),
    )
    op.create_index(
        "uq_vigil_action_tokens_one_pending_drill",
        "vigil_action_tokens",
        ["config_id", "object_id"],
        unique=True,
        postgresql_where=sa.text(
            "purpose = 'drill' AND confirmed_at IS NULL AND used_at IS NULL "
            "AND invalidated_at IS NULL"
        ),
    )
    op.create_table(
        "vigil_consumed_nonces",
        sa.Column("jti", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("jti", name=op.f("pk_vigil_consumed_nonces")),
    )


def downgrade() -> None:
    op.drop_table("vigil_consumed_nonces")
    op.drop_index("uq_vigil_action_tokens_one_pending_drill", table_name="vigil_action_tokens")
    op.drop_table("vigil_action_tokens")
