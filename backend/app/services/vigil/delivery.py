"""Verified delivery evidence for Vigil feature mail (issue #457, P3.2).

This checkpoint is the evidence layer only: persist signed, correlated
facts and return {usable, anchor, reason}. Deadline/state transitions
belong to #459 (P3.3) and are not implemented here.

Webhook verification is local HMAC via resend.Webhooks.verify (Svix
algorithm, 300s timestamp tolerance) — no network. Bounded 5/15/30-minute
polling of GET /emails/{id} is the only outbound call, mocked in tests.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast
from uuid import UUID

import httpx
import resend
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.user import User
from app.models.vigil import VigilDeliveryEvent, VigilOutbox, VigilVault
from app.services.vigil.crypto import VigilCryptoError, encrypt_notification_field

logger = logging.getLogger(__name__)

_ADDRESS_PURPOSE = "vigil_delivery_address"
_RESEND_EMAIL_URL = "https://api.resend.com/emails/{id}"
HTTP_TIMEOUT_SECONDS = 15.0
PROVIDER_FUTURE_SKEW = timedelta(minutes=5)
POLL_WINDOWS_MINUTES = (5, 15, 30)
MAX_POLL_ROWS_PER_SWEEP = 5

_NEGATIVE_TYPES = frozenset(
    {
        "email.bounced",
        "email.complained",
        "email.failed",
        "email.suppressed",
        "bounced",
        "complained",
        "failed",
        "suppressed",
    }
)
_DELIVERED_TYPES = frozenset({"email.delivered", "delivered"})
IngestStatus = Literal["stored", "duplicate", "ignored"]


@dataclass(frozen=True)
class DeliveryEvidence:
    usable: bool
    anchor_at: datetime | None
    reason: str


@dataclass(frozen=True)
class DeliveryPollSummary:
    polled: int


def _normalize_addr(value: str) -> str:
    trimmed = value.strip()
    if "@" not in trimmed:
        return trimmed.casefold()
    local, _, domain = trimmed.partition("@")
    return f"{local}@{domain.casefold()}"


def _event_type_is_negative(event_type: str) -> bool:
    return event_type in _NEGATIVE_TYPES


def _event_type_is_delivered(event_type: str) -> bool:
    return event_type in _DELIVERED_TYPES


def _is_provider_time_mismatch(*, provider_at: datetime | None, received_at: datetime) -> bool:
    if provider_at is None:
        return False
    return provider_at > received_at + PROVIDER_FUTURE_SKEW


def _lock_user_vault_and_outbox(session: Session, outbox_id: UUID) -> VigilOutbox | None:
    peek = session.get(VigilOutbox, outbox_id)
    if peek is None:
        return None
    vault_id = peek.vault_id
    owner_id = session.execute(
        select(VigilVault.owner_user_id).where(VigilVault.id == vault_id)
    ).scalar_one_or_none()
    if owner_id is None:
        return None
    user = session.execute(
        select(User).where(User.id == owner_id).with_for_update()
    ).scalar_one_or_none()
    if user is None:
        return None
    session.execute(
        select(VigilVault).where(VigilVault.id == vault_id).with_for_update()
    ).scalar_one()
    return session.execute(
        select(VigilOutbox).where(VigilOutbox.id == outbox_id).with_for_update()
    ).scalar_one_or_none()


def _frozen_recipient(outbox: VigilOutbox) -> str | None:
    if outbox.payload_cipher is None:
        return None
    from app.services.vigil.dispatch import _decrypt_payload

    try:
        return _decrypt_payload(outbox).to_addr
    except (VigilCryptoError, KeyError, json.JSONDecodeError, TypeError):
        return None


def _recipient_allows_association(
    outbox: VigilOutbox, *, to_addr: str | None, provider_message_id: str
) -> bool:
    if to_addr is None:
        return outbox.provider_id == provider_message_id
    frozen = _frozen_recipient(outbox)
    if frozen is not None:
        return _normalize_addr(frozen) == _normalize_addr(to_addr)
    if outbox.provider_id is None:
        # Tag correlation before the send response: payload should still be
        # present; if it isn't, refuse rather than credit an unverified
        # address onto a vault.
        return False
    return outbox.provider_id == provider_message_id


def _resolve_outbox(
    session: Session, *, provider_message_id: str, outbox_tag: str | None
) -> VigilOutbox | None:
    tagged: VigilOutbox | None = None
    if outbox_tag:
        try:
            tag_id = uuid.UUID(outbox_tag)
        except ValueError:
            tag_id = None
        if tag_id is not None:
            tagged = session.get(VigilOutbox, tag_id)
    by_provider = session.scalars(
        select(VigilOutbox).where(VigilOutbox.provider_id == provider_message_id)
    ).one_or_none()
    if tagged is not None and by_provider is not None and tagged.id != by_provider.id:
        return None
    return tagged or by_provider


def ingest_provider_event(
    session: Session,
    *,
    provider_event_id: str,
    provider_message_id: str,
    event_type: str,
    provider_at: datetime | None,
    received_at: datetime,
    evidence_source: str,
    to_addr: str | None = None,
    outbox_tag: str | None = None,
    raw_body: str | None = None,
) -> IngestStatus:
    """Persist one provider fact. `raw_body` is accepted so callers can
    prove they had it, and is deliberately discarded — never written.
    """
    del raw_body  # never persisted
    if evidence_source not in ("webhook", "poll"):
        raise ValueError(f"unknown evidence_source: {evidence_source!r}")

    candidate = _resolve_outbox(
        session, provider_message_id=provider_message_id, outbox_tag=outbox_tag
    )
    associated: VigilOutbox | None = None
    if candidate is not None and _recipient_allows_association(
        candidate, to_addr=to_addr, provider_message_id=provider_message_id
    ):
        associated = _lock_user_vault_and_outbox(session, candidate.id)
        if associated is not None and not _recipient_allows_association(
            associated, to_addr=to_addr, provider_message_id=provider_message_id
        ):
            associated = None

    event_id = uuid.uuid4()
    address_cipher: str | None = None
    if associated is not None and to_addr:
        address_cipher = encrypt_notification_field(
            to_addr,
            purpose=_ADDRESS_PURPOSE,
            table="vigil_delivery_events",
            row_id=event_id,
            vault_id=associated.vault_id,
        )

    stmt = (
        pg_insert(VigilDeliveryEvent)
        .values(
            id=event_id,
            provider_event_id=provider_event_id,
            outbox_id=associated.id if associated is not None else None,
            provider_message_id=provider_message_id,
            event_type=event_type,
            provider_at=provider_at,
            received_at=received_at,
            evidence_source=evidence_source,
            address_cipher=address_cipher,
        )
        .on_conflict_do_nothing(constraint="uq_vigil_delivery_events_provider_event_id")
    )
    result = cast(CursorResult[Any], session.execute(stmt))
    if int(result.rowcount or 0) == 0:
        return "duplicate"
    return "stored"


def associate_unmatched_events(session: Session) -> int:
    """Link unmatched events whose provider_message_id now equals an
    outbox.provider_id. Webhook-before-response lands here once the send
    response is recorded. Never associates across outboxes/generations.
    """
    unmatched = list(
        session.scalars(
            select(VigilDeliveryEvent).where(VigilDeliveryEvent.outbox_id.is_(None))
        ).all()
    )
    linked = 0
    for event in unmatched:
        outbox = session.scalars(
            select(VigilOutbox).where(VigilOutbox.provider_id == event.provider_message_id)
        ).one_or_none()
        if outbox is None:
            continue
        locked = _lock_user_vault_and_outbox(session, outbox.id)
        if locked is None:
            continue
        if locked.provider_id != event.provider_message_id:
            continue
        event.outbox_id = locked.id
        linked += 1
    return linked


def _events_for_outbox(session: Session, outbox_id: UUID) -> list[VigilDeliveryEvent]:
    return list(
        session.scalars(
            select(VigilDeliveryEvent)
            .where(VigilDeliveryEvent.outbox_id == outbox_id)
            .order_by(VigilDeliveryEvent.received_at, VigilDeliveryEvent.created_at)
        ).all()
    )


def evaluate_delivery_evidence(
    session: Session, outbox_id: UUID, *, now: datetime | None = None
) -> DeliveryEvidence:
    """Return evidence for THIS outbox/generation only. sent/accepted/open/
    click never produce an anchor. Sticky negative overrides a later
    delivered. The first usable delivered freezes the anchor; later
    duplicates do not move it.
    """
    del now  # reserved for P3.3 deadline math; evidence itself is historical
    outbox = session.get(VigilOutbox, outbox_id)
    if outbox is None:
        return DeliveryEvidence(usable=False, anchor_at=None, reason="unknown")

    events = _events_for_outbox(session, outbox_id)
    if any(_event_type_is_negative(e.event_type) for e in events):
        return DeliveryEvidence(usable=False, anchor_at=None, reason="negative")

    delivered = [e for e in events if _event_type_is_delivered(e.event_type)]
    mismatched = [
        e
        for e in delivered
        if _is_provider_time_mismatch(provider_at=e.provider_at, received_at=e.received_at)
    ]
    usable_delivered = [e for e in delivered if e not in mismatched]
    if not usable_delivered:
        if mismatched:
            return DeliveryEvidence(usable=False, anchor_at=None, reason="evidence_mismatch")
        return DeliveryEvidence(usable=False, anchor_at=None, reason="unknown")

    first = usable_delivered[0]
    parts = [first.received_at]
    if first.provider_at is not None:
        parts.append(first.provider_at)
    if outbox.first_attempt_at is not None:
        parts.append(outbox.first_attempt_at)
    return DeliveryEvidence(usable=True, anchor_at=max(parts), reason="delivered")


def _parse_dt(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def ingest_verified_webhook(
    session: Session,
    *,
    provider_event_id: str,
    payload: dict[str, Any],
    received_at: datetime,
) -> IngestStatus:
    event_type = payload.get("type")
    data = payload.get("data")
    if not isinstance(event_type, str) or not isinstance(data, dict):
        return "ignored"
    email_id = data.get("email_id")
    if not isinstance(email_id, str) or not email_id:
        return "ignored"
    to_raw = data.get("to")
    to_addr: str | None = None
    if isinstance(to_raw, list) and to_raw and isinstance(to_raw[0], str):
        to_addr = to_raw[0]
    elif isinstance(to_raw, str):
        to_addr = to_raw
    tags = data.get("tags")
    outbox_tag: str | None = None
    if isinstance(tags, list):
        for tag in tags:
            if isinstance(tag, dict) and tag.get("name") == "vigil_outbox_id":
                value = tag.get("value")
                if isinstance(value, str):
                    outbox_tag = value
                    break
    provider_at = _parse_dt(data.get("created_at")) or _parse_dt(payload.get("created_at"))
    return ingest_provider_event(
        session,
        provider_event_id=provider_event_id,
        provider_message_id=email_id,
        event_type=event_type,
        provider_at=provider_at,
        received_at=received_at,
        evidence_source="webhook",
        to_addr=to_addr,
        outbox_tag=outbox_tag,
    )


def verify_resend_signature(*, payload: str, headers: dict[str, str], secret: str) -> None:
    """Raise ValueError if the Svix signature is missing or invalid."""
    resend.Webhooks.verify(
        {
            "payload": payload,
            "headers": {
                "id": headers["id"],
                "timestamp": headers["timestamp"],
                "signature": headers["signature"],
            },
            "webhook_secret": secret,
        }
    )


def _poll_event_id(outbox_id: UUID, minutes: int) -> str:
    return f"poll:{outbox_id}:{minutes}"


def _due_poll_window(
    outbox: VigilOutbox, events: list[VigilDeliveryEvent], *, now: datetime
) -> int | None:
    if outbox.first_attempt_at is None:
        return None
    existing = {e.provider_event_id for e in events}
    for minutes in POLL_WINDOWS_MINUTES:
        key = _poll_event_id(outbox.id, minutes)
        if key in existing:
            continue
        if now >= outbox.first_attempt_at + timedelta(minutes=minutes):
            return minutes
    return None


def _evidence_already_terminal(session: Session, outbox_id: UUID) -> bool:
    evidence = evaluate_delivery_evidence(session, outbox_id)
    return evidence.reason in {"delivered", "negative"}


def _fetch_provider_email(provider_id: str) -> dict[str, Any] | None:
    settings = get_settings()
    key = settings.RESEND_ALL_ACCESS_API_KEY
    if key is None or not key.get_secret_value():
        return None
    try:
        with httpx.Client(timeout=HTTP_TIMEOUT_SECONDS) as client:
            resp = client.get(
                _RESEND_EMAIL_URL.format(id=provider_id),
                headers={"Authorization": f"Bearer {key.get_secret_value()}"},
            )
    except httpx.HTTPError:
        return None
    if resp.status_code != 200:
        return None
    try:
        body = resp.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def _poll_event_type(last_event: object) -> str:
    if not isinstance(last_event, str) or not last_event:
        return "email.sent"
    if last_event.startswith("email."):
        return last_event
    return f"email.{last_event}"


def run_delivery_poll_sweep(
    session_factory: Callable[[], Session],
    *,
    now: datetime | None = None,
    limit: int = MAX_POLL_ROWS_PER_SWEEP,
) -> DeliveryPollSummary:
    """Poll GET /emails/{id} only for accepted outbox rows still missing
    usable or negative evidence. At most `limit` provider calls per
    invocation; windows are 5, then 15, then 30 minutes after
    first_attempt_at, then stop. Missing evidence stays unknown.
    """
    now = now or datetime.now(UTC)
    session = session_factory()
    try:
        candidates = list(
            session.scalars(
                select(VigilOutbox)
                .where(
                    VigilOutbox.provider_id.isnot(None),
                    VigilOutbox.first_attempt_at.isnot(None),
                    VigilOutbox.first_attempt_at
                    <= now - timedelta(minutes=POLL_WINDOWS_MINUTES[0]),
                )
                .order_by(VigilOutbox.first_attempt_at)
            ).all()
        )
        due: list[tuple[UUID, str, int]] = []
        for row in candidates:
            if row.provider_id is None:
                continue
            if _evidence_already_terminal(session, row.id):
                continue
            events = _events_for_outbox(session, row.id)
            window = _due_poll_window(row, events, now=now)
            if window is None:
                continue
            due.append((row.id, row.provider_id, window))
            if len(due) >= limit:
                break
    finally:
        session.close()

    polled = 0
    for outbox_id, provider_id, window in due:
        body = _fetch_provider_email(provider_id)
        if body is None:
            continue
        to_raw = body.get("to")
        to_addr: str | None = None
        if isinstance(to_raw, list) and to_raw and isinstance(to_raw[0], str):
            to_addr = to_raw[0]
        elif isinstance(to_raw, str):
            to_addr = to_raw
        write_session = session_factory()
        try:
            ingest_provider_event(
                write_session,
                provider_event_id=_poll_event_id(outbox_id, window),
                provider_message_id=provider_id,
                event_type=_poll_event_type(body.get("last_event")),
                provider_at=None,
                received_at=now,
                evidence_source="poll",
                to_addr=to_addr,
            )
            write_session.commit()
            polled += 1
        finally:
            write_session.close()
    return DeliveryPollSummary(polled=polled)
