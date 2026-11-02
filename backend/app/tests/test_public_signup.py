"""Issue #870 public signup and upload limits against real PostgreSQL."""

import uuid
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.deps import Principal, current_principal
from app.main import app
from app.models.credit_ledger import CreditLedgerEntry
from app.models.invite import Invite
from app.models.upload_job import UploadJob
from app.models.user import User
from app.models.waitlist_entry import WaitlistEntry
from app.services import altcha_challenge, invites
from app.tests.conftest import TEST_USER_ID, seed_user
from app.tests.test_altcha_challenge import _solve


@pytest.fixture
def auth(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    create = MagicMock(return_value="public-signup-sub")
    monkeypatch.setattr("app.routers.auth.create_auth_user", create)
    monkeypatch.setattr(
        "app.routers.auth.verify_signup_solution", lambda value: value == "valid", raising=False
    )
    return create


def payload(**extra: object) -> dict[str, object]:
    return {
        "email": "public@example.com",
        "password": "long-password",
        "tos_accepted": True,
        "altcha": "valid",
        **extra,
    }


def test_public_signup(
    app_client: TestClient, db_session: Session, auth: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    delay = MagicMock()
    monkeypatch.setattr(
        "app.tasks.email_verification_tasks.send_account_email_verification_task.delay", delay
    )
    response = app_client.post("/auth/signup", json=payload())
    assert response.status_code == 201
    user = db_session.get(User, uuid.UUID(response.json()["id"]))
    assert user is not None and user.status == "active"
    root = uuid.UUID(get_settings().ADMIN_ID)
    assert (user.invited_by, user.grand_invited_by) == (root, root)
    assert (
        len(
            list(
                db_session.scalars(
                    select(CreditLedgerEntry).where(
                        CreditLedgerEntry.user_id == user.id,
                        CreditLedgerEntry.reason == "signup_grant",
                    )
                )
            )
        )
        == 1
    )
    delay.assert_called_once_with(str(user.id))


def test_referral_token(app_client: TestClient, db_session: Session, auth: MagicMock) -> None:
    referrer = seed_user(db_session, uuid.uuid4())
    entry = WaitlistEntry(
        email="public@example.com", locale="en", source="referral", referrer_user_id=referrer.id
    )
    db_session.add(entry)
    db_session.flush()
    invite = invites.create_invite(db_session, created_by=referrer.id, waitlist_entry_id=entry.id)
    response = app_client.post("/auth/signup", json=payload(invite_token=invite.token))
    assert response.status_code == 201
    user = db_session.get(User, uuid.UUID(response.json()["id"]))
    assert user is not None and user.invited_by == referrer.id
    row = db_session.get(Invite, invite.id)
    assert row is not None and row.used_by_user_id == user.id and row.used_at is not None


def test_used_token(app_client: TestClient, db_session: Session, auth: MagicMock) -> None:
    invite = invites.create_invite(db_session, created_by=uuid.UUID(get_settings().ADMIN_ID))
    previous = seed_user(db_session, uuid.uuid4())
    invites.redeem_invite(db_session, invite.token, used_by=previous.id, email=previous.email)
    row = db_session.get(Invite, invite.id)
    assert row is not None
    db_session.commit()
    before = (row.used_at, row.used_by_user_id)
    response = app_client.post("/auth/signup", json=payload(invite_token=invite.token))
    assert response.status_code == 400
    assert response.json()["detail"] == invites.INVITE_REJECTED_MESSAGE
    db_session.refresh(row)
    assert (row.used_at, row.used_by_user_id) == before
    assert db_session.scalar(select(User).where(User.email == "public@example.com")) is None
    auth.assert_not_called()


def test_captcha_required_and_invalid(
    app_client: TestClient, db_session: Session, auth: MagicMock
) -> None:
    body = payload(invite_token="unused")
    del body["altcha"]
    assert app_client.post("/auth/signup", json=body).status_code == 422
    body["altcha"] = "invalid"
    response = app_client.post("/auth/signup", json=body)
    assert response.status_code == 400
    assert response.json()["detail"] == "invalid captcha"
    assert db_session.scalar(select(User).where(User.email == "public@example.com")) is None
    auth.assert_not_called()


def test_signup_purpose() -> None:
    verify = altcha_challenge.verify_signup_solution
    assert verify(_solve(altcha_challenge.create_forgot_password_challenge())) is False
    assert verify(_solve(altcha_challenge.create_signup_challenge())) is True


def test_public_taken_email(app_client: TestClient, db_session: Session, auth: MagicMock) -> None:
    seed_user(db_session, uuid.uuid4(), "public@example.com")
    response = app_client.post("/auth/signup", json=payload())
    assert response.status_code == 400
    assert response.json()["detail"] == invites.INVITE_REJECTED_MESSAGE
    auth.assert_not_called()


def test_upload_limit(
    app_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_user(db_session, TEST_USER_ID)
    monkeypatch.setattr("app.routers.holdings.parse_holdings_upload.delay", MagicMock())
    for _ in range(10):
        assert (
            app_client.post(
                "/holdings/upload", files={"file": ("holdings.md", b"AAPL 10", "text/plain")}
            ).status_code
            == 202
        )
    before = list(db_session.scalars(select(UploadJob.id)))
    assert (
        app_client.post(
            "/holdings/upload", files={"file": ("holdings.md", b"AAPL 10", "text/plain")}
        ).status_code
        == 429
    )
    assert list(db_session.scalars(select(UploadJob.id))) == before
    other = seed_user(db_session, uuid.uuid4())
    app.dependency_overrides[current_principal] = lambda: Principal(user_id=other.id)
    assert (
        app_client.post(
            "/holdings/upload", files={"file": ("holdings.md", b"AAPL 10", "text/plain")}
        ).status_code
        == 202
    )


def test_waitlist_retired(app_client: TestClient) -> None:
    assert app_client.post("/waitlist", json={}).status_code == 404
