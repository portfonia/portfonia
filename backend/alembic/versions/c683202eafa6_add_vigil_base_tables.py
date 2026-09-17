"""add vigil base tables

Issue #451 (Vigil R0 P1.1): the shared-application base schema — only
vigil_vaults, vigil_runtime and vigil_audit_events. Field/constraint
contract is #450 Design section 4 / Vigil_R0_Dev.md Appendix A. No other
Vigil table exists yet; each later checkpoint (#454+) adds its own via a
fresh migration chained onto whatever head exists then, never by reviving
the withdrawn old Vigil migration history (PR #475 already reverted it to
the pre-Vigil baseline).

`vigil_vaults.owner_user_id` is UNIQUE + ON DELETE RESTRICT: one vault per
user, and a bare `DELETE FROM users` with a vault still pointing at it must
fail loudly rather than cascade — `app/services/user_purge.py`'s purge hook
deletes the vault row first, in the same local transaction, before deleting
the user.

`vigil_audit_events.vault_id` is also ON DELETE RESTRICT for the same
reason (Design section 6/Contract constraints: "No ON DELETE CASCADE that
silently bypasses the feature stop path") — nothing writes audit_events
rows yet, but the FK is part of the frozen schema contract, not deferred
along with the business logic that will eventually populate it.

`active_config_id`/`active_object_id`/`pending_config_id`/`pending_object_id`
are plain nullable UUID columns with no FK — their target tables
(vigil_configurations/vigil_objects) don't exist until #454.

Revision ID: c683202eafa6
Revises: b3c4d5e6f7a8
Create Date: 2026-09-17

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "c683202eafa6"
down_revision: str | None = "b3c4d5e6f7a8"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_VALID_PHASES = (
    "DISARMED",
    "ARMED",
    "CHALLENGE_1",
    "CHALLENGE_2",
    "FINAL_WARNING",
    "RELEASED",
    "REVOKED",
)
_VALID_RUNTIME_HEALTH = ("ok", "held")
_VALID_AUDIT_ACTOR_TYPES = ("owner", "token", "system", "ops")


def upgrade() -> None:
    op.create_table(
        "vigil_vaults",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("owner_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("phase", sa.Text(), nullable=False, server_default=sa.text("'DISARMED'")),
        sa.Column("revision", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("active_config_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("active_object_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("pending_config_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("pending_object_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("next_check_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("hold_reason", sa.Text(), nullable=True),
        sa.Column("held_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("first_armed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_owner_confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retention_anchor_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_vigil_vaults")),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.id"],
            name=op.f("fk_vigil_vaults_owner_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("owner_user_id", name=op.f("uq_vigil_vaults_owner_user_id")),
        sa.CheckConstraint(
            "phase IN ("
            + ", ".join(f"'{p}'" for p in _VALID_PHASES)
            + ")",
            name=op.f("ck_vigil_vaults_phase"),
        ),
        sa.CheckConstraint("revision >= 0", name=op.f("ck_vigil_vaults_revision_nonneg")),
    )

    op.create_table(
        "vigil_runtime",
        sa.Column("id", sa.SmallInteger(), nullable=False),
        sa.Column("last_scan_completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_dispatch_sweep_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("health", sa.Text(), nullable=False, server_default=sa.text("'ok'")),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_vigil_runtime")),
        sa.CheckConstraint("id = 1", name=op.f("ck_vigil_runtime_singleton")),
        sa.CheckConstraint(
            "health IN (" + ", ".join(f"'{h}'" for h in _VALID_RUNTIME_HEALTH) + ")",
            name=op.f("ck_vigil_runtime_health"),
        ),
    )

    op.create_table(
        "vigil_audit_events",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
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
            "detail", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_vigil_audit_events")),
        sa.ForeignKeyConstraint(
            ["vault_id"],
            ["vigil_vaults.id"],
            name=op.f("fk_vigil_audit_events_vault_id_vigil_vaults"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "vault_id", "sequence", name=op.f("uq_vigil_audit_events_vault_id_sequence")
        ),
        sa.CheckConstraint(
            "actor_type IN (" + ", ".join(f"'{a}'" for a in _VALID_AUDIT_ACTOR_TYPES) + ")",
            name=op.f("ck_vigil_audit_events_actor_type"),
        ),
    )


def downgrade() -> None:
    op.drop_table("vigil_audit_events")
    op.drop_table("vigil_runtime")
    op.drop_table("vigil_vaults")
