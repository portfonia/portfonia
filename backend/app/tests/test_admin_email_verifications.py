"""POST/GET /admin/email-verifications (issue #260, Ring 1-Email Validation
design doc §3.5) — Ops API surface for triggering/inspecting verifications
independently of any application-scenario caller (none of which exist yet).
"""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.email_verification import EmailVerification
from app.models.user import User
from app.tests.test_admin_router import _headers

_UID = uuid.UUID("00000000-0000-0000-0000-0000000000f1")


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


def test_create_requires_ops_token(app_client: TestClient) -> None:
    resp = app_client.post("/admin/email-verifications", json={"email": "a@example.com"})
    assert resp.status_code == 401


def test_create_ops_manual_probe_with_no_user_id(app_client: TestClient) -> None:
    resp = app_client.post(
        "/admin/email-verifications",
        headers=_headers(),
        json={"email": "a@example.com"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "pending"
    assert "id" in body and "expires_at" in body
    assert "token" not in body  # never leaks the plaintext token


def test_create_bound_to_known_user_and_purpose(
    app_client: TestClient, db_session: Session
) -> None:
    db_session.add(_user(_UID, "seed@example.com"))
    db_session.flush()

    resp = app_client.post(
        "/admin/email-verifications",
        headers=_headers(),
        json={
            "email": "new-delivery@example.com",
            "purpose": "delivery_email",
            "user_id": str(_UID),
        },
    )

    assert resp.status_code == 201
    verification_id = resp.json()["id"]
    db_session.expire_all()
    row = db_session.get(EmailVerification, uuid.UUID(verification_id))
    assert row is not None
    assert row.user_id == _UID
    assert row.purpose == "delivery_email"


def test_create_404_for_unknown_user_id(app_client: TestClient) -> None:
    resp = app_client.post(
        "/admin/email-verifications",
        headers=_headers(),
        json={"email": "a@example.com", "purpose": "delivery_email", "user_id": str(uuid.uuid4())},
    )
    assert resp.status_code == 404


def test_create_rejects_unknown_purpose(app_client: TestClient) -> None:
    resp = app_client.post(
        "/admin/email-verifications",
        headers=_headers(),
        json={"email": "a@example.com", "purpose": "something_else"},
    )
    assert resp.status_code == 422


def test_get_requires_ops_token(app_client: TestClient) -> None:
    resp = app_client.get(f"/admin/email-verifications/{uuid.uuid4()}")
    assert resp.status_code == 401


def test_get_returns_current_status(app_client: TestClient) -> None:
    created = app_client.post(
        "/admin/email-verifications",
        headers=_headers(),
        json={"email": "a@example.com"},
    )
    verification_id = created.json()["id"]

    resp = app_client.get(f"/admin/email-verifications/{verification_id}", headers=_headers())

    assert resp.status_code == 200
    assert resp.json()["id"] == verification_id
    assert resp.json()["status"] == "pending"


def test_get_response_is_widened_for_diagnosis(app_client: TestClient) -> None:
    """Review, PR #261: the point of GET is post-hoc "why didn't this user
    get their email" diagnosis, which id/status/expires_at alone can't
    answer — widened past that narrow POST shape."""
    created = app_client.post(
        "/admin/email-verifications",
        headers=_headers(),
        json={"email": "a@example.com"},
    )
    verification_id = created.json()["id"]

    resp = app_client.get(f"/admin/email-verifications/{verification_id}", headers=_headers())

    body = resp.json()
    assert body["email"] == "a@example.com"
    assert body["purpose"] == "ops_manual"
    assert body["user_id"] is None
    assert body["provider_message_id"] == "test-provider-id"  # autouse fixture's fake success
    assert "last_sent_at" in body
    assert body["verified_at"] is None


def test_post_response_stays_narrow(app_client: TestClient) -> None:
    """POST's response is deliberately NOT widened the same way GET is —
    still just id/status/expires_at (and never the plaintext token)."""
    resp = app_client.post(
        "/admin/email-verifications",
        headers=_headers(),
        json={"email": "a@example.com"},
    )

    assert set(resp.json().keys()) == {"id", "status", "expires_at"}


def test_create_rejects_bound_purpose_without_user_id(app_client: TestClient) -> None:
    resp = app_client.post(
        "/admin/email-verifications",
        headers=_headers(),
        json={"email": "a@example.com", "purpose": "delivery_email"},
    )
    assert resp.status_code == 422


def test_create_rejects_ops_manual_with_user_id(
    app_client: TestClient, db_session: Session
) -> None:
    db_session.add(_user(_UID, "seed@example.com"))
    db_session.flush()

    resp = app_client.post(
        "/admin/email-verifications",
        headers=_headers(),
        json={"email": "a@example.com", "purpose": "ops_manual", "user_id": str(_UID)},
    )
    assert resp.status_code == 422


def test_create_rejects_blank_email(app_client: TestClient) -> None:
    resp = app_client.post(
        "/admin/email-verifications",
        headers=_headers(),
        json={"email": "   "},
    )
    assert resp.status_code == 422


def test_create_within_60s_of_a_prior_pending_probe_returns_429(app_client: TestClient) -> None:
    first = app_client.post(
        "/admin/email-verifications",
        headers=_headers(),
        json={"email": "a@example.com"},
    )
    assert first.status_code == 201

    resp = app_client.post(
        "/admin/email-verifications",
        headers=_headers(),
        json={"email": "a@example.com"},
    )

    assert resp.status_code == 429


def test_get_404_for_unknown_id(app_client: TestClient) -> None:
    resp = app_client.get(f"/admin/email-verifications/{uuid.uuid4()}", headers=_headers())
    assert resp.status_code == 404
