"""POST /email-verifications (issue #289, Ring 1-Profile Page.md §10):
session-authenticated creation of a fresh verification for one of the
caller's own known email fields — the self-service recovery path after the
only verified address was revoked (§3.7 / Ring 1-Email Validation.md §3.7).
The client never supplies an address; the server resolves it from the
principal's own `users` row (purpose=account_email|delivery_email)."""

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


def _user(
    user_id: uuid.UUID,
    email: str,
    *,
    delivery_email: str | None = None,
    email_verified_at: datetime | None = None,
) -> User:
    return User(
        id=user_id,
        auth_provider="supabase",
        auth_subject=f"sub-{user_id}",
        email=email,
        delivery_email=delivery_email,
        email_verified_at=email_verified_at,
        status="active",
        locale="zh",
        base_currency="USD",
        report_cadence="mwf",
    )


@pytest.fixture
def _self_user(db_session: Session) -> None:
    db_session.add(_user(TEST_USER_ID, "me@example.com", delivery_email="reports@example.com"))
    db_session.flush()


def test_create_requires_auth(db_session: Session) -> None:
    """No current_principal override (unlike app_client) — a real
    unauthenticated request must 401, exactly as production's cutover
    behavior demands."""

    def _override_session() -> Iterator[Session]:
        yield db_session

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides.pop(current_principal, None)
    try:
        client = TestClient(app)
        resp = client.post("/email-verifications", json={"purpose": "account_email"})
        assert resp.status_code == 401
    finally:
        app.dependency_overrides.clear()


def test_create_account_email_succeeds(
    app_client: TestClient, db_session: Session, _self_user: None
) -> None:
    resp = app_client.post("/email-verifications", json={"purpose": "account_email"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "pending"
    assert "expires_at" in body
    assert "token" not in body
    row = db_session.query(EmailVerification).one()
    assert row.user_id == TEST_USER_ID
    assert row.purpose == "account_email"
    assert row.email == "me@example.com"


def test_create_delivery_email_succeeds(
    app_client: TestClient, db_session: Session, _self_user: None
) -> None:
    resp = app_client.post("/email-verifications", json={"purpose": "delivery_email"})

    assert resp.status_code == 200
    row = db_session.query(EmailVerification).one()
    assert row.purpose == "delivery_email"
    assert row.email == "reports@example.com"


def test_create_delivery_email_without_delivery_email_is_422(
    app_client: TestClient, db_session: Session
) -> None:
    db_session.add(_user(TEST_USER_ID, "me@example.com"))
    db_session.flush()

    resp = app_client.post("/email-verifications", json={"purpose": "delivery_email"})

    assert resp.status_code == 422
    assert db_session.query(EmailVerification).count() == 0


def test_create_invalid_purpose_is_422(
    app_client: TestClient, db_session: Session, _self_user: None
) -> None:
    """ops_manual is a valid mechanism purpose but never creatable by a user
    session — only account_email/delivery_email map to the caller's own
    known fields."""
    resp = app_client.post("/email-verifications", json={"purpose": "ops_manual"})

    assert resp.status_code == 422
    assert db_session.query(EmailVerification).count() == 0


def test_create_ignores_client_supplied_email_field(
    app_client: TestClient, db_session: Session, _self_user: None
) -> None:
    """The request shape has no email field — an extra client-supplied
    address is ignored, never used: the server always resolves the target
    from the principal's own users row (issue #289 design comment)."""
    resp = app_client.post(
        "/email-verifications",
        json={"purpose": "account_email", "email": "attacker@example.com"},
    )

    assert resp.status_code == 200
    row = db_session.query(EmailVerification).one()
    assert row.email == "me@example.com"


def test_create_allowed_when_already_verified_leaves_verified_state_untouched(
    app_client: TestClient, db_session: Session
) -> None:
    """Mirrors the Ops API's 'resend doesn't unverify' behavior (Ring
    1-Profile Page.md §9.8 decision #1): the new pending record sits
    alongside the verified state; email_verified_at is not cleared."""
    verified_at = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
    db_session.add(_user(TEST_USER_ID, "me@example.com", email_verified_at=verified_at))
    db_session.flush()

    resp = app_client.post("/email-verifications", json={"purpose": "account_email"})

    assert resp.status_code == 200
    row = db_session.query(EmailVerification).one()
    assert row.status == "pending"
    db_session.expire_all()
    user = db_session.get(User, TEST_USER_ID)
    assert user is not None
    assert user.email_verified_at == verified_at


def test_create_supersedes_prior_pending_record(
    app_client: TestClient, db_session: Session, _self_user: None
) -> None:
    prior = EmailVerification(
        user_id=TEST_USER_ID,
        purpose="account_email",
        email="me@example.com",
        token_hash="hash-old",
        status="pending",
        expires_at=datetime.now(UTC) + timedelta(hours=24),
        last_sent_at=datetime.now(UTC) - timedelta(minutes=5),
        resend_count=0,
    )
    db_session.add(prior)
    db_session.flush()

    resp = app_client.post("/email-verifications", json={"purpose": "account_email"})

    assert resp.status_code == 200
    db_session.expire_all()
    old = db_session.get(EmailVerification, prior.id)
    assert old is not None
    assert old.status == "superseded"
    live = db_session.query(EmailVerification).filter(EmailVerification.status == "pending").one()
    assert live.id != prior.id
    assert live.email == "me@example.com"


def test_create_maps_create_verification_failures(
    app_client: TestClient,
    db_session: Session,
    _self_user: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.routers.email_verification as router_module

    too_soon = MagicMock(side_effect=ResendTooSoon)
    monkeypatch.setattr(router_module, "create_verification", too_soon)
    resp = app_client.post("/email-verifications", json={"purpose": "account_email"})
    assert resp.status_code == 429

    send_failed = MagicMock(side_effect=VerificationSendFailed)
    monkeypatch.setattr(router_module, "create_verification", send_failed)
    resp = app_client.post("/email-verifications", json={"purpose": "account_email"})
    assert resp.status_code == 502


def test_create_rate_limited_after_three_calls(
    app_client: TestClient,
    db_session: Session,
    _self_user: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reuses the resend endpoint's per-user Redis bucket verbatim — the
    same 3/hour allowance, not a separate one (issue #289 design comment:
    reuse the exact limits, do not invent a new limit for this endpoint)."""
    import app.routers.email_verification as router_module
    from app.core import rate_limit as rl

    stub_return = EmailVerification(
        user_id=TEST_USER_ID,
        purpose="account_email",
        email="me@example.com",
        token_hash="hash-stub",
        status="pending",
        expires_at=datetime.now(UTC) + timedelta(hours=24),
        last_sent_at=datetime.now(UTC),
        resend_count=0,
    )
    stub_return.id = uuid.uuid4()
    ok = MagicMock(return_value=stub_return)
    monkeypatch.setattr(router_module, "create_verification", ok)

    for _ in range(rl.RESEND_VERIFICATION_USER_HOUR_LIMIT):
        resp = app_client.post("/email-verifications", json={"purpose": "account_email"})
        assert resp.status_code == 200
    resp = app_client.post("/email-verifications", json={"purpose": "account_email"})
    assert resp.status_code == 429
    ok.assert_called()
