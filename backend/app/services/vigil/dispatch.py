"""Shared-worker encrypted outbox: write + bounded mail dispatch/recovery
(issue #456, Vigil R0 P3.1; state machine flattened by issue #525, #516
finding 4).

This checkpoint builds the MECHANISM only — there is no real business
caller besides drills (#458) and cycle rounds (#459). Tests here are
internal fixture-only callers that write a row via `write_outbox_entry`
and drive `run_outbox_dispatch_sweep` directly, exactly as #456 scopes it.

State machine (#525): three row statuses — `pending` (not accepted yet,
retryable), `accepted` (provider took it), `failed` (terminal non-delivery:
provider rejection, retry window expiry, or caller cancellation). There is
no `leased` status: an in-flight attempt is a future `lease_until` on a
still-`pending` row, and no `unknown` status: a provider outcome we cannot
classify leaves the row `pending` with `next_attempt_at` set. Retry pacing
is ONE interval (`RETRY_INTERVAL`) inside ONE window (`RETRY_WINDOW` from
`created_at`), replacing the 1/5/15/60-minute, 5-attempt schedule with its
23h attempt window beside a separate 24h payload clear.

Transaction boundary (#450 Design section 6 / #456 Design comment):
`write_outbox_entry` does NOT commit — the caller's own transaction (which
also writes the domain-state change that triggered this send) covers both.
Enqueuing the Celery task after that commit is an optimization only; the
real recovery mechanism is `run_outbox_dispatch_sweep`'s periodic DB sweep
for due rows, invoked by app/tasks/vigil_tasks.py on the existing beat
schedule.

Lock order for every mutation here is User -> vigil_vaults -> vigil_outbox
(#450 Design section 3), matching every other Vigil service module. The
external HTTP call itself happens with NO lock held: lease under lock,
release (commit) the leasing transaction, call Resend, then re-acquire the
same lock order in a fresh transaction to record the outcome. This is what
lets a concurrent cancellation (disarm/replace) commit while a send is in
flight without corrupting either side (see `_finalize_attempt`'s
reload-before-write).
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
# tunable per environment (no fast-grace deployed profile, E4). Issue #525
# collapsed the retry machine to ONE interval inside ONE window — see the
# module docstring; nothing here is a per-attempt tier or an attempt cap.
LEASE_SECONDS = 60
HTTP_TIMEOUT_SECONDS = 15.0
RETRY_INTERVAL = timedelta(minutes=15)
RETRY_WINDOW = timedelta(hours=24)
MAX_ROWS_PER_SWEEP = 5

_PENDING = "pending"
_ACCEPTED = "accepted"
_FAILED = "failed"

# Provider outcomes — what ONE HTTP attempt observed, never a row status.
# An `unknown` outcome (timeout/transport/5xx/missing id) leaves the row
# `pending` with a scheduled retry; `failed` is a provider refusal.
_OUTCOME_ACCEPTED = "accepted"
_OUTCOME_FAILED = "failed"
_OUTCOME_UNKNOWN = "unknown"


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
    config_id: UUID | None,
    object_id: UUID | None,
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
    if purpose not in ("drill", "email_verify", "challenge", "release", "owner_notice"):
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


def cancel_outbox_intents(
    session: Session,
    *,
    vault_id: UUID,
    purpose: str | None = None,
    scope_ids: list[UUID] | None = None,
    config_id: UUID | None = None,
    object_id: UUID | None = None,
) -> int:
    """Cancel non-terminal outbox rows and clear their payload.

    Caller already holds the User-then-vault lock. Application-layer stop
    only — not physical erasure. #525: "cancelled" is not a status of its
    own any more, so a cancelled intent lands in the one negative terminal
    status (`failed`); the provider facts (provider_id/accepted_at) of a
    send that already went out are untouched.
    """
    conditions = [
        VigilOutbox.vault_id == vault_id,
        VigilOutbox.status == _PENDING,
    ]
    if purpose is not None:
        conditions.append(VigilOutbox.purpose == purpose)
    if scope_ids is not None:
        if not scope_ids:
            return 0
        conditions.append(VigilOutbox.scope_id.in_(scope_ids))
    if config_id is not None:
        conditions.append(VigilOutbox.config_id == config_id)
    if object_id is not None:
        conditions.append(VigilOutbox.object_id == object_id)
    result = cast(
        CursorResult[Any],
        session.execute(
            update(VigilOutbox)
            .where(and_(*conditions))
            .values(
                status=_FAILED,
                payload_cipher=None,
                payload_sha256=None,
                next_attempt_at=None,
                lease_until=None,
            )
        ),
    )
    return int(result.rowcount or 0)


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
    """Wall-clock expiry sweep (#456 A02/A03; single rule since #525) —
    pure DB work, no network, no lock beyond the row itself. Cheap enough
    to run on every sweep invocation rather than being separately
    scheduled.

    A `pending` row older than `RETRY_WINDOW` is given up on: status
    `failed`, payload cleared. That one rule covers both what used to be
    two rules ("never attempted after 24h" and "attempted, clear payload
    24h after the first attempt") — a row cannot be sent after its window
    either way, and the frozen token/body are gone with it.

    This function is the sweep; callers that need to gate access on "is
    this outbox row still usable" must ALSO check wall-clock time directly
    (row.created_at vs. now) rather than assuming this sweep has already
    run — a stopped worker delays this cleanup, and the check must not
    depend on it having executed (#456 Contract constraints).
    """
    now = now or datetime.now(UTC)

    window_cutoff = now - RETRY_WINDOW
    result = cast(
        CursorResult[Any],
        session.execute(
            update(VigilOutbox)
            .where(
                VigilOutbox.status == _PENDING,
                VigilOutbox.created_at <= window_cutoff,
            )
            .values(
                status=_FAILED,
                payload_cipher=None,
                payload_sha256=None,
                next_attempt_at=None,
                lease_until=None,
            )
        ),
    )
    return int(result.rowcount or 0)


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
    *,
    status: str,
    has_payload: bool,
    created_at: datetime,
    lease_until: datetime | None,
    next_attempt_at: datetime | None,
    now: datetime,
) -> bool:
    """The single authoritative "may this row be sent now" rule (#525).

    Evaluated under the row lock by `_lease_one`; `_lease_due_ids` is only
    a coarse prefilter, so the two cannot drift apart (the P3.1 review
    round 1 defect was a Python predicate that had to mirror a SQL one).

    `next_attempt_at IS NULL` now has exactly one meaning — never attempted,
    due now — because a row that stops being retryable leaves `pending`
    (the sweep marks it `failed`) instead of sitting in a retryable status
    with a NULL schedule.
    """
    if status != _PENDING or not has_payload:
        return False
    if created_at <= now - RETRY_WINDOW:
        return False
    if lease_until is not None and lease_until > now:
        return False
    return next_attempt_at is None or next_attempt_at <= now


def _lease_due_ids(session: Session, *, now: datetime, limit: int) -> list[UUID]:
    """Coarse candidate prefilter, oldest first. Deliberately NOT the
    authoritative due rule — `_lease_one` re-checks under the row lock."""
    return list(
        session.scalars(
            select(VigilOutbox.id)
            .where(
                VigilOutbox.status == _PENDING,
                VigilOutbox.payload_cipher.isnot(None),
                or_(VigilOutbox.lease_until.is_(None), VigilOutbox.lease_until <= now),
            )
            .order_by(VigilOutbox.created_at)
            .limit(limit)
        ).all()
    )


def _lease_one(session_factory: Callable[[], Session], outbox_id: UUID, *, now: datetime) -> bool:
    """Locks User->vault->row (in that order) and, if still due, marks it
    claimed by setting `lease_until` (status stays `pending` — #525).
    Commits (releasing every lock) before returning — the caller does the
    actual HTTP send outside any lock."""
    session = session_factory()
    try:
        row = _lock_user_vault_and_row(session, outbox_id)
        if row is None:
            session.rollback()
            return False
        if not _is_due_for_retry(
            status=row.status,
            has_payload=row.payload_cipher is not None,
            created_at=row.created_at,
            lease_until=row.lease_until,
            next_attempt_at=row.next_attempt_at,
            now=now,
        ):
            session.rollback()
            return False
        row.lease_until = now + timedelta(seconds=LEASE_SECONDS)
        session.commit()
        return True
    finally:
        session.close()


@dataclass(frozen=True)
class ProviderSendResult:
    outcome: str  # "accepted" | "failed" | "unknown" (provider outcome, not a row status)
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
        return ProviderSendResult(outcome=_OUTCOME_UNKNOWN, provider_id=None, error_code="timeout")
    except httpx.HTTPError:
        return ProviderSendResult(
            outcome=_OUTCOME_UNKNOWN, provider_id=None, error_code="transport_error"
        )

    if resp.status_code >= 500:
        return ProviderSendResult(
            outcome=_OUTCOME_UNKNOWN, provider_id=None, error_code=f"http_{resp.status_code}"
        )
    if resp.status_code >= 400:
        return ProviderSendResult(
            outcome=_OUTCOME_FAILED, provider_id=None, error_code=f"http_{resp.status_code}"
        )

    try:
        provider_id = resp.json().get("id")
    except ValueError:
        provider_id = None
    if isinstance(provider_id, str) and provider_id:
        return ProviderSendResult(
            outcome=_OUTCOME_ACCEPTED, provider_id=provider_id, error_code=None
        )
    return ProviderSendResult(
        outcome=_OUTCOME_UNKNOWN, provider_id=None, error_code="missing_provider_id"
    )


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
    longer `pending` by us: no flipping a terminal status back, no
    restoring a cleared payload.
    """
    session = session_factory()
    try:
        row = _lock_user_vault_and_row(session, outbox_id)
        if row is None:
            session.rollback()
            return

        if row.status != _PENDING:
            # Raced: cancelled (or otherwise moved on) while the HTTP call
            # was in flight. Record the provider fact for the historical
            # record ONLY if we actually got one — never touch status or
            # payload_cipher; a cleared payload stays cleared, a failed row
            # stays failed.
            if result.outcome == _OUTCOME_ACCEPTED and row.provider_id is None:
                row.provider_id = result.provider_id
                row.accepted_at = now
            session.commit()
            return

        attempts = row.attempts + 1
        row.attempts = attempts
        if row.first_attempt_at is None:
            row.first_attempt_at = now
        row.lease_until = None

        if result.outcome == _OUTCOME_ACCEPTED:
            row.status = _ACCEPTED
            row.provider_id = result.provider_id
            row.accepted_at = now
            row.payload_cipher = None
            row.payload_sha256 = None
            row.next_attempt_at = None
        elif result.outcome == _OUTCOME_FAILED:
            row.status = _FAILED
            row.last_error_code = result.error_code
            row.payload_cipher = None
            row.payload_sha256 = None
            row.next_attempt_at = None
        else:  # unknown — stay pending, retry once per RETRY_INTERVAL
            row.last_error_code = result.error_code
            row.next_attempt_at = now + RETRY_INTERVAL

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
            "tags": [{"name": "vigil_outbox_id", "value": str(outbox_id)}],
        }
        result = send_fn(body, idempotency_key)
        _finalize_attempt(session_factory, outbox_id, result, now=now)
        sent += 1

    assoc_session = session_factory()
    try:
        from app.services.vigil.delivery import associate_unmatched_events

        associate_unmatched_events(assoc_session)
        assoc_session.commit()
    finally:
        assoc_session.close()

    return DispatchSweepSummary(expired=expired, leased=leased, sent=sent)
