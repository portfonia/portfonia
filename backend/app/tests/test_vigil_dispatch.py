"""services/vigil/dispatch.py — shared-worker encrypted outbox (issue #456,
Vigil R0 P3.1).

Real Postgres per this project's test convention. `send_fn` is always
mocked here (#456 A04 / project-wide "external notifications mocked by
default") — no real Resend call is ever made in this file.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models.user import User
from app.models.vigil import VigilObject, VigilOutbox, VigilVault
from app.services.user_purge import purge_user
from app.services.vigil.configuration import (
    validate_configuration_input,
    write_pending_configuration,
)
from app.services.vigil.dispatch import (
    MAX_ATTEMPT_WINDOW,
    ProviderSendResult,
    _finalize_attempt,
    _lease_one,
    _next_attempt_after,
    run_outbox_dispatch_sweep,
    sweep_expired_outbox,
    write_outbox_entry,
)
from app.services.vigil.objects import init_object

_OWNER_ID = uuid.UUID("00000000-0000-0000-0000-0000000000f3")


@pytest.fixture(autouse=True)
def _vigil_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import get_settings

    monkeypatch.setenv("VIGIL_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("VIGIL_NOTIFICATION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("RESEND_API_KEY", "test-resend-key")
    get_settings.cache_clear()


def _reload(session: Session, outbox_id: uuid.UUID) -> VigilOutbox:
    """Other tests' `_session_factory()` instances commit their own writes
    on the SAME underlying connection (`db_session`'s SAVEPOINT-per-Session
    fixture) — but `db_session`'s own Python-level identity map doesn't
    know that unless expired. Without this, a second `db_session.get(...)`
    for an id already cached from an earlier read would silently return
    stale in-memory data instead of re-querying."""
    session.expire_all()
    row = session.get(VigilOutbox, outbox_id)
    assert row is not None
    return row


def _session_factory() -> Session:
    # Lazy import so this always resolves the `db_session` fixture's
    # monkeypatched app.core.database.SessionLocal (same connection, a new
    # SAVEPOINT per call) rather than the module-level original captured at
    # collection time.
    from app.core.database import SessionLocal

    return SessionLocal()


def _owner(db_session: Session) -> None:
    db_session.add(
        User(
            id=_OWNER_ID,
            auth_provider="supabase",
            auth_subject="owner-sub-p3-1",
            email="owner-p3-1@example.com",
            status="active",
            locale="zh",
            base_currency="USD",
            report_cadence="mwf",
            email_verified_at=datetime.now(UTC),
        )
    )
    db_session.flush()


def _seed_object(db_session: Session) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Returns (vault_id, config_id, object_id) — a staging object is
    enough here: this checkpoint has no real caller that requires `ready`
    (#456 non-goal)."""
    _owner(db_session)
    config_result = write_pending_configuration(
        db_session,
        owner_user_id=_OWNER_ID,
        owner_email="owner-p3-1@example.com",
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
        purpose="drill",
        dedup_key=dedup_key,
        recipient_email="recipient@example.com",
        subject="Vigil drill",
        text_body="drill text",
        html_body="<p>drill html</p>",
        token="tok-abc123",
    )
    db_session.commit()
    return row


# --- write_outbox_entry -----------------------------------------------------


def test_write_outbox_entry_round_trips_payload(db_session: Session) -> None:
    vault_id, config_id, object_id = _seed_object(db_session)
    row = _write_entry(
        db_session, vault_id=vault_id, config_id=config_id, object_id=object_id, dedup_key="dk-1"
    )

    fresh = _reload(db_session, row.id)
    assert fresh.status == "pending"
    assert fresh.attempts == 0
    assert fresh.payload_cipher is not None
    assert fresh.payload_sha256 is not None

    from app.services.vigil.dispatch import _decrypt_payload

    payload = _decrypt_payload(fresh)
    assert payload.to_addr == "recipient@example.com"
    assert payload.token == "tok-abc123"
    assert payload.purpose == "drill"


# --- happy path dispatch -----------------------------------------------------


def test_sweep_sends_pending_row_and_records_accepted(db_session: Session) -> None:
    vault_id, config_id, object_id = _seed_object(db_session)
    row = _write_entry(
        db_session, vault_id=vault_id, config_id=config_id, object_id=object_id, dedup_key="dk-2"
    )

    def _send(body: dict[str, Any], idempotency_key: str) -> ProviderSendResult:
        assert idempotency_key == f"vigil/{row.id}"
        assert body["to"] == ["recipient@example.com"]
        return ProviderSendResult(outcome="accepted", provider_id="resend-1", error_code=None)

    summary = run_outbox_dispatch_sweep(_session_factory, send_fn=_send, now=datetime.now(UTC))
    assert summary.sent == 1

    fresh = _reload(db_session, row.id)
    assert fresh.status == "accepted"
    assert fresh.provider_id == "resend-1"
    assert fresh.accepted_at is not None
    assert fresh.payload_cipher is None
    assert fresh.payload_sha256 is None
    assert fresh.attempts == 1


# --- P3.1-A01 / A10: accepted-then-rollback recovers with same key/body ----


def test_recovery_after_local_commit_failure_reuses_same_key_and_body(
    db_session: Session,
) -> None:
    vault_id, config_id, object_id = _seed_object(db_session)
    row = _write_entry(
        db_session, vault_id=vault_id, config_id=config_id, object_id=object_id, dedup_key="dk-3"
    )
    now = datetime.now(UTC)

    seen: list[tuple[dict[str, Any], str]] = []

    def _send(body: dict[str, Any], idempotency_key: str) -> ProviderSendResult:
        seen.append((body, idempotency_key))
        return ProviderSendResult(outcome="accepted", provider_id="resend-2", error_code=None)

    # Simulate: provider accepted, but the local finalize transaction rolls
    # back (e.g. a DB outage right after the HTTP call returned) — the row
    # is left exactly as `_lease_one` left it: status='leased', payload
    # intact, attempts/first_attempt_at unchanged.
    assert _lease_one(_session_factory, row.id, now=now)
    result = _send(*_manual_body_and_key(db_session, row.id))
    finalize_session = _session_factory()
    try:
        r = finalize_session.get(VigilOutbox, row.id, with_for_update=True)
        assert r is not None
        r.status = "accepted"
        r.provider_id = result.provider_id
        finalize_session.rollback()  # the simulated failure: never committed
    finally:
        finalize_session.close()

    stuck = _reload(db_session, row.id)
    assert stuck.status == "leased"  # unaffected by the rolled-back attempt
    assert stuck.payload_cipher is not None

    # Recovery: the next sweep (lease reclaimed once its 60s expires)
    # replays with the SAME idempotency key and SAME body — no new token.
    later = now + timedelta(seconds=61)
    summary = run_outbox_dispatch_sweep(_session_factory, send_fn=_send, now=later)
    assert summary.sent == 1
    assert len(seen) == 2
    assert seen[0][1] == seen[1][1] == f"vigil/{row.id}"
    assert seen[0][0] == seen[1][0]

    final = _reload(db_session, row.id)
    assert final.status == "accepted"
    assert final.payload_cipher is None


def _manual_body_and_key(db_session: Session, outbox_id: uuid.UUID) -> tuple[dict[str, Any], str]:
    from app.services.vigil.dispatch import _decrypt_payload

    row = db_session.get(VigilOutbox, outbox_id)
    assert row is not None
    payload = _decrypt_payload(row)
    body = {
        "from": payload.from_addr,
        "to": [payload.to_addr],
        "subject": payload.subject,
        "text": payload.text,
        "html": payload.html,
    }
    return body, f"vigil/{outbox_id}"


# --- post-send finalization race: a cancellation commits mid-flight -------


def test_late_accept_never_reactivates_a_cancelled_row(db_session: Session) -> None:
    vault_id, config_id, object_id = _seed_object(db_session)
    row = _write_entry(
        db_session, vault_id=vault_id, config_id=config_id, object_id=object_id, dedup_key="dk-4"
    )
    now = datetime.now(UTC)
    assert _lease_one(_session_factory, row.id, now=now)

    # A cancellation commits WHILE our (simulated) HTTP call is "in flight".
    cancel_session = _session_factory()
    try:
        cancel_session.execute(
            update(VigilOutbox)
            .where(VigilOutbox.id == row.id)
            .values(status="cancelled", payload_cipher=None, payload_sha256=None)
        )
        cancel_session.commit()
    finally:
        cancel_session.close()

    # Our attempt's HTTP call returns success only AFTER the cancellation
    # already committed.
    late_result = ProviderSendResult(outcome="accepted", provider_id="resend-late", error_code=None)
    _finalize_attempt(_session_factory, row.id, late_result, now=now + timedelta(seconds=5))

    final = _reload(db_session, row.id)
    assert final.status == "cancelled"  # never reactivated
    assert final.payload_cipher is None  # never restored
    # The late acceptance is still recorded as a historical delivery fact.
    assert final.provider_id == "resend-late"
    assert final.accepted_at is not None


# --- P3.1-A02: unknown outcome retry ceiling + 24h payload sweep -----------


def test_unknown_outcome_is_retried_then_stops_after_23h_window() -> None:
    first_attempt = datetime(2026, 1, 1, tzinfo=UTC)
    # attempts 1..4 land within the 1/5/15/60-minute schedule.
    for attempts, expected_minutes in zip((1, 2, 3, 4), (1, 5, 15, 60), strict=True):
        now = first_attempt
        next_at = _next_attempt_after(attempts=attempts, first_attempt_at=first_attempt, now=now)
        assert next_at == now + timedelta(minutes=expected_minutes)

    # 5th attempt: max attempts reached regardless of the 23h window.
    assert (
        _next_attempt_after(attempts=5, first_attempt_at=first_attempt, now=first_attempt) is None
    )

    # A candidate that would land past first_attempt + 23h is refused even
    # under the attempt-count ceiling.
    near_deadline = first_attempt + MAX_ATTEMPT_WINDOW - timedelta(minutes=30)
    assert (
        _next_attempt_after(attempts=4, first_attempt_at=first_attempt, now=near_deadline) is None
    )


def test_unknown_payload_cleared_24h_after_first_attempt(db_session: Session) -> None:
    vault_id, config_id, object_id = _seed_object(db_session)
    row = _write_entry(
        db_session, vault_id=vault_id, config_id=config_id, object_id=object_id, dedup_key="dk-5"
    )
    first_attempt = datetime.now(UTC) - timedelta(hours=25)
    db_session.execute(
        update(VigilOutbox)
        .where(VigilOutbox.id == row.id)
        .values(status="unknown", first_attempt_at=first_attempt, attempts=5, next_attempt_at=None)
    )
    db_session.commit()

    # Wall-clock check must hold independent of whether the sweep ran:
    still_stale_before_sweep = db_session.get(VigilOutbox, row.id)
    assert still_stale_before_sweep is not None
    assert still_stale_before_sweep.payload_cipher is not None  # sweep hasn't run yet

    cleared = sweep_expired_outbox(db_session, now=datetime.now(UTC))
    db_session.commit()
    assert cleared >= 1

    fresh = db_session.get(VigilOutbox, row.id)
    assert fresh is not None
    assert fresh.status == "unknown"
    assert fresh.payload_cipher is None
    assert fresh.payload_sha256 is None


# --- P3.1-A03: never-attempted intent cancelled at 24h from creation -------


def test_never_attempted_row_cancelled_24h_after_creation(db_session: Session) -> None:
    vault_id, config_id, object_id = _seed_object(db_session)
    row = _write_entry(
        db_session, vault_id=vault_id, config_id=config_id, object_id=object_id, dedup_key="dk-6"
    )
    db_session.execute(
        update(VigilOutbox)
        .where(VigilOutbox.id == row.id)
        .values(created_at=datetime.now(UTC) - timedelta(hours=25))
    )
    db_session.commit()

    cleared = sweep_expired_outbox(db_session, now=datetime.now(UTC))
    db_session.commit()
    assert cleared >= 1

    fresh = db_session.get(VigilOutbox, row.id)
    assert fresh is not None
    assert fresh.status == "cancelled"
    assert fresh.payload_cipher is None


# --- P3.1-A04: bounded to <=5 rows per sweep, no sleep ---------------------


def test_sweep_processes_at_most_five_rows(db_session: Session) -> None:
    vault_id, config_id, object_id = _seed_object(db_session)
    rows = [
        _write_entry(
            db_session,
            vault_id=vault_id,
            config_id=config_id,
            object_id=object_id,
            dedup_key=f"dk-bulk-{i}",
        )
        for i in range(7)
    ]

    def _send(body: dict[str, Any], idempotency_key: str) -> ProviderSendResult:
        return ProviderSendResult(outcome="accepted", provider_id=idempotency_key, error_code=None)

    summary = run_outbox_dispatch_sweep(_session_factory, send_fn=_send, now=datetime.now(UTC))
    assert summary.sent == 5

    statuses = {
        r.id: db_session.get(VigilOutbox, r.id).status  # type: ignore[union-attr]
        for r in rows
    }
    assert sum(1 for s in statuses.values() if s == "accepted") == 5
    assert sum(1 for s in statuses.values() if s == "pending") == 2


# --- account purge extension -------------------------------------------------


def test_purge_user_removes_outbox_rows_before_configurations_and_objects(
    db_session: Session,
) -> None:
    vault_id, config_id, object_id = _seed_object(db_session)
    outbox_row = _write_entry(
        db_session, vault_id=vault_id, config_id=config_id, object_id=object_id, dedup_key="dk-7"
    )
    outbox_id = outbox_row.id  # captured before purge expires/deletes the row

    result = purge_user(db_session, _OWNER_ID)
    db_session.commit()

    assert result.vigil_outbox == 1
    assert result.vigil_configurations == 1
    assert result.vigil_objects == 1
    assert db_session.get(VigilOutbox, outbox_id) is None
    assert (
        db_session.execute(select(VigilObject).where(VigilObject.id == object_id)).first() is None
    )
    assert db_session.get(User, _OWNER_ID) is None
