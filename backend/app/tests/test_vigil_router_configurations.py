"""POST /vigil/configurations HTTP contract (issue #454, Vigil R0 P2.1).

Issue #524 (#516 finding 2) removed save-time DNS/MX validation — no DNS
mock is needed in this suite anymore. `test_configuration_save_needs_no_
dns` below is the regression guard: recipients at a domain with no real
mail route must still save successfully, since save-time DNS/MX no longer
gates it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.user import User
from app.models.vigil import VigilVault
from app.tests.conftest import TEST_USER_ID
from app.tests.vigil_helpers import ensure_confirmation_email

_EMAIL_ID = uuid.UUID("00000000-0000-4000-8000-000000000539")


@pytest.fixture(autouse=True)
def _vigil_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import get_settings

    monkeypatch.setenv("VIGIL_MODE", "active")
    monkeypatch.setenv("VIGIL_OWNER_AUTH_SUBJECT", "owner-sub")
    monkeypatch.setenv("VIGIL_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("VIGIL_NOTIFICATION_KEY", Fernet.generate_key().decode())
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _owner_user(db_session: Session, _vigil_configured: None) -> User:
    row = User(
        id=TEST_USER_ID,
        auth_provider="supabase",
        auth_subject="owner-sub",
        email="owner@example.com",
        status="active",
        locale="zh",
        base_currency="USD",
        report_cadence="mwf",
        email_verified_at=datetime.now(UTC),
    )
    db_session.add(row)
    db_session.flush()
    ensure_confirmation_email(db_session, owner_user_id=TEST_USER_ID, email_id=_EMAIL_ID)
    return row


def _payload(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "expected_revision": 0,
        "confirmation_email_id": str(_EMAIL_ID),
        "recipients": [{"email": "a@example.com", "email_confirm": "a@example.com"}],
    }
    base.update(overrides)
    return base


def test_first_configuration_creates_vault_and_returns_201(
    app_client: TestClient, db_session: Session
) -> None:
    resp = app_client.post("/vigil/configurations", json=_payload())
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["revision"] == 1
    assert "vault_id" in body and "config_id" in body

    vault = db_session.query(VigilVault).one()
    assert vault.pending_config_id is not None


def test_second_configuration_uses_returned_revision(
    app_client: TestClient, db_session: Session
) -> None:
    first = app_client.post("/vigil/configurations", json=_payload()).json()
    resp = app_client.post(
        "/vigil/configurations",
        json=_payload(
            expected_revision=first["revision"],
            recipients=[{"email": "b@example.com", "email_confirm": "b@example.com"}],
        ),
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["revision"] == 2


def test_stale_expected_revision_returns_409_with_current_revision(
    app_client: TestClient, db_session: Session
) -> None:
    app_client.post("/vigil/configurations", json=_payload())
    resp = app_client.post("/vigil/configurations", json=_payload(expected_revision=0))
    assert resp.status_code == 409
    assert resp.json()["detail"]["current_revision"] == 1


def test_invalid_interval_days_returns_422(app_client: TestClient) -> None:
    resp = app_client.post("/vigil/configurations", json=_payload(interval_days=5))
    assert resp.status_code == 422


def test_email_confirm_mismatch_returns_422(app_client: TestClient) -> None:
    resp = app_client.post(
        "/vigil/configurations",
        json=_payload(recipients=[{"email": "a@example.com", "email_confirm": "b@example.com"}]),
    )
    assert resp.status_code == 422


def test_unknown_field_returns_422(app_client: TestClient) -> None:
    resp = app_client.post("/vigil/configurations", json=_payload(unexpected_field="nope"))
    assert resp.status_code == 422


def test_configuration_save_needs_no_dns(app_client: TestClient) -> None:
    """A syntactically valid recipient at a domain with no real mail route
    still saves — save-time DNS/MX no longer gates configuration save."""
    resp = app_client.post(
        "/vigil/configurations",
        json=_payload(
            recipients=[
                {
                    "email": "a@no-such-mail-route.invalid",
                    "email_confirm": "a@no-such-mail-route.invalid",
                }
            ]
        ),
    )
    assert resp.status_code == 201, resp.text


def test_confirmation_email_rest_lifecycle_is_owner_scoped_and_revisioned(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "app.tasks.vigil_tasks.dispatch_vigil_outbox_task.delay", lambda *args, **kwargs: None
    )
    listed = app_client.get("/vigil/confirmation-emails")
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["emails"]] == [str(_EMAIL_ID)]

    added = app_client.post(
        "/vigil/confirmation-emails",
        json={"expected_revision": 0, "address": "Second@Example.COM"},
    )
    assert added.status_code == 201, added.text
    email_id = added.json()["email"]["id"]
    assert added.json()["email"]["address"] == "second@example.com"
    assert added.json()["email"]["verified_at"] is None

    sent = app_client.post(
        f"/vigil/confirmation-emails/{email_id}/send-verification",
        json={"expected_revision": added.json()["revision"]},
    )
    assert sent.status_code == 202, sent.text

    cancelled = app_client.post(
        f"/vigil/confirmation-emails/{email_id}/cancel-verification",
        json={"expected_revision": sent.json()["revision"]},
    )
    assert cancelled.status_code == 200, cancelled.text
    deleted = app_client.request(
        "DELETE",
        f"/vigil/confirmation-emails/{email_id}",
        json={"expected_revision": cancelled.json()["revision"]},
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["email"] is None


def test_vault_read_returns_non_secret_pending_projection(app_client: TestClient) -> None:
    config = app_client.post("/vigil/configurations", json=_payload()).json()
    initialized = app_client.post(
        "/vigil/objects/init",
        json={
            "expected_revision": config["revision"],
            "config_id": config["config_id"],
            "request_id": str(uuid.uuid4()),
            "filename": "private-will.pdf",
            "plaintext_size": 123,
        },
    )
    assert initialized.status_code == 201, initialized.text
    status = app_client.get("/vigil/vault")
    assert status.status_code == 200
    pending = status.json()["pending"]
    assert pending == {
        "config_id": config["config_id"],
        "object_id": initialized.json()["object_id"],
        "object_status": "staging",
        "confirmation_email_id": str(_EMAIL_ID),
        "confirmation_email_verified": True,
    }
    assert "filename" not in pending
    assert "recipients" not in pending
