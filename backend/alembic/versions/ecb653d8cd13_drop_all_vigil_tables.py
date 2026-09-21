"""drop all vigil tables

Issue #542: the Vigil feature is removed entirely rather than continuing to
be fixed (second wholesale reversal after #473's isolated-architecture
retraction — see the issue for the production-verification defects that
triggered this). Drops all 11 `vigil_*` tables. No Vigil-only enum types
exist (all "enum" validation in this schema uses CHECK constraints, not
Postgres native enum types), so there is nothing else to drop.

`vigil_vaults` and `vigil_configurations`/`vigil_objects` hold a genuine
circular FK (vigil_vaults.active_config_id/active_object_id/
pending_config_id/pending_object_id -> vigil_configurations/vigil_objects,
while vigil_configurations.vault_id/vigil_objects.vault_id -> vigil_vaults),
so no single table in that trio can be dropped ahead of the others with a
plain per-table `DROP TABLE`. A single multi-table `DROP TABLE t1, t2, ...`
statement resolves this natively (Postgres checks constraints against the
combined post-drop catalog, not incrementally table-by-table), so all 11
tables are dropped in one statement rather than needing individual FK-order
sequencing or `CASCADE`. Verified empirically against a schema-copy of
production before writing this migration.

Historical Vigil-creating/altering migrations
(c683202eafa6/3c9aa8def681/26050c5392cb/c8e4a1b7d902/e7a1c4d9b218/
a9c3e7f1b204/b7d1c4e8f2a3/700aa7d9e13e/a0b1c2d3e4f5) are left untouched.

Revision ID: ecb653d8cd13
Revises: a0b1c2d3e4f5
Create Date: 2026-09-20

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "ecb653d8cd13"
down_revision: str | None = "a0b1c2d3e4f5"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_VIGIL_TABLES = (
    "vigil_delivery_events",
    "vigil_rounds",
    "vigil_action_tokens",
    "vigil_outbox",
    "vigil_cycles",
    "vigil_objects",
    "vigil_configurations",
    "vigil_audit_events",
    "vigil_confirmation_emails",
    "vigil_vaults",
    "vigil_runtime",
)


def upgrade() -> None:
    # Single statement, not per-table op.drop_table calls — see module
    # docstring for why the vigil_vaults/vigil_configurations/vigil_objects
    # circular FK requires this.
    op.execute(f"DROP TABLE {', '.join(_VIGIL_TABLES)}")


def downgrade() -> None:
    op.create_table(
        "vigil_vaults",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("owner_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("phase", sa.Text(), server_default=sa.text("'DISARMED'"), nullable=False),
        sa.Column("revision", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("active_config_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("active_object_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("pending_config_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("pending_object_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("next_check_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("hold_reason", sa.Text(), nullable=True),
        sa.Column("held_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("first_armed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_owner_confirmed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("retention_anchor_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "phase = ANY (ARRAY['DISARMED'::text, 'ARMED'::text, 'CHALLENGE_1'::text, "
            "'CHALLENGE_2'::text, 'FINAL_WARNING'::text, 'RELEASED'::text, 'REVOKED'::text])",
            name=op.f("ck_vigil_vaults_phase"),
        ),
        sa.CheckConstraint("revision >= 0", name=op.f("ck_vigil_vaults_revision_nonneg")),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.id"],
            name=op.f("fk_vigil_vaults_owner_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_vigil_vaults")),
        sa.UniqueConstraint("owner_user_id", name=op.f("uq_vigil_vaults_owner_user_id")),
    )

    op.create_table(
        "vigil_confirmation_emails",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("owner_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("address_cipher", sa.Text(), nullable=False),
        sa.Column("address_fingerprint", sa.Text(), nullable=False),
        sa.Column("verified_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
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
            name=op.f("uq_vigil_confirmation_emails_owner_user_id_address_fingerprint"),
        ),
    )

    op.create_table(
        "vigil_configurations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("vault_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("config_revision", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("data_cipher", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status = ANY (ARRAY['pending'::text, 'active'::text, 'retired'::text])",
            name=op.f("ck_vigil_configurations_status"),
        ),
        sa.ForeignKeyConstraint(
            ["vault_id"],
            ["vigil_vaults.id"],
            name=op.f("fk_vigil_configurations_vault_id_vigil_vaults"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_vigil_configurations")),
        sa.UniqueConstraint(
            "vault_id",
            "config_revision",
            name=op.f("uq_vigil_configurations_vault_id_config_revision"),
        ),
    )
    op.create_index(
        "uq_vigil_configurations_one_active",
        "vigil_configurations",
        ["vault_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'::text"),
    )
    op.create_index(
        "uq_vigil_configurations_one_pending",
        "vigil_configurations",
        ["vault_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'::text"),
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
        sa.Column("activated_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status <> ALL (ARRAY['ready'::text, 'active'::text]) OR "
            "(ciphertext_size IS NOT NULL AND cipher_sha256 IS NOT NULL AND manifest IS NOT NULL "
            "AND outer_cipher IS NOT NULL AND ciphertext IS NOT NULL AND "
            "octet_length(ciphertext) = (plaintext_size + 16))",
            name=op.f("ck_vigil_objects_crypto_fields_present_when_ready"),
        ),
        sa.CheckConstraint(
            "plaintext_size >= 0 AND plaintext_size <= 10000000",
            name=op.f("ck_vigil_objects_plaintext_size_bounds"),
        ),
        sa.CheckConstraint(
            "status = ANY (ARRAY['staging'::text, 'ready'::text, 'active'::text, 'retired'::text, 'deleted'::text])",
            name=op.f("ck_vigil_objects_status"),
        ),
        sa.ForeignKeyConstraint(
            ["config_id"],
            ["vigil_configurations.id"],
            name=op.f("fk_vigil_objects_config_id_vigil_configurations"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["vault_id"],
            ["vigil_vaults.id"],
            name=op.f("fk_vigil_objects_vault_id_vigil_vaults"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_vigil_objects")),
        sa.UniqueConstraint(
            "vault_id", "request_id", name=op.f("uq_vigil_objects_vault_id_request_id")
        ),
    )
    op.create_index(
        "uq_vigil_objects_one_active",
        "vigil_objects",
        ["vault_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'::text"),
    )
    op.create_index(
        "uq_vigil_objects_one_pending",
        "vigil_objects",
        ["vault_id"],
        unique=True,
        postgresql_where=sa.text("status = ANY (ARRAY['staging'::text, 'ready'::text])"),
    )

    op.create_foreign_key(
        op.f("fk_vigil_vaults_active_config_id_vigil_configurations"),
        "vigil_vaults",
        "vigil_configurations",
        ["active_config_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        op.f("fk_vigil_vaults_active_object_id_vigil_objects"),
        "vigil_vaults",
        "vigil_objects",
        ["active_object_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        op.f("fk_vigil_vaults_pending_config_id_vigil_configurations"),
        "vigil_vaults",
        "vigil_configurations",
        ["pending_config_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        op.f("fk_vigil_vaults_pending_object_id_vigil_objects"),
        "vigil_vaults",
        "vigil_objects",
        ["pending_object_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    op.create_table(
        "vigil_audit_events",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("vault_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("actor_type", sa.Text(), nullable=False),
        sa.Column("actor_ref", sa.Text(), nullable=True),
        sa.Column("from_phase", sa.Text(), nullable=True),
        sa.Column("to_phase", sa.Text(), nullable=True),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "actor_type = ANY (ARRAY['owner'::text, 'token'::text, 'system'::text, 'ops'::text])",
            name=op.f("ck_vigil_audit_events_actor_type"),
        ),
        sa.ForeignKeyConstraint(
            ["vault_id"],
            ["vigil_vaults.id"],
            name=op.f("fk_vigil_audit_events_vault_id_vigil_vaults"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_vigil_audit_events")),
        sa.UniqueConstraint(
            "vault_id", "sequence", name=op.f("uq_vigil_audit_events_vault_id_sequence")
        ),
    )

    op.create_table(
        "vigil_outbox",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("vault_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("config_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("object_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("scope_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("dedup_key", sa.Text(), nullable=False),
        sa.Column("payload_cipher", sa.Text(), nullable=True),
        sa.Column("payload_sha256", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("attempts", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("lease_until", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("next_attempt_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("first_attempt_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("provider_id", sa.Text(), nullable=True),
        sa.Column("accepted_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.Text(), nullable=True),
        sa.Column("recipient_index", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("attempts >= 0", name=op.f("ck_vigil_outbox_attempts_nonneg")),
        sa.CheckConstraint(
            "purpose = ANY (ARRAY['challenge'::text, 'drill'::text, 'email_verify'::text, "
            "'owner_notice'::text, 'release'::text])",
            name=op.f("ck_vigil_outbox_purpose"),
        ),
        sa.CheckConstraint(
            "(purpose = 'email_verify'::text AND config_id IS NULL AND object_id IS NULL) OR "
            "(purpose <> 'email_verify'::text AND config_id IS NOT NULL AND object_id IS NOT NULL)",
            name=op.f("ck_vigil_outbox_purpose_context"),
        ),
        sa.CheckConstraint(
            "recipient_index IS NULL OR (recipient_index >= 1 AND recipient_index <= 3)",
            name=op.f("ck_vigil_outbox_recipient_index_bounds"),
        ),
        sa.CheckConstraint(
            "status = ANY (ARRAY['pending'::text, 'accepted'::text, 'failed'::text])",
            name=op.f("ck_vigil_outbox_status"),
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
        sa.ForeignKeyConstraint(
            ["vault_id"],
            ["vigil_vaults.id"],
            name=op.f("fk_vigil_outbox_vault_id_vigil_vaults"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_vigil_outbox")),
        sa.UniqueConstraint("dedup_key", name=op.f("uq_vigil_outbox_dedup_key")),
    )
    op.create_index(
        "uq_vigil_outbox_provider_id",
        "vigil_outbox",
        ["provider_id"],
        unique=True,
        postgresql_where=sa.text("provider_id IS NOT NULL"),
    )

    op.create_table(
        "vigil_delivery_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider_event_id", sa.Text(), nullable=False),
        sa.Column("outbox_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("provider_message_id", sa.Text(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("provider_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("received_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("evidence_source", sa.Text(), nullable=False),
        sa.Column("address_cipher", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "evidence_source = ANY (ARRAY['webhook'::text, 'poll'::text])",
            name=op.f("ck_vigil_delivery_events_evidence_source"),
        ),
        sa.ForeignKeyConstraint(
            ["outbox_id"],
            ["vigil_outbox.id"],
            name=op.f("fk_vigil_delivery_events_outbox_id_vigil_outbox"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_vigil_delivery_events")),
        sa.UniqueConstraint(
            "provider_event_id", name=op.f("uq_vigil_delivery_events_provider_event_id")
        ),
    )

    op.create_table(
        "vigil_cycles",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("vault_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("config_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("object_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("current_level", sa.BigInteger(), nullable=False),
        sa.Column("resolved_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "current_level >= 1 AND current_level <= 3",
            name=op.f("ck_vigil_cycles_current_level_bounds"),
        ),
        sa.CheckConstraint(
            "status = ANY (ARRAY['active'::text, 'confirmed'::text, 'released'::text, 'cancelled'::text])",
            name=op.f("ck_vigil_cycles_status"),
        ),
        sa.ForeignKeyConstraint(
            ["config_id"],
            ["vigil_configurations.id"],
            name=op.f("fk_vigil_cycles_config_id_vigil_configurations"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["object_id"],
            ["vigil_objects.id"],
            name=op.f("fk_vigil_cycles_object_id_vigil_objects"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["vault_id"],
            ["vigil_vaults.id"],
            name=op.f("fk_vigil_cycles_vault_id_vigil_vaults"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_vigil_cycles")),
    )
    op.create_index(
        "uq_vigil_cycles_one_active",
        "vigil_cycles",
        ["vault_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'::text"),
    )

    op.create_table(
        "vigil_action_tokens",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("vault_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("config_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("object_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("cycle_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("batch_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("used_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("invalidated_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("confirmed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("confirmation_email_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "purpose = ANY (ARRAY['cycle_confirm'::text, 'drill'::text, 'email_verify'::text, 'owner_revoke'::text])",
            name=op.f("ck_vigil_action_tokens_purpose"),
        ),
        sa.CheckConstraint(
            "(purpose = 'email_verify'::text AND confirmation_email_id IS NOT NULL AND "
            "config_id IS NULL AND object_id IS NULL) OR "
            "(purpose <> 'email_verify'::text AND confirmation_email_id IS NULL AND "
            "config_id IS NOT NULL AND object_id IS NOT NULL)",
            name=op.f("ck_vigil_action_tokens_purpose_context"),
        ),
        sa.ForeignKeyConstraint(
            ["config_id"],
            ["vigil_configurations.id"],
            name=op.f("fk_vigil_action_tokens_config_id_vigil_configurations"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["confirmation_email_id"],
            ["vigil_confirmation_emails.id"],
            name=op.f("fk_vigil_action_tokens_confirmation_email_id_vigil_conf_3bdc"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["cycle_id"],
            ["vigil_cycles.id"],
            name=op.f("fk_vigil_action_tokens_cycle_id_vigil_cycles"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["object_id"],
            ["vigil_objects.id"],
            name=op.f("fk_vigil_action_tokens_object_id_vigil_objects"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["vault_id"],
            ["vigil_vaults.id"],
            name=op.f("fk_vigil_action_tokens_vault_id_vigil_vaults"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_vigil_action_tokens")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_vigil_action_tokens_token_hash")),
    )
    op.create_index(
        "uq_vigil_action_tokens_one_pending_drill",
        "vigil_action_tokens",
        ["config_id", "object_id"],
        unique=True,
        postgresql_where=sa.text(
            "purpose = 'drill'::text AND confirmed_at IS NULL AND used_at IS NULL AND invalidated_at IS NULL"
        ),
    )

    op.create_table(
        "vigil_rounds",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("cycle_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("level", sa.BigInteger(), nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("outbox_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("anchor_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("deadline_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("superseded_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(anchor_at IS NULL AND deadline_at IS NULL) OR (anchor_at IS NOT NULL AND deadline_at IS NOT NULL)",
            name=op.f("ck_vigil_rounds_anchor_deadline_pair"),
        ),
        sa.CheckConstraint("generation >= 1", name=op.f("ck_vigil_rounds_generation_nonneg")),
        sa.CheckConstraint("level >= 1 AND level <= 3", name=op.f("ck_vigil_rounds_level_bounds")),
        sa.ForeignKeyConstraint(
            ["cycle_id"],
            ["vigil_cycles.id"],
            name=op.f("fk_vigil_rounds_cycle_id_vigil_cycles"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["outbox_id"],
            ["vigil_outbox.id"],
            name=op.f("fk_vigil_rounds_outbox_id_vigil_outbox"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_vigil_rounds")),
        sa.UniqueConstraint(
            "cycle_id",
            "level",
            "generation",
            name=op.f("uq_vigil_rounds_cycle_id_level_generation"),
        ),
    )
    op.create_index(
        "uq_vigil_rounds_one_open_generation",
        "vigil_rounds",
        ["cycle_id", "level"],
        unique=True,
        postgresql_where=sa.text("superseded_at IS NULL"),
    )

    op.create_table(
        "vigil_runtime",
        sa.Column("id", sa.SmallInteger(), nullable=False),
        sa.Column("last_scan_completed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_dispatch_sweep_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("health", sa.Text(), server_default=sa.text("'ok'"), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_vigil_runtime")),
        sa.CheckConstraint("id = 1", name=op.f("ck_vigil_runtime_singleton")),
        sa.CheckConstraint(
            "health = ANY (ARRAY['ok'::text, 'held'::text])", name=op.f("ck_vigil_runtime_health")
        ),
    )
