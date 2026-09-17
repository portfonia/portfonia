"""GET /vigil/vault response shape (issue #452, Vigil R0 P1.2).

Owner-authorization matrix (401/403/503) lives in test_vigil_access.py —
this file only covers the response contract once a call is authorized:
the P1.1 "absent vault" default shape, and that an existing vault row's
real fields are actually read rather than the absent-vault default being
hardcoded regardless of DB state.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
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


def test_vigil_vault_absent_returns_disarmed_defaults_and_creates_nothing(
    app_client: TestClient, db_session: Session
) -> None:
    resp = app_client.get("/vigil/vault")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "vault_id": None,
        "phase": "DISARMED",
        "revision": 0,
        "hold_reason": None,
        "next_check_at": None,
        "deadline_at": None,
        "last_scan_completed_at": None,
        "active": None,
        "pending": None,
        "recipients": [],
        "delivery_status": [],
    }

    count = db_session.query(VigilVault).count()
    assert count == 0


def test_vigil_vault_reads_existing_row_not_a_hardcoded_default(
    app_client: TestClient, db_session: Session
) -> None:
    vault = VigilVault(
        owner_user_id=TEST_USER_ID,
        phase="ARMED",
        revision=3,
        hold_reason="dependency_hold",
    )
    db_session.add(vault)
    db_session.flush()

    resp = app_client.get("/vigil/vault")
    assert resp.status_code == 200
    body = resp.json()
    assert body["vault_id"] == str(vault.id)
    assert body["phase"] == "ARMED"
    assert body["revision"] == 3
    assert body["hold_reason"] == "dependency_hold"
