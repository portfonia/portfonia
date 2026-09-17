"""add vigil configurations and objects

Issue #454 (Vigil R0 P2.1): adds vigil_configurations and vigil_objects, and
upgrades vigil_vaults.active_config_id/active_object_id/pending_config_id/
pending_object_id from plain nullable UUID columns (#451, no target table
existed yet) to real ON DELETE RESTRICT FKs now that the target tables
exist. Field/constraint contract is #450 Design section 4 /
Vigil_R0_Dev.md Appendix A.

Table creation order matters because of a genuine circular FK
(vigil_vaults.active_config_id -> vigil_configurations.id and
vigil_configurations.vault_id -> vigil_vaults.id, similarly for objects):
create vigil_configurations, then vigil_objects (which also FKs to
vigil_configurations via config_id), then ALTER vigil_vaults to add its four
new FK constraints last.

`vigil_configurations`/`vigil_objects` "at most one active and one pending"
rule (Appendix A) is a partial unique index rather than an app-only check —
this project's convention (Vigil Concept & Design.md appendix C.1) is that
an invariant expressible as a DB constraint isn't left to application code.
`vigil_objects`'s `crypto_fields_present_when_ready` CHECK is the same
reasoning applied to "ready/active rows have every crypto field populated
and octet_length(ciphertext) = plaintext_size + 16" (Appendix A) — a
`staging` row (no upload yet) intentionally has all of those columns NULL.

Purge order (services/user_purge.py) must null vigil_vaults' active/pending
pointers before deleting the vigil_objects/vigil_configurations rows they
point at — RESTRICT means a bare DELETE would otherwise fail loudly, which
is the intended defense against silently bypassing the feature's stop path
(Design section 6 / Contract constraints: "No ON DELETE CASCADE that
silently bypasses the feature stop path").

Revision ID: 3c9aa8def681
Revises: c683202eafa6
Create Date: 2026-09-17

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "3c9aa8def681"
down_revision: str | None = "c683202eafa6"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_VALID_CONFIGURATION_STATUSES = ("pending", "active", "retired")
_VALID_OBJECT_STATUSES = ("staging", "ready", "active", "retired", "deleted")
_MAX_PLAINTEXT_SIZE = 10_000_000
_GCM_TAG_LENGTH = 16


def upgrade() -> None:
    op.create_table(
        "vigil_configurations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("vault_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("config_revision", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("data_cipher", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_vigil_configurations")),
        sa.ForeignKeyConstraint(
            ["vault_id"],
            ["vigil_vaults.id"],
            name=op.f("fk_vigil_configurations_vault_id_vigil_vaults"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "vault_id",
            "config_revision",
            name=op.f("uq_vigil_configurations_vault_id_config_revision"),
        ),
        sa.CheckConstraint(
            "status IN (" + ", ".join(f"'{s}'" for s in _VALID_CONFIGURATION_STATUSES) + ")",
            name=op.f("ck_vigil_configurations_status"),
        ),
    )
    op.create_index(
        "uq_vigil_configurations_one_active",
        "vigil_configurations",
        ["vault_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_index(
        "uq_vigil_configurations_one_pending",
        "vigil_configurations",
        ["vault_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )

    op.create_table(
        "vigil_objects",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("vault_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("config_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("filename_cipher", sa.Text(), nullable=False),
        sa.Column("plaintext_size", sa.BigInteger(), nullable=False),
        sa.Column("ciphertext_size", sa.BigInteger(), nullable=True),
        sa.Column("cipher_sha256", sa.Text(), nullable=True),
        sa.Column("manifest", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("outer_cipher", sa.Text(), nullable=True),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=True),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_vigil_objects")),
        sa.ForeignKeyConstraint(
            ["vault_id"],
            ["vigil_vaults.id"],
            name=op.f("fk_vigil_objects_vault_id_vigil_vaults"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["config_id"],
            ["vigil_configurations.id"],
            name=op.f("fk_vigil_objects_config_id_vigil_configurations"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "vault_id", "request_id", name=op.f("uq_vigil_objects_vault_id_request_id")
        ),
        sa.CheckConstraint(
            "status IN (" + ", ".join(f"'{s}'" for s in _VALID_OBJECT_STATUSES) + ")",
            name=op.f("ck_vigil_objects_status"),
        ),
        sa.CheckConstraint(
            f"plaintext_size >= 0 AND plaintext_size <= {_MAX_PLAINTEXT_SIZE}",
            name=op.f("ck_vigil_objects_plaintext_size_bounds"),
        ),
        sa.CheckConstraint(
            "status NOT IN ('ready', 'active') OR ("
            "ciphertext_size IS NOT NULL AND cipher_sha256 IS NOT NULL "
            "AND manifest IS NOT NULL AND outer_cipher IS NOT NULL "
            "AND ciphertext IS NOT NULL "
            f"AND octet_length(ciphertext) = plaintext_size + {_GCM_TAG_LENGTH}"
            ")",
            name=op.f("ck_vigil_objects_crypto_fields_present_when_ready"),
        ),
    )
    op.create_index(
        "uq_vigil_objects_one_active",
        "vigil_objects",
        ["vault_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_index(
        "uq_vigil_objects_one_pending",
        "vigil_objects",
        ["vault_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('staging', 'ready')"),
    )

    with op.batch_alter_table("vigil_vaults") as batch_op:
        batch_op.create_foreign_key(
            op.f("fk_vigil_vaults_active_config_id_vigil_configurations"),
            "vigil_configurations",
            ["active_config_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_foreign_key(
            op.f("fk_vigil_vaults_active_object_id_vigil_objects"),
            "vigil_objects",
            ["active_object_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_foreign_key(
            op.f("fk_vigil_vaults_pending_config_id_vigil_configurations"),
            "vigil_configurations",
            ["pending_config_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_foreign_key(
            op.f("fk_vigil_vaults_pending_object_id_vigil_objects"),
            "vigil_objects",
            ["pending_object_id"],
            ["id"],
            ondelete="RESTRICT",
        )


def downgrade() -> None:
    with op.batch_alter_table("vigil_vaults") as batch_op:
        batch_op.drop_constraint(
            op.f("fk_vigil_vaults_pending_object_id_vigil_objects"), type_="foreignkey"
        )
        batch_op.drop_constraint(
            op.f("fk_vigil_vaults_pending_config_id_vigil_configurations"), type_="foreignkey"
        )
        batch_op.drop_constraint(
            op.f("fk_vigil_vaults_active_object_id_vigil_objects"), type_="foreignkey"
        )
        batch_op.drop_constraint(
            op.f("fk_vigil_vaults_active_config_id_vigil_configurations"), type_="foreignkey"
        )
    op.drop_table("vigil_objects")
    op.drop_table("vigil_configurations")
