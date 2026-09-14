"""Create Vigil foundation tables.

Revision ID: c0ffee000011
Revises:
Create Date: 2026-09-14

P1.1 / issue #451: vaults, audit_events, runtime_heartbeat, consumed_nonces.
active_config_id / active_object_id are nullable columns with no FK.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "c0ffee000011"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_VAULT_PHASES = (
    "ARMED",
    "CHALLENGE_1",
    "CHALLENGE_2",
    "DISARMED",
    "FINAL_WARNING",
    "RELEASED",
    "REVOKED",
)


def upgrade() -> None:
    op.create_table(
        "vaults",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("owner_auth_subject", sa.Text(), nullable=False),
        sa.Column("phase", sa.Text(), nullable=False),
        sa.Column("revision", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("active_config_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("active_object_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("next_check_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("hold_reason", sa.Text(), nullable=True),
        sa.Column("held_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_owner_confirmed_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("retention_anchor_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("first_armed_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_vaults"),
        sa.UniqueConstraint("owner_auth_subject", name="uq_vaults_owner_auth_subject"),
        sa.CheckConstraint(
            "phase IN (" + ", ".join(f"'{p}'" for p in sorted(_VAULT_PHASES)) + ")",
            name="ck_vaults_phase",
        ),
        sa.CheckConstraint("revision >= 0", name="ck_vaults_revision_nonnegative"),
    )

    op.create_table(
        "audit_events",
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
        sa.Column(
            "occurred_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "detail",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_audit_events"),
        sa.ForeignKeyConstraint(
            ["vault_id"],
            ["vaults.id"],
            name="fk_audit_events_vault_id_vaults",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("vault_id", "sequence", name="uq_audit_events_vault_id_sequence"),
        sa.CheckConstraint(
            "actor_type IN ('ops', 'owner', 'system', 'token')",
            name="ck_audit_events_actor_type",
        ),
    )

    op.create_table(
        "runtime_heartbeat",
        sa.Column("id", sa.SmallInteger(), nullable=False),
        sa.Column("last_scan_completed_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_dependency_check_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("health", sa.Text(), server_default=sa.text("'held'"), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_runtime_heartbeat"),
        sa.CheckConstraint("id = 1", name="ck_runtime_heartbeat_singleton"),
        sa.CheckConstraint("health IN ('held', 'ok')", name="ck_runtime_heartbeat_health"),
    )
    op.execute(sa.text("INSERT INTO runtime_heartbeat (id, health) VALUES (1, 'held')"))

    op.create_table(
        "consumed_nonces",
        sa.Column("jti", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("expires_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column(
            "used_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("jti", name="pk_consumed_nonces"),
    )


def downgrade() -> None:
    op.drop_table("consumed_nonces")
    op.drop_table("runtime_heartbeat")
    op.drop_table("audit_events")
    op.drop_table("vaults")
