"""POST/GET/DELETE /admin/invites (Ring 1-B design.md §6.4)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.services.invites import hash_invite_token
from app.tests.test_admin_router import _headers


def test_create_invite_requires_ops_token(app_client: TestClient) -> None:
    resp = app_client.post("/admin/invites", json={})
    assert resp.status_code == 401


def test_create_invite_returns_plaintext_token_once(
    app_client: TestClient, db_session: Session
) -> None:
    resp = app_client.post("/admin/invites", headers=_headers(), json={})
    assert resp.status_code == 201
    body = resp.json()
    assert body["token"]
    assert "token_hash" not in body
    assert body["id"]
    listed = app_client.get("/admin/invites", headers=_headers())
    assert listed.status_code == 200
    rows = listed.json()
    assert any(row["id"] == body["id"] for row in rows)
    assert all("token" not in row or row["token"] is None for row in rows)
    assert body["token"] not in listed.text
    from app.models.invite import Invite

    row = db_session.get(Invite, body["id"])
    assert row is not None
    assert row.token_hash == hash_invite_token(body["token"])


def test_create_invite_optional_email(app_client: TestClient) -> None:
    resp = app_client.post(
        "/admin/invites", headers=_headers(), json={"email": "Guest@Example.COM"}
    )
    assert resp.status_code == 201
    assert resp.json()["email"] == "guest@example.com"


def test_revoke_invite(app_client: TestClient, db_session: Session) -> None:
    created = app_client.post("/admin/invites", headers=_headers(), json={})
    invite_id = created.json()["id"]
    resp = app_client.delete(f"/admin/invites/{invite_id}", headers=_headers())
    assert resp.status_code == 204
    from app.models.invite import Invite

    row = db_session.get(Invite, invite_id)
    assert row is not None
    assert row.revoked_at is not None


def test_list_invites_requires_ops_token(app_client: TestClient) -> None:
    resp = app_client.get("/admin/invites")
    assert resp.status_code == 401


def test_create_invite_custom_expiry(app_client: TestClient) -> None:
    resp = app_client.post("/admin/invites", headers=_headers(), json={"expires_days": 3})
    assert resp.status_code == 201
    expires = datetime.fromisoformat(resp.json()["expires_at"])
    expected = datetime.now(tz=UTC) + timedelta(days=3)
    assert abs((expires - expected).total_seconds()) < 60


def test_bind_subject_sets_null_auth_subject(app_client: TestClient, db_session: Session) -> None:
    from app.models.user import User
    from app.tests.test_user_scope import _user

    uid = uuid.UUID("00000000-0000-0000-0000-0000000000b1")
    row = _user(uid, "seed@example.com")
    row.auth_subject = None
    db_session.add(row)
    db_session.flush()

    resp = app_client.post(
        f"/admin/users/{uid}/bind-subject",
        headers=_headers(),
        json={"auth_subject": "supabase-sub-seed"},
    )
    assert resp.status_code == 200
    db_session.expire_all()
    bound = db_session.get(User, uid)
    assert bound is not None
    assert bound.auth_subject == "supabase-sub-seed"


def test_bind_subject_rejects_already_bound(app_client: TestClient, db_session: Session) -> None:
    from app.tests.test_user_scope import _user

    uid = uuid.UUID("00000000-0000-0000-0000-0000000000b1")
    db_session.add(_user(uid, "seed@example.com"))
    db_session.flush()
    resp = app_client.post(
        f"/admin/users/{uid}/bind-subject",
        headers=_headers(),
        json={"auth_subject": "another-sub"},
    )
    assert resp.status_code == 409


def test_bind_subject_requires_ops_token(app_client: TestClient) -> None:
    resp = app_client.post(
        "/admin/users/00000000-0000-0000-0000-0000000000b1/bind-subject",
        json={"auth_subject": "x"},
    )
    assert resp.status_code == 401


def test_admin_invites_use_ops_token_not_user_session(app_client: TestClient) -> None:
    """Creating an invite with only a user-looking bearer (the ops token
    is required) — a random bearer that is not ADMIN_API_TOKEN is 401."""
    resp = app_client.post(
        "/admin/invites",
        headers={"Authorization": "Bearer user-jwt-not-ops"},
        json={},
    )
    assert resp.status_code == 401
    assert get_settings().ADMIN_API_TOKEN.get_secret_value() != "user-jwt-not-ops"
