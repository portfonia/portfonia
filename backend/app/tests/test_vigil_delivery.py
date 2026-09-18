"""services/vigil/delivery.py — verified delivery evidence (issue #457, P3.2).

Real Postgres. Provider HTTP (Resend GET /emails/{id}) is mocked. Webhook
signature verification is local HMAC (resend.Webhooks.verify, 300s
tolerance) and needs no network mock.
"""

from __future__ import annotations

import base64
import hmac
import json
import uuid
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest
import yaml
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models.user import User
from app.models.vigil import VigilDeliveryEvent, VigilOutbox, VigilVault
from app.services.user_purge import purge_user
from app.services.vigil.configuration import (
    validate_configuration_input,
    write_pending_configuration,
)
from app.services.vigil.dispatch import write_outbox_entry
from app.services.vigil.objects import init_object

_OWNER_ID = uuid.UUID("00000000-0000-0000-0000-0000000000f4")
_WEBHOOK_SECRET = "whsec_" + base64.b64encode(b"vigil-p3-2-test-secret-bytes").decode()
_REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(autouse=True)
def _vigil_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import get_settings

    monkeypatch.setenv("VIGIL_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("VIGIL_NOTIFICATION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("VIGIL_MODE", "active")
    monkeypatch.setenv("RESEND_API_KEY", "test-resend-key")
    monkeypatch.setenv("RESEND_WEBHOOK_SECRET", _WEBHOOK_SECRET)
    monkeypatch.setenv("RESEND_ALL_ACCESS_API_KEY", "test-all-access-key")
    get_settings.cache_clear()


def _session_factory() -> Session:
    from app.core.database import SessionLocal

    return SessionLocal()


def _reload_outbox(session: Session, outbox_id: uuid.UUID) -> VigilOutbox:
    session.expire_all()
    row = session.get(VigilOutbox, outbox_id)
    assert row is not None
    return row


def _owner(db_session: Session) -> None:
    db_session.add(
        User(
            id=_OWNER_ID,
            auth_provider="supabase",
            auth_subject="owner-sub-p3-2",
            email="owner-p3-2@example.com",
            status="active",
            locale="zh",
            base_currency="USD",
            report_cadence="mwf",
            email_verified_at=datetime.now(UTC),
        )
    )
    db_session.flush()


def _seed_object(db_session: Session) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    _owner(db_session)
    config_result = write_pending_configuration(
        db_session,
        owner_user_id=_OWNER_ID,
        owner_email="owner-p3-2@example.com",
        expected_revision=0,
        normalized=validate_configuration_input(
            interval_days=30,
            grace_hours=72,
            recipients=[
                {"email": "recipient@example.com", "email_confirm": "recipient@example.com"}
            ],
            message="",
        ),
    )
    db_session.flush()
    object_result = init_object(
        db_session,
        owner_user_id=_OWNER_ID,
        expected_revision=config_result.revision,
        config_id=config_result.config_id,
        request_id=uuid.uuid4(),
        filename="will.pdf",
        plaintext_size=100,
    )
    db_session.flush()
    return object_result.vault_id, config_result.config_id, object_result.object_id


def _write_entry(
    db_session: Session,
    *,
    vault_id: uuid.UUID,
    config_id: uuid.UUID,
    object_id: uuid.UUID,
    dedup_key: str,
    scope_id: uuid.UUID | None = None,
    recipient_email: str = "recipient@example.com",
) -> VigilOutbox:
    vault = db_session.execute(
        select(VigilVault).where(VigilVault.id == vault_id).with_for_update()
    ).scalar_one()
    row = write_outbox_entry(
        db_session,
        vault=vault,
        config_id=config_id,
        object_id=object_id,
        scope_id=scope_id or uuid.uuid4(),
        purpose="challenge",
        dedup_key=dedup_key,
        recipient_email=recipient_email,
        subject="Vigil challenge",
        text_body="challenge text",
        html_body="<p>challenge html</p>",
        token="tok-p3-2",
    )
    db_session.commit()
    return row


def _mark_accepted(
    db_session: Session,
    outbox_id: uuid.UUID,
    *,
    provider_id: str,
    first_attempt_at: datetime,
    accepted_at: datetime | None = None,
) -> None:
    db_session.execute(
        update(VigilOutbox)
        .where(VigilOutbox.id == outbox_id)
        .values(
            status="accepted",
            provider_id=provider_id,
            first_attempt_at=first_attempt_at,
            accepted_at=accepted_at or first_attempt_at,
            payload_cipher=None,
            payload_sha256=None,
        )
    )
    db_session.commit()


def _sign(payload: str, *, msg_id: str, timestamp: str) -> str:
    secret = _WEBHOOK_SECRET.removeprefix("whsec_")
    decoded = base64.b64decode(secret)
    signed = f"{msg_id}.{timestamp}.{payload}"
    digest = hmac.new(decoded, signed.encode(), sha256).digest()
    return "v1," + base64.b64encode(digest).decode()


def _webhook_body(
    *,
    event_type: str,
    email_id: str,
    to_addr: str = "recipient@example.com",
    created_at: str = "2026-01-01T12:00:00+00:00",
    tags: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "created_at": created_at,
        "email_id": email_id,
        "from": "Portfonia <from@example.com>",
        "to": [to_addr],
        "subject": "Vigil challenge",
    }
    if tags is not None:
        data["tags"] = tags
    return {"type": event_type, "created_at": created_at, "data": data}


# --- P3.2-A01 / A07: accepted has no anchor; late provider clock; dup freeze -


def test_accepted_alone_has_no_anchor(db_session: Session) -> None:
    from app.services.vigil.delivery import evaluate_delivery_evidence

    vault_id, config_id, object_id = _seed_object(db_session)
    row = _write_entry(
        db_session,
        vault_id=vault_id,
        config_id=config_id,
        object_id=object_id,
        dedup_key="dk-a01-1",
    )
    first = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    _mark_accepted(db_session, row.id, provider_id="msg-accepted", first_attempt_at=first)

    evidence = evaluate_delivery_evidence(db_session, row.id)
    assert evidence.usable is False
    assert evidence.anchor_at is None
    assert evidence.reason == "unknown"


def test_sent_open_click_never_imply_delivery(db_session: Session) -> None:
    from app.services.vigil.delivery import evaluate_delivery_evidence, ingest_provider_event

    vault_id, config_id, object_id = _seed_object(db_session)
    row = _write_entry(
        db_session,
        vault_id=vault_id,
        config_id=config_id,
        object_id=object_id,
        dedup_key="dk-a01-2",
    )
    first = datetime(2026, 1, 1, 11, 0, tzinfo=UTC)
    _mark_accepted(db_session, row.id, provider_id="msg-opened", first_attempt_at=first)
    received = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    for i, event_type in enumerate(("email.sent", "email.opened", "email.clicked")):
        ingest_provider_event(
            db_session,
            provider_event_id=f"evt-non-{i}",
            provider_message_id="msg-opened",
            event_type=event_type,
            provider_at=received,
            received_at=received,
            evidence_source="webhook",
            to_addr="recipient@example.com",
        )
    db_session.commit()

    evidence = evaluate_delivery_evidence(db_session, row.id)
    assert evidence.usable is False
    assert evidence.anchor_at is None


def test_provider_noon_received_1300_anchors_at_1300_and_duplicates_do_not_move(
    db_session: Session,
) -> None:
    from app.services.vigil.delivery import evaluate_delivery_evidence, ingest_provider_event

    vault_id, config_id, object_id = _seed_object(db_session)
    row = _write_entry(
        db_session,
        vault_id=vault_id,
        config_id=config_id,
        object_id=object_id,
        dedup_key="dk-a01-3",
    )
    first = datetime(2026, 1, 1, 11, 0, tzinfo=UTC)
    _mark_accepted(db_session, row.id, provider_id="msg-del", first_attempt_at=first)

    provider_at = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    received_at = datetime(2026, 1, 1, 13, 0, tzinfo=UTC)
    ingest_provider_event(
        db_session,
        provider_event_id="evt-del-1",
        provider_message_id="msg-del",
        event_type="email.delivered",
        provider_at=provider_at,
        received_at=received_at,
        evidence_source="webhook",
        to_addr="recipient@example.com",
    )
    db_session.commit()

    evidence = evaluate_delivery_evidence(db_session, row.id)
    assert evidence.usable is True
    assert evidence.anchor_at == received_at
    assert evidence.reason == "delivered"

    ingest_provider_event(
        db_session,
        provider_event_id="evt-del-2",
        provider_message_id="msg-del",
        event_type="email.delivered",
        provider_at=datetime(2026, 1, 1, 14, 0, tzinfo=UTC),
        received_at=datetime(2026, 1, 1, 15, 0, tzinfo=UTC),
        evidence_source="webhook",
        to_addr="recipient@example.com",
    )
    db_session.commit()

    later = evaluate_delivery_evidence(db_session, row.id)
    assert later.usable is True
    assert later.anchor_at == received_at


def test_provider_time_more_than_five_minutes_ahead_is_mismatch(db_session: Session) -> None:
    from app.services.vigil.delivery import evaluate_delivery_evidence, ingest_provider_event

    vault_id, config_id, object_id = _seed_object(db_session)
    row = _write_entry(
        db_session,
        vault_id=vault_id,
        config_id=config_id,
        object_id=object_id,
        dedup_key="dk-a01-4",
    )
    first = datetime(2026, 1, 1, 11, 0, tzinfo=UTC)
    _mark_accepted(db_session, row.id, provider_id="msg-mis", first_attempt_at=first)
    received = datetime(2026, 1, 1, 13, 0, tzinfo=UTC)
    ingest_provider_event(
        db_session,
        provider_event_id="evt-mis",
        provider_message_id="msg-mis",
        event_type="email.delivered",
        provider_at=received + timedelta(minutes=6),
        received_at=received,
        evidence_source="webhook",
        to_addr="recipient@example.com",
    )
    db_session.commit()

    evidence = evaluate_delivery_evidence(db_session, row.id)
    assert evidence.usable is False
    assert evidence.anchor_at is None
    assert evidence.reason == "evidence_mismatch"


# --- P3.2-A02: wrong product/message/address; webhook-before-response; 400 --


def test_wrong_address_does_not_drive_state(db_session: Session) -> None:
    from app.services.vigil.delivery import evaluate_delivery_evidence, ingest_provider_event

    vault_id, config_id, object_id = _seed_object(db_session)
    row = _write_entry(
        db_session,
        vault_id=vault_id,
        config_id=config_id,
        object_id=object_id,
        dedup_key="dk-a02-1",
    )

    ingest_provider_event(
        db_session,
        provider_event_id="evt-wrong-addr",
        provider_message_id="msg-addr",
        event_type="email.delivered",
        provider_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        received_at=datetime(2026, 1, 1, 12, 1, tzinfo=UTC),
        evidence_source="webhook",
        to_addr="other@example.com",
        outbox_tag=str(row.id),
    )
    db_session.commit()

    events = list(db_session.scalars(select(VigilDeliveryEvent)).all())
    assert len(events) == 1
    assert events[0].outbox_id is None

    evidence = evaluate_delivery_evidence(db_session, row.id)
    assert evidence.usable is False
    assert evidence.anchor_at is None


def test_unrelated_report_mail_cannot_drive_vigil_state(db_session: Session) -> None:
    from app.services.vigil.delivery import evaluate_delivery_evidence, ingest_provider_event

    vault_id, config_id, object_id = _seed_object(db_session)
    row = _write_entry(
        db_session,
        vault_id=vault_id,
        config_id=config_id,
        object_id=object_id,
        dedup_key="dk-a02-2",
    )
    first = datetime(2026, 1, 1, 11, 0, tzinfo=UTC)
    _mark_accepted(db_session, row.id, provider_id="msg-vigil", first_attempt_at=first)

    ingest_provider_event(
        db_session,
        provider_event_id="evt-report",
        provider_message_id="msg-report-other",
        event_type="email.delivered",
        provider_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        received_at=datetime(2026, 1, 1, 12, 1, tzinfo=UTC),
        evidence_source="webhook",
        to_addr="recipient@example.com",
    )
    db_session.commit()

    event = db_session.scalars(select(VigilDeliveryEvent)).one()
    assert event.outbox_id is None
    evidence = evaluate_delivery_evidence(db_session, row.id)
    assert evidence.usable is False
    assert evidence.anchor_at is None


def test_webhook_before_provider_id_persists_then_associates(db_session: Session) -> None:
    from app.services.vigil.delivery import (
        associate_unmatched_events,
        evaluate_delivery_evidence,
        ingest_provider_event,
    )

    vault_id, config_id, object_id = _seed_object(db_session)
    row = _write_entry(
        db_session,
        vault_id=vault_id,
        config_id=config_id,
        object_id=object_id,
        dedup_key="dk-a02-3",
    )
    first = datetime(2026, 1, 1, 11, 0, tzinfo=UTC)
    received = datetime(2026, 1, 1, 12, 5, tzinfo=UTC)

    ingest_provider_event(
        db_session,
        provider_event_id="evt-early",
        provider_message_id="msg-early",
        event_type="email.delivered",
        provider_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        received_at=received,
        evidence_source="webhook",
        to_addr="recipient@example.com",
        outbox_tag=str(row.id),
    )
    db_session.commit()

    linked = db_session.scalars(select(VigilDeliveryEvent)).one()
    assert linked.outbox_id == row.id

    _mark_accepted(db_session, row.id, provider_id="msg-early", first_attempt_at=first)
    associate_unmatched_events(db_session)
    db_session.commit()

    evidence = evaluate_delivery_evidence(db_session, row.id)
    assert evidence.usable is True
    assert evidence.anchor_at == received


def test_webhook_before_response_associates_once_provider_id_lands(
    db_session: Session,
) -> None:
    from app.services.vigil.delivery import (
        associate_unmatched_events,
        evaluate_delivery_evidence,
        ingest_provider_event,
    )

    vault_id, config_id, object_id = _seed_object(db_session)
    row = _write_entry(
        db_session,
        vault_id=vault_id,
        config_id=config_id,
        object_id=object_id,
        dedup_key="dk-a02-4",
    )
    received = datetime(2026, 1, 1, 12, 5, tzinfo=UTC)
    ingest_provider_event(
        db_session,
        provider_event_id="evt-no-tag",
        provider_message_id="msg-late-id",
        event_type="email.delivered",
        provider_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        received_at=received,
        evidence_source="webhook",
        to_addr="recipient@example.com",
    )
    db_session.commit()
    assert db_session.scalars(select(VigilDeliveryEvent)).one().outbox_id is None

    first = datetime(2026, 1, 1, 11, 0, tzinfo=UTC)
    _mark_accepted(db_session, row.id, provider_id="msg-late-id", first_attempt_at=first)
    associate_unmatched_events(db_session)
    db_session.commit()

    assert db_session.scalars(select(VigilDeliveryEvent)).one().outbox_id == row.id
    evidence = evaluate_delivery_evidence(db_session, row.id)
    assert evidence.usable is True
    assert evidence.anchor_at == received


def test_bad_signature_is_rejected_with_400(app_client: TestClient, db_session: Session) -> None:
    vault_id, config_id, object_id = _seed_object(db_session)
    _write_entry(
        db_session,
        vault_id=vault_id,
        config_id=config_id,
        object_id=object_id,
        dedup_key="dk-a02-5",
    )
    payload = json.dumps(_webhook_body(event_type="email.delivered", email_id="msg-x"))
    resp = app_client.post(
        "/vigil/webhooks/resend",
        content=payload,
        headers={
            "svix-id": "evt-bad",
            "svix-timestamp": str(int(datetime.now(UTC).timestamp())),
            "svix-signature": "v1,not-a-real-signature",
            "content-type": "application/json",
        },
    )
    assert resp.status_code == 400
    assert db_session.scalars(select(VigilDeliveryEvent)).first() is None


def test_valid_signature_persists_event_and_duplicate_is_200_noop(
    app_client: TestClient, db_session: Session
) -> None:
    vault_id, config_id, object_id = _seed_object(db_session)
    row = _write_entry(
        db_session,
        vault_id=vault_id,
        config_id=config_id,
        object_id=object_id,
        dedup_key="dk-a02-6",
    )
    first = datetime.now(UTC) - timedelta(minutes=10)
    _mark_accepted(db_session, row.id, provider_id="msg-signed", first_attempt_at=first)

    body = _webhook_body(
        event_type="email.delivered",
        email_id="msg-signed",
        created_at=first.isoformat(),
        tags=[{"name": "vigil_outbox_id", "value": str(row.id)}],
    )
    payload = json.dumps(body, separators=(",", ":"))
    ts = str(int(datetime.now(UTC).timestamp()))
    headers = {
        "svix-id": "evt-signed-1",
        "svix-timestamp": ts,
        "svix-signature": _sign(payload, msg_id="evt-signed-1", timestamp=ts),
        "content-type": "application/json",
    }
    first_resp = app_client.post("/vigil/webhooks/resend", content=payload, headers=headers)
    assert first_resp.status_code == 200
    assert db_session.query(VigilDeliveryEvent).count() == 1

    dup = app_client.post("/vigil/webhooks/resend", content=payload, headers=headers)
    assert dup.status_code == 200
    assert db_session.query(VigilDeliveryEvent).count() == 1


def test_ingest_never_persists_raw_body_or_token(db_session: Session) -> None:
    from app.services.vigil.delivery import ingest_provider_event

    vault_id, config_id, object_id = _seed_object(db_session)
    row = _write_entry(
        db_session,
        vault_id=vault_id,
        config_id=config_id,
        object_id=object_id,
        dedup_key="dk-a02-7",
    )
    first = datetime(2026, 1, 1, 11, 0, tzinfo=UTC)
    _mark_accepted(db_session, row.id, provider_id="msg-raw", first_attempt_at=first)
    ingest_provider_event(
        db_session,
        provider_event_id="evt-raw",
        provider_message_id="msg-raw",
        event_type="email.delivered",
        provider_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        received_at=datetime(2026, 1, 1, 12, 1, tzinfo=UTC),
        evidence_source="webhook",
        to_addr="recipient@example.com",
        raw_body='{"token":"tok-p3-2","type":"email.delivered"}',
    )
    db_session.commit()
    event = db_session.scalars(select(VigilDeliveryEvent)).one()
    dumped = json.dumps(
        {c.name: getattr(event, c.name) for c in event.__table__.columns}, default=str
    )
    assert "tok-p3-2" not in dumped
    assert "raw_body" not in event.__table__.c


# --- P3.2-A03: sticky negative; old generation cannot start a new one ------


def test_negative_then_delayed_delivered_stays_unusable(db_session: Session) -> None:
    from app.services.vigil.delivery import evaluate_delivery_evidence, ingest_provider_event

    vault_id, config_id, object_id = _seed_object(db_session)
    row = _write_entry(
        db_session,
        vault_id=vault_id,
        config_id=config_id,
        object_id=object_id,
        dedup_key="dk-a03-1",
    )
    first = datetime(2026, 1, 1, 11, 0, tzinfo=UTC)
    _mark_accepted(db_session, row.id, provider_id="msg-bounce", first_attempt_at=first)
    ingest_provider_event(
        db_session,
        provider_event_id="evt-bounce",
        provider_message_id="msg-bounce",
        event_type="email.bounced",
        provider_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        received_at=datetime(2026, 1, 1, 12, 1, tzinfo=UTC),
        evidence_source="webhook",
        to_addr="recipient@example.com",
    )
    ingest_provider_event(
        db_session,
        provider_event_id="evt-late-del",
        provider_message_id="msg-bounce",
        event_type="email.delivered",
        provider_at=datetime(2026, 1, 1, 13, 0, tzinfo=UTC),
        received_at=datetime(2026, 1, 1, 13, 1, tzinfo=UTC),
        evidence_source="webhook",
        to_addr="recipient@example.com",
    )
    db_session.commit()

    evidence = evaluate_delivery_evidence(db_session, row.id)
    assert evidence.usable is False
    assert evidence.anchor_at is None
    assert evidence.reason == "negative"


def test_old_generation_event_cannot_start_new_generation(db_session: Session) -> None:
    from app.services.vigil.delivery import evaluate_delivery_evidence, ingest_provider_event

    vault_id, config_id, object_id = _seed_object(db_session)
    old_scope = uuid.uuid4()
    new_scope = uuid.uuid4()
    old = _write_entry(
        db_session,
        vault_id=vault_id,
        config_id=config_id,
        object_id=object_id,
        dedup_key="dk-a03-old",
        scope_id=old_scope,
    )
    new = _write_entry(
        db_session,
        vault_id=vault_id,
        config_id=config_id,
        object_id=object_id,
        dedup_key="dk-a03-new",
        scope_id=new_scope,
    )
    first = datetime(2026, 1, 1, 11, 0, tzinfo=UTC)
    _mark_accepted(db_session, old.id, provider_id="msg-old", first_attempt_at=first)
    _mark_accepted(db_session, new.id, provider_id="msg-new", first_attempt_at=first)

    ingest_provider_event(
        db_session,
        provider_event_id="evt-old-del",
        provider_message_id="msg-old",
        event_type="email.delivered",
        provider_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        received_at=datetime(2026, 1, 1, 12, 1, tzinfo=UTC),
        evidence_source="webhook",
        to_addr="recipient@example.com",
    )
    db_session.commit()

    old_ev = evaluate_delivery_evidence(db_session, old.id)
    assert old_ev.usable is True
    new_ev = evaluate_delivery_evidence(db_session, new.id)
    assert new_ev.usable is False
    assert new_ev.anchor_at is None
    assert new_ev.reason == "unknown"


# --- P3.2-A04: missing evidence unknown; mocked network; no new service -----


def test_missing_provider_evidence_stays_unknown(db_session: Session) -> None:
    from app.services.vigil.delivery import evaluate_delivery_evidence

    vault_id, config_id, object_id = _seed_object(db_session)
    row = _write_entry(
        db_session,
        vault_id=vault_id,
        config_id=config_id,
        object_id=object_id,
        dedup_key="dk-a04-1",
    )
    _mark_accepted(
        db_session,
        row.id,
        provider_id="msg-missing",
        first_attempt_at=datetime(2026, 1, 1, 11, 0, tzinfo=UTC),
    )
    evidence = evaluate_delivery_evidence(db_session, row.id)
    assert evidence.usable is False
    assert evidence.anchor_at is None
    assert evidence.reason == "unknown"


def test_poll_records_delivered_at_observation_when_timestamp_missing(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.vigil import delivery as delivery_mod
    from app.services.vigil.delivery import evaluate_delivery_evidence, run_delivery_poll_sweep

    vault_id, config_id, object_id = _seed_object(db_session)
    row = _write_entry(
        db_session,
        vault_id=vault_id,
        config_id=config_id,
        object_id=object_id,
        dedup_key="dk-a04-2",
    )
    first = datetime(2026, 1, 1, 11, 0, tzinfo=UTC)
    _mark_accepted(db_session, row.id, provider_id="msg-poll", first_attempt_at=first)

    calls: list[str] = []

    def _fake_get(provider_id: str) -> dict[str, Any]:
        calls.append(provider_id)
        return {"id": provider_id, "last_event": "delivered", "to": ["recipient@example.com"]}

    monkeypatch.setattr(delivery_mod, "_fetch_provider_email", _fake_get)

    now = first + timedelta(minutes=5)
    summary = run_delivery_poll_sweep(_session_factory, now=now)
    assert summary.polled == 1
    assert calls == ["msg-poll"]

    evidence = evaluate_delivery_evidence(db_session, row.id, now=now)
    assert evidence.usable is True
    assert evidence.anchor_at == now
    event = db_session.scalars(select(VigilDeliveryEvent)).one()
    assert event.evidence_source == "poll"
    assert event.provider_at is None


def test_poll_skips_when_evidence_already_present(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.vigil import delivery as delivery_mod
    from app.services.vigil.delivery import ingest_provider_event, run_delivery_poll_sweep

    vault_id, config_id, object_id = _seed_object(db_session)
    row = _write_entry(
        db_session,
        vault_id=vault_id,
        config_id=config_id,
        object_id=object_id,
        dedup_key="dk-a04-3",
    )
    first = datetime(2026, 1, 1, 11, 0, tzinfo=UTC)
    _mark_accepted(db_session, row.id, provider_id="msg-skip", first_attempt_at=first)
    ingest_provider_event(
        db_session,
        provider_event_id="evt-already",
        provider_message_id="msg-skip",
        event_type="email.delivered",
        provider_at=datetime(2026, 1, 1, 11, 30, tzinfo=UTC),
        received_at=datetime(2026, 1, 1, 11, 31, tzinfo=UTC),
        evidence_source="webhook",
        to_addr="recipient@example.com",
    )
    db_session.commit()

    def _fake_get(provider_id: str) -> dict[str, Any]:
        raise AssertionError(f"must not poll when evidence exists: {provider_id}")

    monkeypatch.setattr(delivery_mod, "_fetch_provider_email", _fake_get)
    summary = run_delivery_poll_sweep(_session_factory, now=first + timedelta(minutes=30))
    assert summary.polled == 0


def test_poll_windows_are_5_15_30_and_stop_after_third(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.vigil import delivery as delivery_mod
    from app.services.vigil.delivery import run_delivery_poll_sweep

    vault_id, config_id, object_id = _seed_object(db_session)
    row = _write_entry(
        db_session,
        vault_id=vault_id,
        config_id=config_id,
        object_id=object_id,
        dedup_key="dk-a04-4",
    )
    first = datetime(2026, 1, 1, 11, 0, tzinfo=UTC)
    _mark_accepted(db_session, row.id, provider_id="msg-windows", first_attempt_at=first)

    def _fake_get(provider_id: str) -> dict[str, Any]:
        return {"id": provider_id, "last_event": "sent", "to": ["recipient@example.com"]}

    monkeypatch.setattr(delivery_mod, "_fetch_provider_email", _fake_get)

    assert run_delivery_poll_sweep(_session_factory, now=first + timedelta(minutes=4)).polled == 0
    assert run_delivery_poll_sweep(_session_factory, now=first + timedelta(minutes=5)).polled == 1
    assert run_delivery_poll_sweep(_session_factory, now=first + timedelta(minutes=6)).polled == 0
    assert run_delivery_poll_sweep(_session_factory, now=first + timedelta(minutes=15)).polled == 1
    assert run_delivery_poll_sweep(_session_factory, now=first + timedelta(minutes=30)).polled == 1
    assert run_delivery_poll_sweep(_session_factory, now=first + timedelta(minutes=45)).polled == 0
    assert db_session.query(VigilDeliveryEvent).count() == 3


def test_no_new_compose_service_or_domain() -> None:
    compose = yaml.safe_load((_REPO_ROOT / "docker-compose.yml").read_text())
    assert set(compose["services"]) == {
        "postgres",
        "redis",
        "migrate",
        "backend",
        "celery-worker",
        "celery-beat",
        "frontend",
        "caddy",
    }
    caddy = (_REPO_ROOT / "Caddyfile").read_text()
    assert "vigil." not in caddy
    assert "portfonia.com" in caddy


# --- lock order, purge, webhook 503 ----------------------------------------


def test_associated_ingest_locks_user_then_vault_then_outbox(db_session: Session) -> None:
    from sqlalchemy import event as sa_event

    from app.core.database import get_engine
    from app.services.vigil.delivery import ingest_provider_event

    vault_id, config_id, object_id = _seed_object(db_session)
    row = _write_entry(
        db_session, vault_id=vault_id, config_id=config_id, object_id=object_id, dedup_key="dk-lock"
    )
    first = datetime(2026, 1, 1, 11, 0, tzinfo=UTC)
    _mark_accepted(db_session, row.id, provider_id="msg-lock", first_attempt_at=first)

    order: list[str] = []

    def _on_cursor(conn: object, cursor: object, statement: str, *args: object) -> None:
        sql = statement.lower()
        if "for update" not in sql:
            return
        if "from users" in sql or "from users " in sql:
            order.append("users")
        elif "vigil_vaults" in sql:
            order.append("vigil_vaults")
        elif "vigil_outbox" in sql:
            order.append("vigil_outbox")

    sa_event.listen(get_engine(), "before_cursor_execute", _on_cursor)
    try:
        ingest_provider_event(
            db_session,
            provider_event_id="evt-lock",
            provider_message_id="msg-lock",
            event_type="email.delivered",
            provider_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
            received_at=datetime(2026, 1, 1, 12, 1, tzinfo=UTC),
            evidence_source="webhook",
            to_addr="recipient@example.com",
        )
        db_session.commit()
    finally:
        sa_event.remove(get_engine(), "before_cursor_execute", _on_cursor)

    first_seen = [name for i, name in enumerate(order) if name not in order[:i]]
    assert first_seen[:3] == ["users", "vigil_vaults", "vigil_outbox"]


def test_purge_user_removes_delivery_events_before_outbox(db_session: Session) -> None:
    from app.services.vigil.delivery import ingest_provider_event

    vault_id, config_id, object_id = _seed_object(db_session)
    row = _write_entry(
        db_session,
        vault_id=vault_id,
        config_id=config_id,
        object_id=object_id,
        dedup_key="dk-purge",
    )
    first = datetime(2026, 1, 1, 11, 0, tzinfo=UTC)
    _mark_accepted(db_session, row.id, provider_id="msg-purge", first_attempt_at=first)
    ingest_provider_event(
        db_session,
        provider_event_id="evt-purge",
        provider_message_id="msg-purge",
        event_type="email.delivered",
        provider_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        received_at=datetime(2026, 1, 1, 12, 1, tzinfo=UTC),
        evidence_source="webhook",
        to_addr="recipient@example.com",
    )
    db_session.commit()
    event_id = db_session.scalars(select(VigilDeliveryEvent.id)).one()
    outbox_id = row.id

    result = purge_user(db_session, _OWNER_ID)
    db_session.commit()

    assert result.vigil_delivery_events == 1
    assert result.vigil_outbox == 1
    assert db_session.get(VigilDeliveryEvent, event_id) is None
    assert db_session.get(VigilOutbox, outbox_id) is None
    assert db_session.get(User, _OWNER_ID) is None


def test_db_failure_on_webhook_returns_503(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy.exc import OperationalError

    def _boom(*args: object, **kwargs: object) -> None:
        raise OperationalError("select 1", {}, Exception("down"))

    monkeypatch.setattr("app.routers.vigil.ingest_verified_webhook", _boom)

    payload = json.dumps(_webhook_body(event_type="email.delivered", email_id="msg-503"))
    ts = str(int(datetime.now(UTC).timestamp()))
    resp = app_client.post(
        "/vigil/webhooks/resend",
        content=payload,
        headers={
            "svix-id": "evt-503",
            "svix-timestamp": ts,
            "svix-signature": _sign(payload, msg_id="evt-503", timestamp=ts),
            "content-type": "application/json",
        },
    )
    assert resp.status_code == 503
