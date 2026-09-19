from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    LargeBinary,
    SmallInteger,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

# Vigil R0 P1.1 (issue #451) added the base schema: vigil_vaults,
# vigil_runtime, vigil_audit_events. Issue #454 (P2.1) adds
# vigil_configurations/vigil_objects and upgrades vaults'
# active/pending_config/object_id columns from plain nullable UUIDs to real
# FKs now that their target tables exist. Field/constraint contract is #450
# Design section 4 / Vigil_R0_Dev.md Appendix A, incorporated by reference —
# do not diverge from it without updating that comment. Later checkpoints
# (#458+) add the rest of the vigil_* tables (cycles/rounds/outbox/...); this
# file only grows alongside the checkpoint that actually needs each one.

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
VALID_VIGIL_CONFIGURATION_STATUSES = ("pending", "active", "retired")
VALID_VIGIL_OBJECT_STATUSES = ("staging", "ready", "active", "retired", "deleted")
VIGIL_OBJECT_MAX_PLAINTEXT_SIZE = 10_000_000
VALID_VIGIL_OUTBOX_PURPOSES = ("drill", "challenge", "release", "owner_notice")
VALID_VIGIL_OUTBOX_STATUSES = ("pending", "accepted", "failed")
VALID_VIGIL_DELIVERY_EVIDENCE_SOURCES = ("webhook", "poll")
VALID_VIGIL_ACTION_TOKEN_PURPOSES = ("drill", "cycle_confirm", "owner_revoke")
VALID_VIGIL_CYCLE_STATUSES = ("active", "confirmed", "released", "cancelled")
# AES-256-GCM tag length (bytes) the browser-produced ciphertext always
# carries appended — #450 Design section 5 / Vigil_R0_Dev.md §3.
VIGIL_OBJECT_GCM_TAG_LENGTH = 16


def _in_list_sql(column: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{v}'" for v in sorted(values))
    return f"{column} IN ({quoted})"


class VigilVault(Base):
    """Per-owner Vigil vault row — one per user (issue #451).

    Business logic (arm/release/replace/etc.) is not implemented yet, but
    since #454 (P2.1) `active_config_id`/`active_object_id`/
    `pending_config_id`/`pending_object_id` are real FKs into
    vigil_configurations/vigil_objects (RESTRICT — a purge must null these
    out before deleting the rows they point at; see
    services/user_purge.py). No route sets active_* yet (#458 arm);
    pending_* is set by POST /vigil/configurations and
    POST /vigil/objects/init.
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
    active_config_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("vigil_configurations.id", ondelete="RESTRICT")
    )
    active_object_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("vigil_objects.id", ondelete="RESTRICT")
    )
    pending_config_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("vigil_configurations.id", ondelete="RESTRICT")
    )
    pending_object_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("vigil_objects.id", ondelete="RESTRICT")
    )
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
    """DEPRECATED / UNUSED (issue #527, #516 finding 6) — do not write rows.

    Vigil's own audit-log table was retired as an over-engineered duplicate
    of the generic `operational_events` store (issue #446): no application
    code reads a row here, and every transition it used to
    record is already durable in the business rows (`vigil_vaults` phase/
    revision/hold_reason/first_armed_at/last_owner_confirmed_at,
    `vigil_rounds` anchor/deadline, `vigil_action_tokens` confirmed/used
    timestamps). The table and its constraints stay as-is — the schema is
    not migrated and historical rows are abandoned in place, never
    backfilled. `purge_user` still deletes rows for the vault it removes,
    because `vault_id` is ON DELETE RESTRICT; a physical DROP TABLE is a
    separate decision (no forensic need confirmed yet).

    If a Vigil event ever does need a durable record again, it goes through
    `app.core.operational_events`, not this table.
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


class VigilConfiguration(Base):
    """Versioned configuration candidate for a vault (issue #454, P2.1).

    `data_cipher` is a services.vigil.crypto contextual envelope wrapping
    the strict business JSON {interval_days, grace_hours, account_email,
    recipients:[{position,email}], message} — never a plain encrypted
    scalar. `id` is a Python-side UUID (not server-generated) because the
    crypto envelope must bind `row_id` to this row's own id *before* the
    INSERT that stores it — the caller allocates the id, builds
    `data_cipher` from it, then constructs this row with that id.

    Address uniqueness/count within `recipients` is enforced after decrypt
    under the vault lock (services/vigil/configuration.py), not at the SQL
    level — recipients live inside `data_cipher`, not a separate column.
    """

    __tablename__ = "vigil_configurations"
    __table_args__ = (
        UniqueConstraint(
            "vault_id", "config_revision", name="uq_vigil_configurations_vault_id_config_revision"
        ),
        CheckConstraint(_in_list_sql("status", VALID_VIGIL_CONFIGURATION_STATUSES), name="status"),
        Index(
            "uq_vigil_configurations_one_active",
            "vault_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
        Index(
            "uq_vigil_configurations_one_pending",
            "vault_id",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    vault_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("vigil_vaults.id", ondelete="RESTRICT"), nullable=False
    )
    config_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    data_cipher: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )


class VigilObject(Base):
    """Versioned file candidate for a vault (issue #454, P2.1).

    Same id-allocation rationale as VigilConfiguration: `id` is a
    Python-side UUID so services/vigil/objects.py can allocate it, return
    it to the caller from POST /vigil/objects/init, and only later (a
    separate POST /vigil/objects/upload call) fill in the crypto/ciphertext
    columns bound to that same id.

    `staging` rows have every crypto/ciphertext column NULL (no upload yet);
    `ready`/`active` rows must have all of them populated with
    octet_length(ciphertext) == plaintext_size + 16 — enforced by the
    `crypto_fields_present_when_ready` CHECK below rather than left to
    application code, per this project's "table boundary = concurrency/
    invariant boundary" convention (Vigil Concept & Design.md appendix C.1).
    `retired`/`deleted` rows have ciphertext/outer_cipher set back to NULL
    (an application-layer tombstone only — see #450 Design section 7 for the
    physical-storage boundary this does NOT claim).
    """

    __tablename__ = "vigil_objects"
    __table_args__ = (
        UniqueConstraint("vault_id", "request_id", name="uq_vigil_objects_vault_id_request_id"),
        CheckConstraint(_in_list_sql("status", VALID_VIGIL_OBJECT_STATUSES), name="status"),
        CheckConstraint(
            f"plaintext_size >= 0 AND plaintext_size <= {VIGIL_OBJECT_MAX_PLAINTEXT_SIZE}",
            name="plaintext_size_bounds",
        ),
        CheckConstraint(
            "status NOT IN ('ready', 'active') OR ("
            "ciphertext_size IS NOT NULL AND cipher_sha256 IS NOT NULL "
            "AND manifest IS NOT NULL AND outer_cipher IS NOT NULL "
            "AND ciphertext IS NOT NULL "
            f"AND octet_length(ciphertext) = plaintext_size + {VIGIL_OBJECT_GCM_TAG_LENGTH}"
            ")",
            name="crypto_fields_present_when_ready",
        ),
        Index(
            "uq_vigil_objects_one_active",
            "vault_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
        Index(
            "uq_vigil_objects_one_pending",
            "vault_id",
            unique=True,
            postgresql_where=text("status IN ('staging', 'ready')"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    vault_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("vigil_vaults.id", ondelete="RESTRICT"), nullable=False
    )
    config_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("vigil_configurations.id", ondelete="RESTRICT"), nullable=False
    )
    request_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    filename_cipher: Mapped[str] = mapped_column(Text, nullable=False)
    plaintext_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    ciphertext_size: Mapped[int | None] = mapped_column(BigInteger)
    cipher_sha256: Mapped[str | None] = mapped_column(Text)
    manifest: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    outer_cipher: Mapped[str | None] = mapped_column(Text)
    ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    activated_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )


class VigilOutbox(Base):
    """Shared-worker encrypted mail intent (issue #456, P3.1).

    `scope_id` identifies the business event this send belongs to (drill
    token id, round id, later a release-batch id). It stays a plain UUID,
    not a FK: drill/cycle/round rows already exist, but release_batches
    do not until #460, and a polymorphic association is still not wanted.

    `payload_cipher`/`payload_sha256` hold the frozen recipient snapshot +
    mail body + token under `VIGIL_NOTIFICATION_KEY` (never the data key —
    services/vigil/crypto.py's `encrypt_field` always selects
    VIGIL_ENCRYPTION_KEY, so dispatch.py uses its own notification-key
    Fernet builder). Both columns are cleared when the intent reaches a
    terminal status — `accepted`, or `failed` (provider refusal, caller
    cancellation, or retry-window expiry). The three-status machine and its
    single retry interval/window are described in
    services/vigil/dispatch.py's module docstring (#525 flattened the
    6-status, multi-tier schedule; `leased`/`unknown`/`cancelled` no
    longer exist, and an in-flight attempt is a future `lease_until` on a
    still-`pending` row).

    `dedup_key` is UNIQUE together with a non-null `provider_id`
    (partial unique index below) per #450 Design section 4/6: two outbox
    rows can share a caller-chosen `dedup_key` only until one of them
    actually gets a provider id, at which point a second provider-accepted
    send under the same key would be a real duplicate.
    """

    __tablename__ = "vigil_outbox"
    __table_args__ = (
        UniqueConstraint("dedup_key", name="uq_vigil_outbox_dedup_key"),
        CheckConstraint(_in_list_sql("purpose", VALID_VIGIL_OUTBOX_PURPOSES), name="purpose"),
        CheckConstraint(_in_list_sql("status", VALID_VIGIL_OUTBOX_STATUSES), name="status"),
        CheckConstraint(
            "recipient_index IS NULL OR recipient_index BETWEEN 1 AND 3",
            name="recipient_index_bounds",
        ),
        CheckConstraint("attempts >= 0", name="attempts_nonneg"),
        Index(
            "uq_vigil_outbox_provider_id",
            "provider_id",
            unique=True,
            postgresql_where=text("provider_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    vault_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("vigil_vaults.id", ondelete="RESTRICT"), nullable=False
    )
    config_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("vigil_configurations.id", ondelete="RESTRICT"), nullable=False
    )
    object_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("vigil_objects.id", ondelete="RESTRICT"), nullable=False
    )
    scope_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    dedup_key: Mapped[str] = mapped_column(Text, nullable=False)
    payload_cipher: Mapped[str | None] = mapped_column(Text)
    payload_sha256: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pending'"))
    attempts: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    lease_until: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    next_attempt_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    first_attempt_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    provider_id: Mapped[str | None] = mapped_column(Text)
    accepted_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(Text)
    recipient_index: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )


class VigilDeliveryEvent(Base):
    """Signed provider delivery facts (issue #457, P3.2).

    UNIQUE `provider_event_id` is the Svix/Resend event id (webhook). Rows
    written before issue #526 (#516 finding 3) removed the bounded
    5/15/30-minute delivery poll may carry a legacy synthetic
    `poll:{outbox_id}:{window}` key instead — nothing writes that shape
    anymore. `outbox_id` is nullable: unmatched events (wrong product,
    webhook-before-send-response, shared-provider report mail) are kept
    for later association and never credited to a vault. No raw body,
    token, or personal message is stored; an address is retained only as
    a notification-key ciphertext bound to this row.
    """

    __tablename__ = "vigil_delivery_events"
    __table_args__ = (
        UniqueConstraint("provider_event_id", name="uq_vigil_delivery_events_provider_event_id"),
        CheckConstraint(
            _in_list_sql("evidence_source", VALID_VIGIL_DELIVERY_EVIDENCE_SOURCES),
            name="evidence_source",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    provider_event_id: Mapped[str] = mapped_column(Text, nullable=False)
    outbox_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("vigil_outbox.id", ondelete="RESTRICT")
    )
    provider_message_id: Mapped[str] = mapped_column(Text, nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    provider_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    received_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    evidence_source: Mapped[str] = mapped_column(Text, nullable=False)
    address_cipher: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )


class VigilActionToken(Base):
    """Purpose-bound link token, hash-only (issue #458, P2.3; cycle FK #459).

    `purpose=drill` is written by #458; `purpose=cycle_confirm` and
    `cycle_id` (FK to vigil_cycles) are written by #459. `batch_id` stays
    a nullable UUID without an FK until #460 creates release_batches.
    """

    __tablename__ = "vigil_action_tokens"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_vigil_action_tokens_token_hash"),
        CheckConstraint(_in_list_sql("purpose", VALID_VIGIL_ACTION_TOKEN_PURPOSES), name="purpose"),
        Index(
            "uq_vigil_action_tokens_one_pending_drill",
            "config_id",
            "object_id",
            unique=True,
            postgresql_where=text(
                "purpose = 'drill' AND confirmed_at IS NULL AND used_at IS NULL "
                "AND invalidated_at IS NULL"
            ),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    vault_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("vigil_vaults.id", ondelete="RESTRICT"), nullable=False
    )
    config_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("vigil_configurations.id", ondelete="RESTRICT"), nullable=False
    )
    object_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("vigil_objects.id", ondelete="RESTRICT"), nullable=False
    )
    cycle_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("vigil_cycles.id", ondelete="RESTRICT")
    )
    batch_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    token_hash: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    invalidated_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    confirmed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )


class VigilConsumedNonce(Base):
    """One-time public-action nonce consumption (issue #458, P2.3).

    jti is the PK: inserting it in the action transaction is what consumes
    the nonce. GET /vigil/public/status must never write this table.
    """

    __tablename__ = "vigil_consumed_nonces"

    jti: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    token_hash: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    used_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)


class VigilCycle(Base):
    """One confirmation cycle for an armed arrangement (issue #459, P3.3).

    At most one `active` row per vault. `current_level` is 1..3;
    RELEASED is a later checkpoint (P4.1) and is not written here.
    """

    __tablename__ = "vigil_cycles"
    __table_args__ = (
        CheckConstraint(_in_list_sql("status", VALID_VIGIL_CYCLE_STATUSES), name="status"),
        CheckConstraint("current_level >= 1 AND current_level <= 3", name="current_level_bounds"),
        Index(
            "uq_vigil_cycles_one_active",
            "vault_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    vault_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("vigil_vaults.id", ondelete="RESTRICT"), nullable=False
    )
    config_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("vigil_configurations.id", ondelete="RESTRICT"), nullable=False
    )
    object_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("vigil_objects.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(Text, nullable=False)
    current_level: Mapped[int] = mapped_column(BigInteger, nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )


class VigilRound(Base):
    """Persisted confirmation window for one cycle level/generation (#459).

    Both `anchor_at` and `deadline_at` are null, or both are present.
    Superseded generations stay so three windows can be reconstructed.
    """

    __tablename__ = "vigil_rounds"
    __table_args__ = (
        UniqueConstraint(
            "cycle_id", "level", "generation", name="uq_vigil_rounds_cycle_id_level_generation"
        ),
        CheckConstraint("level >= 1 AND level <= 3", name="level_bounds"),
        CheckConstraint("generation >= 1", name="generation_nonneg"),
        CheckConstraint(
            "(anchor_at IS NULL AND deadline_at IS NULL) "
            "OR (anchor_at IS NOT NULL AND deadline_at IS NOT NULL)",
            name="anchor_deadline_pair",
        ),
        Index(
            "uq_vigil_rounds_one_open_generation",
            "cycle_id",
            "level",
            unique=True,
            postgresql_where=text("superseded_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    cycle_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("vigil_cycles.id", ondelete="RESTRICT"), nullable=False
    )
    level: Mapped[int] = mapped_column(BigInteger, nullable=False)
    generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    outbox_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("vigil_outbox.id", ondelete="RESTRICT"), nullable=False
    )
    anchor_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    deadline_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    superseded_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
