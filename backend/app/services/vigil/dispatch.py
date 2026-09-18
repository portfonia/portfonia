"""Shared-worker encrypted outbox: write + bounded mail dispatch/recovery
(issue #456, Vigil R0 P3.1).

This checkpoint builds the MECHANISM only — there is no real business
caller yet (drill/arm is #458, three-round escalation is #459). Tests here
are internal fixture-only callers that write a row via `write_outbox_entry`
and drive `run_outbox_dispatch_sweep` directly, exactly as #456 scopes it.

Transaction boundary (#450 Design section 6 / #456 Design comment):
`write_outbox_entry` does NOT commit — the caller's own transaction (which
also writes the domain-state change that triggered this send) covers both.
Enqueuing the Celery task after that commit is an optimization only; the
real recovery mechanism is `run_outbox_dispatch_sweep`'s periodic DB sweep
for due/stale rows, invoked by app/tasks/vigil_tasks.py on the existing
beat schedule.

Lock order for every mutation here is User -> vigil_vaults -> vigil_outbox
(#450 Design section 3), matching every other Vigil service module. The
external HTTP call itself happens with NO lock held: lease under lock,
release (commit) the leasing transaction, call Resend, then re-acquire the
same lock order in a fresh transaction to record the outcome. This is what
lets a concurrent cancellation (once a real cancel path exists) commit
while a send is in flight without corrupting either side (see
`_finalize_attempt`'s reload-before-write).
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

import httpx
from sqlalchemy import and_, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.user import User
from app.models.vigil import VigilOutbox, VigilVault
from app.services.vigil.crypto import decrypt_notification_field, encrypt_notification_field

logger = logging.getLogger(__name__)

_PAYLOAD_PURPOSE = "vigil_outbox_payload"
_RESEND_SEND_URL = "https://api.resend.com/emails"

# #450 Design section 6 / Contract constraints — exact thresholds, not
# tunable per environment (no fast-grace deployed profile, E4).
LEASE_SECONDS = 60
HTTP_TIMEOUT_SECONDS = 15.0
MAX_ATTEMPTS = 5
RETRY_SCHEDULE_MINUTES = (1, 5, 15, 60)
MAX_ATTEMPT_WINDOW = timedelta(hours=23)
NEVER_ATTEMPTED_CANCEL_AFTER = timedelta(hours=24)
UNKNOWN_PAYLOAD_CLEAR_AFTER = timedelta(hours=24)
MAX_ROWS_PER_SWEEP = 5

_PENDING = "pending"
_LEASED = "leased"
_ACCEPTED = "accepted"
_FAILED = "failed"
_UNKNOWN = "unknown"
_CANCELLED = "cancelled"
_RETRYABLE_STATUSES = (_PENDING, _UNKNOWN)
_TERMINAL_STATUSES = (_ACCEPTED, _FAILED, _CANCELLED)


class VigilOutboxError(RuntimeError):
    """Caller error writing an outbox row (unknown/mismatched context)."""


@dataclass(frozen=True)
class OutboxPayload:
    from_addr: str
    to_addr: str
    subject: str
    text: str
    html: str
    token: str
    scope_id: str
    purpose: str
    recipient_index: int | None


def _canonical_payload_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def write_outbox_entry(
    session: Session,
    *,
    vault: VigilVault,
    config_id: UUID,
    object_id: UUID,
    scope_id: UUID,
    purpose: str,
    dedup_key: str,
    recipient_email: str,
    subject: str,
    text_body: str,
    html_body: str,
    token: str,
    recipient_index: int | None = None,
) -> VigilOutbox:
    """Insert one `vigil_outbox` row bound to `vault`. The CALLER already
    holds the User->vault lock for this transaction (`with_for_update()`)
    as part of whatever domain-state change this send accompanies — this
    function does not lock anything itself, matching
    services/vigil/objects.py's `init_object`/`upload_object` convention of
    locking once per request, not per helper call.

    Does not commit. Does not enqueue the Celery task — the caller does
    that (best-effort) after its own commit succeeds; the periodic sweep
    is the real recovery path if the enqueue is lost.
    """
    if purpose not in ("drill", "challenge", "release", "owner_notice"):
        raise VigilOutboxError(f"unknown outbox purpose: {purpose!r}")

    outbox_id = uuid.uuid4()
    settings = get_settings()
    payload = {
        "schema": 1,
        "from": settings.EMAIL_FROM,
        "to": recipient_email,
        "subject": subject,
        "text": text_body,
        "html": html_body,
        "token": token,
        "scope_id": str(scope_id),
        "purpose": purpose,
        "recipient_index": recipient_index,
    }
    payload_sha256 = hashlib.sha256(_canonical_payload_json(payload).encode()).hexdigest()
    payload_cipher = encrypt_notification_field(
        _canonical_payload_json(payload),
        purpose=_PAYLOAD_PURPOSE,
        table="vigil_outbox",
        row_id=outbox_id,
        vault_id=vault.id,
    )

    row = VigilOutbox(
        id=outbox_id,
        vault_id=vault.id,
        config_id=config_id,
        object_id=object_id,
        scope_id=scope_id,
        purpose=purpose,
        dedup_key=dedup_key,
        payload_cipher=payload_cipher,
        payload_sha256=payload_sha256,
        status=_PENDING,
        attempts=0,
        recipient_index=recipient_index,
    )
    session.add(row)
    session.flush()
    return row


def _decrypt_payload(row: VigilOutbox) -> OutboxPayload:
    raw = decrypt_notification_field(
        row.payload_cipher,  # type: ignore[arg-type]
        purpose=_PAYLOAD_PURPOSE,
        table="vigil_outbox",
        row_id=row.id,
        vault_id=row.vault_id,
    )
    payload = json.loads(raw)
    return OutboxPayload(
        from_addr=payload["from"],
        to_addr=payload["to"],
        subject=payload["subject"],
        text=payload["text"],
        html=payload["html"],
        token=payload["token"],
        scope_id=payload["scope_id"],
        purpose=payload["purpose"],
        recipient_index=payload["recipient_index"],
    )


def _lock_user_and_vault(session: Session, vault_id: UUID) -> tuple[User, VigilVault] | None:
    vault = session.get(VigilVault, vault_id)
    if vault is None:
        return None
    user = session.execute(
        select(User).where(User.id == vault.owner_user_id).with_for_update()
    ).scalar_one_or_none()
    if user is None:
        return None
    locked_vault = session.execute(
        select(VigilVault).where(VigilVault.id == vault_id).with_for_update()
    ).scalar_one()
    return user, locked_vault


def sweep_expired_outbox(session: Session, *, now: datetime | None = None) -> int:
    """Wall-clock expiry sweep (#456 A02/A03) — pure DB work, no network,
    no lock beyond the row itself. Cheap enough to run on every sweep
    invocation rather than being separately scheduled.

    A never-attempted row is cancelled 24h after creation. A row stuck in
    `unknown` has its payload nulled 24h after its first attempt (it stops
    being retried well before that — see `_next_attempt_after` — but the
    payload itself lingers, retained for potential reconciliation, until
    this wall-clock deadline). This function is the sweep; callers that
    need to gate access on "is this outbox row still usable" must ALSO
    check wall-clock time directly (row.created_at/first_attempt_at vs.
    now) rather than assuming this sweep has already run — a stopped
    worker delays this cleanup, and the check must not depend on it having
    executed (#456 Contract constraints).
    """
    now = now or datetime.now(UTC)

    never_attempted_cutoff = now - NEVER_ATTEMPTED_CANCEL_AFTER
    result_a = cast(
        CursorResult[Any],
        session.execute(
            update(VigilOutbox)
            .where(
                VigilOutbox.status.in_((_PENDING, _LEASED)),
                VigilOutbox.first_attempt_at.is_(None),
                VigilOutbox.created_at <= never_attempted_cutoff,
            )
            .values(
                status=_CANCELLED, payload_cipher=None, payload_sha256=None, next_attempt_at=None
            )
        ),
    )

    unknown_cutoff = now - UNKNOWN_PAYLOAD_CLEAR_AFTER
    result_b = cast(
        CursorResult[Any],
        session.execute(
            update(VigilOutbox)
            .where(
                VigilOutbox.status == _UNKNOWN,
                VigilOutbox.first_attempt_at.isnot(None),
                VigilOutbox.first_attempt_at <= unknown_cutoff,
                VigilOutbox.payload_cipher.isnot(None),
            )
            .values(payload_cipher=None, payload_sha256=None, next_attempt_at=None)
        ),
    )
    return int(result_a.rowcount or 0) + int(result_b.rowcount or 0)


def _lock_user_vault_and_row(session: Session, outbox_id: UUID) -> VigilOutbox | None:
    """Locks User -> vigil_vaults -> vigil_outbox, in that exact order
    (#450 Design section 3 / blacktomb42 PR #510 review round 1: an
    earlier version locked the outbox row first, backwards from every
    other Vigil service module's lock order). The initial unlocked read
    only discovers which vault this row belongs to — `vault_id` is never
    reassigned after insert, so a stale read here cannot point the
    subsequent locks at the wrong vault."""
    peek = session.get(VigilOutbox, outbox_id)
    if peek is None:
        return None
    locked = _lock_user_and_vault(session, peek.vault_id)
    if locked is None:
        return None
    return session.execute(
        select(VigilOutbox).where(VigilOutbox.id == outbox_id).with_for_update()
    ).scalar_one_or_none()


def _is_due_for_retry(
    *, status: str, has_payload: bool, next_attempt_at: datetime | None, now: datetime
) -> bool:
    """Must mirror `_lease_due_ids`'s SQL `due_retry` predicate exactly.

    `next_attempt_at IS NULL` means two different things depending on
    status: for a never-tried PENDING row it means "due immediately" (no
    attempt has scheduled a delay yet); for an UNKNOWN row it means
    retries are EXHAUSTED (`_next_attempt_after` returned None because the
    attempt ceiling or the 23h window was hit) — treating that the same as
    "due now" would re-send an already-exhausted row on every sweep until
    `sweep_expired_outbox`'s 24h clear finally removes its payload
    (blacktomb42 PR #510 review round 1, P3.1-A02).
    """
    if status not in _RETRYABLE_STATUSES or not has_payload:
        return False
    if next_attempt_at is not None:
        return next_attempt_at <= now
    return status == _PENDING


def _lease_due_ids(session: Session, *, now: datetime, limit: int) -> list[UUID]:
    stuck_lease = and_(VigilOutbox.status == _LEASED, VigilOutbox.lease_until <= now)
    due_retry = and_(
        VigilOutbox.status.in_(_RETRYABLE_STATUSES),
        VigilOutbox.payload_cipher.isnot(None),
        or_(
            and_(VigilOutbox.next_attempt_at.isnot(None), VigilOutbox.next_attempt_at <= now),
            and_(VigilOutbox.status == _PENDING, VigilOutbox.next_attempt_at.is_(None)),
        ),
    )
    return list(
        session.scalars(
            select(VigilOutbox.id)
            .where(or_(due_retry, stuck_lease))
            .order_by(VigilOutbox.created_at)
            .limit(limit)
        ).all()
    )


def _lease_one(session_factory: Callable[[], Session], outbox_id: UUID, *, now: datetime) -> bool:
    """Locks User->vault->row (in that order) and, if still due, marks it
    `leased`. Commits (releasing every lock) before returning — the caller
    does the actual HTTP send outside any lock."""
    session = session_factory()
    try:
        row = _lock_user_vault_and_row(session, outbox_id)
        if row is None:
            session.rollback()
            return False
        is_stuck_lease = (
            row.status == _LEASED and row.lease_until is not None and row.lease_until <= now
        )
        is_due_retry = _is_due_for_retry(
            status=row.status,
            has_payload=row.payload_cipher is not None,
            next_attempt_at=row.next_attempt_at,
            now=now,
        )
        if not (is_stuck_lease or is_due_retry):
            session.rollback()
            return False
        row.status = _LEASED
        row.lease_until = now + timedelta(seconds=LEASE_SECONDS)
        session.commit()
        return True
    finally:
        session.close()


@dataclass(frozen=True)
class ProviderSendResult:
    outcome: str  # "accepted" | "failed" | "unknown"
    provider_id: str | None
    error_code: str | None


SendFn = Callable[[dict[str, Any], str], ProviderSendResult]


def _default_send(body: dict[str, Any], idempotency_key: str) -> ProviderSendResult:
    settings = get_settings()
    api_key = settings.RESEND_API_KEY.get_secret_value()
    try:
        with httpx.Client(timeout=HTTP_TIMEOUT_SECONDS) as client:
            resp = client.post(
                _RESEND_SEND_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "Idempotency-Key": idempotency_key,
                },
                json=body,
            )
    except httpx.TimeoutException:
        return ProviderSendResult(outcome=_UNKNOWN, provider_id=None, error_code="timeout")
    except httpx.HTTPError:
        return ProviderSendResult(outcome=_UNKNOWN, provider_id=None, error_code="transport_error")

    if resp.status_code >= 500:
        return ProviderSendResult(
            outcome=_UNKNOWN, provider_id=None, error_code=f"http_{resp.status_code}"
        )
    if resp.status_code >= 400:
        return ProviderSendResult(
            outcome=_FAILED, provider_id=None, error_code=f"http_{resp.status_code}"
        )

    try:
        provider_id = resp.json().get("id")
    except ValueError:
        provider_id = None
    if isinstance(provider_id, str) and provider_id:
        return ProviderSendResult(outcome=_ACCEPTED, provider_id=provider_id, error_code=None)
    return ProviderSendResult(outcome=_UNKNOWN, provider_id=None, error_code="missing_provider_id")


def _next_attempt_after(
    *, attempts: int, first_attempt_at: datetime, now: datetime
) -> datetime | None:
    """`attempts` is the count AFTER this attempt. Returns None when
    retries are exhausted (max attempts reached, or the next scheduled
    attempt would land past first_attempt_at + 23h)."""
    if attempts >= MAX_ATTEMPTS or attempts > len(RETRY_SCHEDULE_MINUTES):
        return None
    delay = timedelta(minutes=RETRY_SCHEDULE_MINUTES[attempts - 1])
    candidate = now + delay
    if candidate - first_attempt_at > MAX_ATTEMPT_WINDOW:
        return None
    return candidate


def _finalize_attempt(
    session_factory: Callable[[], Session],
    outbox_id: UUID,
    result: ProviderSendResult,
    *,
    now: datetime,
) -> None:
    """Re-acquires the User->vault->row lock order and reloads current
    state before recording the outcome (#456 post-send finalization —
    "a cancellation or revocation may have committed DURING the HTTP
    call"). A late `accepted` is always recorded as a historical fact
    (provider_id/accepted_at), but never reactivates a row that is no
    longer `leased` by us: no flipping a terminal status back, no
    restoring a cleared payload.
    """
    session = session_factory()
    try:
        row = _lock_user_vault_and_row(session, outbox_id)
        if row is None:
            session.rollback()
            return

        if row.status != _LEASED:
            # Raced: cancelled (or otherwise moved on) while the HTTP call
            # was in flight. Record the provider fact for the historical
            # record ONLY if we actually got one — never touch status or
            # payload_cipher; a cleared payload stays cleared, a cancelled
            # row stays cancelled.
            if result.outcome == _ACCEPTED and row.provider_id is None:
                row.provider_id = result.provider_id
                row.accepted_at = now
            session.commit()
            return

        attempts = row.attempts + 1
        row.attempts = attempts
        if row.first_attempt_at is None:
            row.first_attempt_at = now

        if result.outcome == _ACCEPTED:
            row.status = _ACCEPTED
            row.provider_id = result.provider_id
            row.accepted_at = now
            row.payload_cipher = None
            row.payload_sha256 = None
            row.next_attempt_at = None
        elif result.outcome == _FAILED:
            row.status = _FAILED
            row.last_error_code = result.error_code
            row.payload_cipher = None
            row.payload_sha256 = None
            row.next_attempt_at = None
        else:  # unknown
            row.status = _UNKNOWN
            row.last_error_code = result.error_code
            row.next_attempt_at = _next_attempt_after(
                attempts=attempts, first_attempt_at=row.first_attempt_at, now=now
            )
            # Payload is NOT cleared here even if retries are exhausted —
            # sweep_expired_outbox owns the 24h-since-first-attempt clear
            # (A02), so a request-path check that runs before the next
            # sweep still sees a payload and must rely on its OWN
            # wall-clock check, not payload presence, to decide validity.

        session.commit()
    finally:
        session.close()


@dataclass(frozen=True)
class DispatchSweepSummary:
    expired: int
    leased: int
    sent: int


def run_outbox_dispatch_sweep(
    session_factory: Callable[[], Session],
    *,
    send_fn: SendFn = _default_send,
    now: datetime | None = None,
    limit: int = MAX_ROWS_PER_SWEEP,
) -> DispatchSweepSummary:
    """The Celery task's entire body (app/tasks/vigil_tasks.py). Processes
    at most `limit` (default 5) due rows per call — no sleeping, no long
    ETA chain (#456 non-goal / A04)."""
    now = now or datetime.now(UTC)

    session = session_factory()
    try:
        expired = sweep_expired_outbox(session, now=now)
        session.commit()
    finally:
        session.close()

    candidate_session = session_factory()
    try:
        candidate_ids = _lease_due_ids(candidate_session, now=now, limit=limit)
    finally:
        candidate_session.close()

    sent = 0
    leased = 0
    for outbox_id in candidate_ids:
        if not _lease_one(session_factory, outbox_id, now=now):
            continue
        leased += 1

        # Reload the payload under a short-lived session — decrypting is
        # CPU-only, no lock needed once we hold the lease.
        read_session = session_factory()
        try:
            row = read_session.get(VigilOutbox, outbox_id)
            if row is None or row.payload_cipher is None:
                continue
            payload = _decrypt_payload(row)
        finally:
            read_session.close()

        idempotency_key = f"vigil/{outbox_id}"
        body = {
            "from": payload.from_addr,
            "to": [payload.to_addr],
            "subject": payload.subject,
            "text": payload.text,
            "html": payload.html,
        }
        result = send_fn(body, idempotency_key)
        _finalize_attempt(session_factory, outbox_id, result, now=now)
        sent += 1

    return DispatchSweepSummary(expired=expired, leased=leased, sent=sent)
