"""POST /vigil/configurations HTTP contract (issue #454, Vigil R0 P2.1).

Issue #524 (#516 finding 2) removed save-time DNS/MX validation — no DNS
mock is needed in this suite anymore. `test_configuration_save_needs_no_
dns` below is the regression guard: recipients at a domain with no real
mail route must still save successfully, since save-time DNS/MX no longer
gates it.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.user import User
from app.models.vigil import VigilVault
from app.tests.conftest import TEST_USER_ID


@pytest.fixture(autouse=True)
def _vigil_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import get_settings

    monkeypatch.setenv("VIGIL_MODE", "active")
    monkeypatch.setenv("VIGIL_OWNER_AUTH_SUBJECT", "owner-sub")
    monkeypatch.setenv("VIGIL_ENCRYPTION_KEY", Fernet.generate_key().decode())
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _owner_user(db_session: Session) -> User:
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
    return row


def _payload(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "expected_revision": 0,
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
