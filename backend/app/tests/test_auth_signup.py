"""POST /auth/signup — invite gate + users insert, never auto-provision."""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.user import User
from app.services.invites import INVITE_REJECTED_MESSAGE, create_invite

_CREATOR = uuid.UUID("00000000-0000-0000-0000-0000000000aa")


@pytest.fixture
def _fake_auth_provider(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    create = MagicMock(return_value="supabase-sub-new")
    delete = MagicMock()
    monkeypatch.setattr("app.routers.auth.create_auth_user", create)
    monkeypatch.setattr("app.routers.auth.delete_auth_user", delete)
    return create


def test_signup_without_invite_rejected(
    app_client: TestClient, db_session: Session, _fake_auth_provider: MagicMock
) -> None:
    before = db_session.execute(select(User)).scalars().all()
    resp = app_client.post(
        "/auth/signup",
        json={
            "invite_token": "no-such-token",
            "email": "new@example.com",
            "password": "a-long-enough-password",
        },
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == INVITE_REJECTED_MESSAGE
    db_session.expire_all()
    after = db_session.execute(select(User)).scalars().all()
    assert len(after) == len(before)
    _fake_auth_provider.assert_not_called()


def test_signup_with_valid_invite_creates_user(
    app_client: TestClient, db_session: Session, _fake_auth_provider: MagicMock
) -> None:
    issued = create_invite(db_session, created_by=_CREATOR)
    db_session.flush()

    resp = app_client.post(
        "/auth/signup",
        json={
            "invite_token": issued.token,
            "email": "New@Example.com",
            "password": "a-long-enough-password",
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["email"] == "new@example.com"
    assert "password" not in body

    row = db_session.execute(select(User).where(User.email == "new@example.com")).scalar_one()
    assert row.auth_subject == "supabase-sub-new"
    assert row.auth_provider == "supabase"
    assert row.status == "active"
    _fake_auth_provider.assert_called_once()


def test_signup_deletes_auth_user_if_db_work_fails_after_create(
    app_client: TestClient,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PR #183 review: compensation must run for any failure after Auth create,
    not only AuthProviderError/IntegrityError — otherwise the Auth user is
    orphaned and retry maps to a generic invalid invite."""
    issued = create_invite(db_session, created_by=_CREATOR)
    db_session.flush()
    create = MagicMock(return_value="sub-to-compensate")
    delete = MagicMock()
    monkeypatch.setattr("app.routers.auth.create_auth_user", create)
    monkeypatch.setattr("app.routers.auth.delete_auth_user", delete)
    monkeypatch.setattr(
        "app.routers.auth.backfill_news_surfaced_before",
        MagicMock(side_effect=RuntimeError("db work failed")),
    )

    with pytest.raises(RuntimeError, match="db work failed"):
        app_client.post(
            "/auth/signup",
            json={
                "invite_token": issued.token,
                "email": "orphan@example.com",
                "password": "a-long-enough-password",
            },
        )
    delete.assert_called_once_with("sub-to-compensate")
    db_session.expire_all()
    assert (
        db_session.execute(
            select(User).where(User.email == "orphan@example.com")
        ).scalar_one_or_none()
        is None
    )


def test_signup_validation_error_does_not_echo_password(app_client: TestClient) -> None:
    """Public 422 must not echo the submitted password (`input` in Pydantic errors)."""
    password = "s3cret7"
    resp = app_client.post(
        "/auth/signup",
        json={
            "invite_token": "x",
            "email": "a@example.com",
            "password": password,
        },
    )
    assert resp.status_code == 422
    assert password not in resp.text


def test_signup_does_not_log_password(
    app_client: TestClient,
    db_session: Session,
    _fake_auth_provider: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    issued = create_invite(db_session, created_by=_CREATOR)
    db_session.flush()
    password = "super-secret-password-xyz"
    with caplog.at_level("DEBUG"):
        resp = app_client.post(
            "/auth/signup",
            json={
                "invite_token": issued.token,
                "email": "safe@example.com",
                "password": password,
            },
        )
    assert resp.status_code == 201
    assert password not in caplog.text
