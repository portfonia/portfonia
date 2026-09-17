from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    SmallInteger,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

# Vigil R0 P1.1 (issue #451) — base schema only: vigil_vaults, vigil_runtime,
# vigil_audit_events. Field/constraint contract is #450 Design section 4 /
# Vigil_R0_Dev.md Appendix A, incorporated by reference — do not diverge from
# it without updating that comment. Later checkpoints (#454+) add the rest of
# the vigil_* tables (configurations/objects/cycles/...); this file only
# grows alongside the checkpoint that actually needs each one.

VALID_VIGIL_PHASES = (
    "DISARMED",
    "ARMED",
    "CHALLENGE_1",
    "CHALLENGE_2",
    "FINAL_WARNING",
    "RELEASED",
    "REVOKED",
)
VALID_VIGIL_RUNTIME_HEALTH = ("ok", "held")
VALID_VIGIL_AUDIT_ACTOR_TYPES = ("owner", "token", "system", "ops")


def _in_list_sql(column: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{v}'" for v in sorted(values))
    return f"{column} IN ({quoted})"


class VigilVault(Base):
    """Per-owner Vigil vault row — one per user (issue #451).

    Business logic (arm/release/replace/etc.) is not implemented yet; this
    checkpoint only carries the schema and the DISARMED/revision=0 default
    state a bare `INSERT` produces. `active_config_id`/`active_object_id`/
    `pending_config_id`/`pending_object_id` are plain nullable UUID columns
    with no FK — their target tables (vigil_configurations/vigil_objects)
    don't exist until #454.
    """

    __tablename__ = "vigil_vaults"
    __table_args__ = (
        CheckConstraint(_in_list_sql("phase", VALID_VIGIL_PHASES), name="phase"),
        CheckConstraint("revision >= 0", name="revision_nonneg"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), unique=True, nullable=False
    )
    phase: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'DISARMED'"))
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    active_config_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    active_object_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    pending_config_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    pending_object_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    next_check_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    hold_reason: Mapped[str | None] = mapped_column(Text)
    held_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    first_armed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    last_owner_confirmed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    retention_anchor_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )


class VigilRuntime(Base):
    """System-level singleton heartbeat row (`id=1` only) — issue #451.

    No scan/dispatch task writes this yet (#453/#456+); the CHECK constraint
    just enforces the singleton shape from day one so a later insert bug
    can't silently create a second row.
    """

    __tablename__ = "vigil_runtime"
    __table_args__ = (
        CheckConstraint("id = 1", name="singleton"),
        CheckConstraint(_in_list_sql("health", VALID_VIGIL_RUNTIME_HEALTH), name="health"),
    )

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    last_scan_completed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    last_dispatch_sweep_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    health: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'ok'"))
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )


class VigilAuditEvent(Base):
    """Append-only per-vault audit log (issue #451).

    Nothing writes rows here yet — no route or task in this checkpoint
    mutates a vault's phase — but the table/FK/UNIQUE contract is part of
    #450 Design section4's frozen field list, so it ships now alongside
    vaults/runtime rather than being added piecemeal later.
    """

    __tablename__ = "vigil_audit_events"
    __table_args__ = (
        UniqueConstraint("vault_id", "sequence", name="uq_vigil_audit_events_vault_id_sequence"),
        CheckConstraint(
            _in_list_sql("actor_type", VALID_VIGIL_AUDIT_ACTOR_TYPES), name="actor_type"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    vault_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("vigil_vaults.id", ondelete="RESTRICT"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    actor_type: Mapped[str] = mapped_column(Text, nullable=False)
    actor_ref: Mapped[str | None] = mapped_column(Text)
    from_phase: Mapped[str | None] = mapped_column(Text)
    to_phase: Mapped[str | None] = mapped_column(Text)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
