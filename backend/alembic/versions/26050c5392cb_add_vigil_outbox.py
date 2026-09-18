"""add vigil outbox

Issue #456 (Vigil R0 P3.1): adds vigil_outbox, the shared-worker encrypted
mail intent table. Field/constraint contract is #450 Design section 4
(Appendix A "outbox" row), incorporated by reference.

`scope_id` is a plain UUID column, not a FK, because the tables it will
eventually reference (cycles/rounds/release_batches) don't exist until
#458/#459/#460 add them — see app/models/vigil.py's VigilOutbox docstring.

`dedup_key` gets a plain UNIQUE constraint; `provider_id` gets a separate
partial unique index (nulls allowed, non-null values unique) since Postgres
UNIQUE already treats NULL as distinct from every other NULL, but a
two-column composite UNIQUE(dedup_key, provider_id) would still allow two
rows with the SAME dedup_key as long as their provider_id values differ —
not what "UNIQUE dedup_key and nonnull provider_id" (Appendix A) means.

Revision ID: 26050c5392cb
Revises: 3c9aa8def681
Create Date: 2026-09-17

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "26050c5392cb"
down_revision: str | None = "3c9aa8def681"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_VALID_PURPOSES = ("drill", "challenge", "release", "owner_notice")
_VALID_STATUSES = ("pending", "leased", "accepted", "failed", "unknown", "cancelled")


def upgrade() -> None:
    op.create_table(
        "vigil_outbox",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("vault_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("config_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("object_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("scope_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("dedup_key", sa.Text(), nullable=False),
        sa.Column("payload_cipher", sa.Text(), nullable=True),
        sa.Column("payload_sha256", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("attempts", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("first_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provider_id", sa.Text(), nullable=True),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.Text(), nullable=True),
        sa.Column("recipient_index", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_vigil_outbox")),
        sa.ForeignKeyConstraint(
            ["vault_id"],
            ["vigil_vaults.id"],
            name=op.f("fk_vigil_outbox_vault_id_vigil_vaults"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["config_id"],
            ["vigil_configurations.id"],
            name=op.f("fk_vigil_outbox_config_id_vigil_configurations"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["object_id"],
            ["vigil_objects.id"],
            name=op.f("fk_vigil_outbox_object_id_vigil_objects"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("dedup_key", name=op.f("uq_vigil_outbox_dedup_key")),
        sa.CheckConstraint(
            "purpose IN (" + ", ".join(f"'{p}'" for p in _VALID_PURPOSES) + ")",
            name=op.f("ck_vigil_outbox_purpose"),
        ),
        sa.CheckConstraint(
            "status IN (" + ", ".join(f"'{s}'" for s in _VALID_STATUSES) + ")",
            name=op.f("ck_vigil_outbox_status"),
        ),
        sa.CheckConstraint(
            "recipient_index IS NULL OR recipient_index BETWEEN 1 AND 3",
            name=op.f("ck_vigil_outbox_recipient_index_bounds"),
        ),
        sa.CheckConstraint("attempts >= 0", name=op.f("ck_vigil_outbox_attempts_nonneg")),
    )
    op.create_index(
        "uq_vigil_outbox_provider_id",
        "vigil_outbox",
        ["provider_id"],
        unique=True,
        postgresql_where=sa.text("provider_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_table("vigil_outbox")
