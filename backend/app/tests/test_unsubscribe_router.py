"""GET/POST /unsubscribe/* (issue #257, design doc §3.7).

Unauthenticated confirm-page flow: GET is inert, POST is the only write.
No Altcha — the token is the credential and the click is the anti-prefetch
gate.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.email_verification import EmailVerification
from app.models.user import User
from app.services.unsubscribe_token import create_token

_UID = uuid.UUID("00000000-0000-0000-0000-0000000000e8")
_NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)


def _user(
    *,
    email: str = "acct@example.com",
    delivery_email: str | None = "delivery@example.com",
) -> User:
    verified = datetime(2026, 8, 30, tzinfo=UTC)
    return User(
        id=_UID,
        auth_provider="supabase",
        auth_subject=f"sub-{_UID}",
        email=email,
        delivery_email=delivery_email,
        status="active",
        locale="zh",
        base_currency="USD",
        report_cadence="mwf",
        email_verified_at=verified,
        delivery_email_verified_at=verified,
    )


def _verified_row(*, purpose: str, email: str) -> EmailVerification:
    now = datetime.now(UTC)
    return EmailVerification(
        user_id=_UID,
        purpose=purpose,
        email=email,
        token_hash=f"verified-hash-{purpose}-{email}",
        status="verified",
        expires_at=now + timedelta(days=2),
        verified_at=now,
        last_sent_at=now,
        resend_count=0,
    )


def test_status_unknown_token_reports_not_found(app_client: TestClient) -> None:
    resp = app_client.get("/unsubscribe/status", params={"token": "garbage"})
    assert resp.status_code == 200
    assert resp.json() == {"found": False, "email": None}


def test_status_valid_token_is_inert(app_client: TestClient, db_session: Session) -> None:
    db_session.add(_user())
    db_session.commit()
    token = create_token(user_id=_UID, purpose="delivery_email", email="delivery@example.com")

    before = db_session.get(User, _UID)
    assert before is not None
    verified_at = before.delivery_email_verified_at
    row_count = db_session.scalar(select(func.count()).select_from(EmailVerification))

    resp = app_client.get("/unsubscribe/status", params={"token": token})

    assert resp.status_code == 200
    assert resp.json() == {"found": True, "email": "delivery@example.com"}
    db_session.expire_all()
    after = db_session.get(User, _UID)
    assert after is not None
    assert after.delivery_email_verified_at == verified_at
    assert after.email_verified_at is not None
    assert db_session.scalar(select(func.count()).select_from(EmailVerification)) == row_count


def test_status_expired_token_reports_not_found(app_client: TestClient) -> None:
    token = create_token(
        user_id=_UID,
        purpose="account_email",
        email="acct@example.com",
        now=_NOW - timedelta(days=8),
    )
    resp = app_client.get("/unsubscribe/status", params={"token": token})
    assert resp.status_code == 200
    assert resp.json() == {"found": False, "email": None}


def test_confirm_revokes_delivery_email_only(app_client: TestClient, db_session: Session) -> None:
    db_session.add(_user())
    db_session.add(_verified_row(purpose="delivery_email", email="delivery@example.com"))
    db_session.add(_verified_row(purpose="account_email", email="acct@example.com"))
    db_session.commit()
    token = create_token(user_id=_UID, purpose="delivery_email", email="delivery@example.com")

    resp = app_client.post("/unsubscribe/confirm", json={"token": token})

    assert resp.status_code == 200
    assert resp.json() == {"email": "delivery@example.com"}
    db_session.expire_all()
    user = db_session.get(User, _UID)
    assert user is not None
    assert user.delivery_email_verified_at is None
    assert user.email_verified_at is not None

    rows = db_session.execute(select(EmailVerification)).scalars().all()
    by_status = {(r.purpose, r.status, r.email) for r in rows}
    assert ("delivery_email", "verified", "delivery@example.com") in by_status
    assert ("account_email", "verified", "acct@example.com") in by_status
    assert ("delivery_email", "revoked", "delivery@example.com") in by_status
    assert ("account_email", "revoked", "acct@example.com") not in by_status


def test_confirm_revokes_account_email_only(app_client: TestClient, db_session: Session) -> None:
    db_session.add(_user())
    db_session.commit()
    token = create_token(user_id=_UID, purpose="account_email", email="acct@example.com")

    resp = app_client.post("/unsubscribe/confirm", json={"token": token})

    assert resp.status_code == 200
    db_session.expire_all()
    user = db_session.get(User, _UID)
    assert user is not None
    assert user.email_verified_at is None
    assert user.delivery_email_verified_at is not None


def test_confirm_does_not_clear_a_replaced_address(
    app_client: TestClient, db_session: Session
) -> None:
    """Per-address scope: a token for an old delivery address must not
    wipe verification of a different address now on the row."""
    db_session.add(_user(delivery_email="new@example.com"))
    db_session.commit()
    token = create_token(user_id=_UID, purpose="delivery_email", email="old@example.com")

    resp = app_client.post("/unsubscribe/confirm", json={"token": token})

    assert resp.status_code == 200
    db_session.expire_all()
    user = db_session.get(User, _UID)
    assert user is not None
    assert user.delivery_email_verified_at is not None
    revoked = db_session.execute(
        select(EmailVerification).where(EmailVerification.status == "revoked")
    ).scalar_one()
    assert revoked.email == "old@example.com"


def test_confirm_bad_token_returns_generic_400(app_client: TestClient) -> None:
    resp = app_client.post("/unsubscribe/confirm", json={"token": "nope"})
    assert resp.status_code == 400
    assert resp.json()["detail"] == "invalid or expired unsubscribe link"


def test_confirm_body_is_token_only_no_altcha(app_client: TestClient, db_session: Session) -> None:
    db_session.add(_user())
    db_session.commit()
    token = create_token(user_id=_UID, purpose="delivery_email", email="delivery@example.com")
    resp = app_client.post(
        "/unsubscribe/confirm", json={"token": token, "altcha": "should-be-ignored"}
    )
    assert resp.status_code == 200
