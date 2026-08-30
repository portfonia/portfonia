"""POST /email-verifications/{id}/resend (issue #262, Ring 1-Profile
Page.md §8.3): session-authenticated resend of the caller's own actionable
verification records, rate-limited at the router layer."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.database import get_session
from app.core.deps import current_principal
from app.main import app
from app.models.email_verification import EmailVerification
from app.models.user import User
from app.services.email_verification import ResendTooSoon, VerificationSendFailed
from app.tests.conftest import TEST_USER_ID

_OTHER_USER_ID = uuid.UUID("00000000-0000-0000-0000-0000000000aa")


def _user(user_id: uuid.UUID, email: str) -> User:
    return User(
        id=user_id,
        auth_provider="supabase",
        auth_subject=f"sub-{user_id}",
        email=email,
        status="active",
        locale="zh",
        base_currency="USD",
        report_cadence="mwf",
    )


def _verification(
    *,
    user_id: uuid.UUID | None,
    purpose: str = "account_email",
    status: str = "pending",
    email: str = "pending@example.com",
) -> EmailVerification:
    return EmailVerification(
        user_id=user_id,
        purpose=purpose,
        email=email,
        token_hash=f"hash-{uuid.uuid4()}",
        status=status,
        expires_at=datetime.now(UTC) + timedelta(hours=24),
        last_sent_at=datetime.now(UTC) - timedelta(minutes=5),
        resend_count=0,
    )


@pytest.fixture
def _resend_users(db_session: Session) -> None:
    db_session.add(_user(TEST_USER_ID, "me@example.com"))
    db_session.add(_user(_OTHER_USER_ID, "other@example.com"))
    db_session.flush()


def test_resend_requires_auth(db_session: Session) -> None:
    """No current_principal override (unlike app_client) — a real unauthenticated
    request must 401, exactly as production's cutover behavior demands."""

    def _override_session() -> Iterator[Session]:
        yield db_session

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides.pop(current_principal, None)
    try:
        client = TestClient(app)
        resp = client.post(f"/email-verifications/{uuid.uuid4()}/resend")
        assert resp.status_code == 401
    finally:
        app.dependency_overrides.clear()


def test_resend_own_pending_record_succeeds(
    app_client: TestClient, db_session: Session, _resend_users: None
) -> None:
    row = _verification(user_id=TEST_USER_ID)
    db_session.add(row)
    db_session.flush()

    resp = app_client.post(f"/email-verifications/{row.id}/resend")

    assert resp.status_code == 200
    body = resp.json()
    # Supersede semantics: the resend created a NEW live record — the
    # response id must not be the requested one.
    assert body["id"] != str(row.id)
    assert body["status"] == "pending"
    assert "expires_at" in body
    assert "token" not in body
    db_session.expire_all()
    superseded = db_session.get(EmailVerification, row.id)
    assert superseded is not None
    assert superseded.status == "superseded"


def test_resend_own_undeliverable_record_succeeds(
    app_client: TestClient, db_session: Session, _resend_users: None
) -> None:
    row = _verification(user_id=TEST_USER_ID, status="undeliverable")
    db_session.add(row)
    db_session.flush()

    resp = app_client.post(f"/email-verifications/{row.id}/resend")

    assert resp.status_code == 200


def test_resend_other_users_record_is_404(
    app_client: TestClient, db_session: Session, _resend_users: None
) -> None:
    """404, not 403 — never reveal that the id exists but belongs to
    someone else (Profile Page.md §8.3)."""
    row = _verification(user_id=_OTHER_USER_ID)
    db_session.add(row)
    db_session.flush()

    resp = app_client.post(f"/email-verifications/{row.id}/resend")

    assert resp.status_code == 404


def test_resend_unknown_id_is_404(app_client: TestClient, _resend_users: None) -> None:
    resp = app_client.post(f"/email-verifications/{uuid.uuid4()}/resend")
    assert resp.status_code == 404


def test_resend_terminal_status_is_404(
    app_client: TestClient, db_session: Session, _resend_users: None
) -> None:
    """expired/verified/superseded rows are not actionable — same 404 shape
    as a missing record (verified rows also leak nothing about the user)."""
    for status in ("expired", "verified", "superseded"):
        row = _verification(user_id=TEST_USER_ID, status=status)
        db_session.add(row)
    db_session.flush()
    rows = db_session.query(EmailVerification).all()

    for row in rows:
        resp = app_client.post(f"/email-verifications/{row.id}/resend")
        assert resp.status_code == 404


def test_resend_ops_manual_row_is_404(
    app_client: TestClient, db_session: Session, _resend_users: None
) -> None:
    """ops_manual is always unbound (user_id=NULL) — no user owns it, so
    no session can resend it."""
    row = _verification(user_id=None, purpose="ops_manual")
    db_session.add(row)
    db_session.flush()

    resp = app_client.post(f"/email-verifications/{row.id}/resend")

    assert resp.status_code == 404


def test_resend_maps_create_verification_failures(
    app_client: TestClient,
    db_session: Session,
    _resend_users: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _verification(user_id=TEST_USER_ID)
    db_session.add(row)
    db_session.flush()

    import app.routers.email_verification as router_module

    too_soon = MagicMock(side_effect=ResendTooSoon)
    monkeypatch.setattr(router_module, "create_verification", too_soon)
    resp = app_client.post(f"/email-verifications/{row.id}/resend")
    assert resp.status_code == 429

    send_failed = MagicMock(side_effect=VerificationSendFailed)
    monkeypatch.setattr(router_module, "create_verification", send_failed)
    resp = app_client.post(f"/email-verifications/{row.id}/resend")
    assert resp.status_code == 502


def test_resend_rate_limited_after_three_resends(
    app_client: TestClient,
    db_session: Session,
    _resend_users: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The router-layer Redis limiter fires before create_verification —
    with the service call stubbed out, the 4th resend for the same user
    must 429 without any service-level cooldown involved."""
    from app.core import rate_limit as rl

    row = _verification(user_id=TEST_USER_ID)
    db_session.add(row)
    db_session.flush()

    import app.routers.email_verification as router_module

    # The 60s data-driven cooldown inside create_verification must not be
    # what blocks here — stubbing the service out isolates the Redis
    # limiter. (Alert-dedup .delay is already stubbed by the autouse
    # _rate_limit_memory fixture.) The stub's return value must carry a
    # real id/expiry: the router builds its response from it. The model
    # defaults id server-side (gen_random_uuid), so an unflushed instance
    # has id=None — assign one explicitly.
    stub_return = _verification(user_id=TEST_USER_ID, email="fresh@example.com")
    stub_return.id = uuid.uuid4()
    ok = MagicMock(return_value=stub_return)
    monkeypatch.setattr(router_module, "create_verification", ok)

    for _ in range(rl.RESEND_VERIFICATION_USER_HOUR_LIMIT):
        resp = app_client.post(f"/email-verifications/{row.id}/resend")
        assert resp.status_code == 200
    resp = app_client.post(f"/email-verifications/{row.id}/resend")
    assert resp.status_code == 429
    ok.assert_called()
