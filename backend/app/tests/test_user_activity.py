"""Issue #880 acceptance contracts, backed by PostgreSQL."""

import hashlib
from contextlib import contextmanager
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.deps import current_principal
from app.core.timezones import ET, today_et
from app.main import app
from app.models.api_audit_log import ApiAuditLog
from app.models.email_verification import EmailVerification
from app.models.holding import Holding
from app.services import holding_changes, subscription
from app.tests.conftest import TEST_USER_ID, seed_user

if TYPE_CHECKING:
    from app.models.user_activity import UserActivity


def activity(session: Session, event: str) -> list["UserActivity"]:
    from app.models.user_activity import UserActivity

    return list(session.scalars(select(UserActivity).where(UserActivity.event == event)))


def ops_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {get_settings().ADMIN_API_TOKEN.get_secret_value()}"}


@pytest.fixture
def admin_session(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    @contextmanager
    def factory():  # type: ignore[no-untyped-def]
        yield db_session

    monkeypatch.setattr("app.core.database.SessionLocal", factory)


def test_01_login_session(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    user = seed_user(db_session, TEST_USER_ID)
    user.auth_subject = str(uuid4())
    db_session.commit()
    claims = SimpleNamespace(sub=user.auth_subject, session_id="session-one")
    monkeypatch.setattr("app.core.deps.verify_access_token", lambda _: claims)
    monkeypatch.setattr("app.core.deps.is_idle", lambda *a, **k: False)
    monkeypatch.setattr("app.core.deps.session_lifetime_expired", lambda *a, **k: False)
    monkeypatch.setattr("app.core.deps.touch_activity", lambda *a, **k: None)
    backend = Mock()
    backend.set_nx.side_effect = [True, False, True]
    monkeypatch.setattr("app.core.rate_limit.get_backend", lambda: backend)
    request = Request({"type": "http", "headers": [(b"authorization", b"Bearer valid")]})
    current_principal(request, db_session)
    current_principal(request, db_session)
    assert len(activity(db_session, "login")) == 1
    assert user.last_login_at is not None
    claims.session_id = "session-two"
    current_principal(request, db_session)
    assert len(activity(db_session, "login")) == 2
    assert backend.set_nx.call_args.args == ("activity:login:session-two", 8 * 3600)


def test_02_page_view(app_client: TestClient, db_session: Session) -> None:
    seed_user(db_session, TEST_USER_ID)
    response = app_client.post(
        "/me/activity/page", json={"path": "/profile"}, headers={"user-agent": "raw agent"}
    )
    assert response.status_code == 204
    [row] = activity(db_session, "page_view")
    assert row.detail == {"path": "/profile"}
    assert row.client_ip == "testclient" and row.user_agent == "raw agent"
    override = app.dependency_overrides.pop(current_principal)
    try:
        assert app_client.post("/me/activity/page", json={"path": "/profile"}).status_code == 401
    finally:
        app.dependency_overrides[current_principal] = override
    assert len(activity(db_session, "page_view")) == 1


def test_03_holdings_counts(db_session: Session) -> None:
    seed_user(db_session, TEST_USER_ID)
    old = Holding(
        user_id=TEST_USER_ID,
        name="Old private holding",
        ticker="OLD",
        shares=Decimal("2"),
        currency="USD",
        pricing_mode="auto",
        asset_type="stock",
        market="US",
    )
    db_session.add(old)
    db_session.flush()
    before = holding_changes.snapshot([old])
    db_session.delete(old)
    db_session.add(
        Holding(
            user_id=TEST_USER_ID,
            name="New private holding",
            ticker="NEW",
            shares=Decimal("3"),
            currency="USD",
            pricing_mode="auto",
            asset_type="stock",
            market="US",
        )
    )
    holding_changes.record(db_session, TEST_USER_ID, before, source="web")
    db_session.commit()
    [row] = activity(db_session, "holdings_change")
    assert row.detail == {"source": "web", "created": 1, "updated": 0, "deleted": 1}


def test_04_subscription(app_client: TestClient, db_session: Session) -> None:
    user = seed_user(db_session, TEST_USER_ID)
    user.email_verified_at = datetime.now(tz=ET)
    user.credit_gift_balance = Decimal("100")
    db_session.add(
        Holding(
            user_id=TEST_USER_ID,
            name="Private",
            ticker="AAPL",
            shares=Decimal("1"),
            currency="USD",
            pricing_mode="auto",
            asset_type="stock",
            market="US",
        )
    )
    db_session.commit()
    response = app_client.post("/me/subscription", json={"type": "weekly"})
    assert response.status_code == 200, response.text
    [row] = activity(db_session, "subscription")
    assert row.detail == {"action": "subscribe", "plan": "weekly", "source": "web"}
    user.subscription_expires_on = today_et() - timedelta(days=1)
    db_session.commit()
    outcomes = subscription.run_cadence_checks(db_session, "weekly", today_et())
    assert outcomes[0].outcome == "renewed"
    rows = activity(db_session, "subscription")
    assert len(rows) == 2
    assert rows[-1].detail == {"action": "renewed", "plan": "weekly", "source": "system"}


def test_05_download(app_client: TestClient, db_session: Session) -> None:
    seed_user(db_session, TEST_USER_ID)
    assert app_client.get("/holdings/export").status_code == 200
    [row] = activity(db_session, "download")
    assert row.detail == {"kind": "holdings", "format": None}


def test_06_record_failure(
    app_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services import user_activity

    seed_user(db_session, TEST_USER_ID)
    monkeypatch.setattr(user_activity, "UserActivity", Mock(side_effect=RuntimeError("private")))
    assert app_client.get("/holdings/export").status_code == 200
    assert activity(db_session, "download") == []


def test_07_retention(db_session: Session) -> None:
    from app.core.agent_audit import cleanup_user_activity
    from app.models.user_activity import UserActivity

    seed_user(db_session, TEST_USER_ID)
    for days in (91, 89):
        db_session.add(
            UserActivity(
                user_id=TEST_USER_ID,
                event="login",
                detail={},
                occurred_at=datetime.now(tz=ET) - timedelta(days=days),
            )
        )
    db_session.commit()
    assert cleanup_user_activity(db_session) == 1
    assert len(activity(db_session, "login")) == 1


def test_08_purge(db_session: Session) -> None:
    from app.services.user_activity import record
    from app.services.user_purge import purge_user

    seed_user(db_session, TEST_USER_ID)
    record(db_session, TEST_USER_ID, "login")
    db_session.commit()
    purge_user(db_session, TEST_USER_ID)
    db_session.commit()
    assert activity(db_session, "login") == []


def test_09_ops_read(app_client: TestClient, db_session: Session, admin_session: None) -> None:
    from app.services.user_activity import record

    user = seed_user(db_session, TEST_USER_ID)
    record(db_session, user.id, "login")
    db_session.add(
        ApiAuditLog(
            user_id=user.id,
            occurred_at=datetime.now(tz=ET),
            endpoint="/agent/v1/account",
            params={},
            status_code=200,
            item_count=1,
            client_ip="127.0.0.1",
        )
    )
    db_session.commit()
    response = app_client.get(
        "/admin/activity", params={"email": user.email}, headers=ops_headers()
    )
    assert response.status_code == 200, response.text
    rows = response.json()
    assert [row["event"] for row in rows] == ["api_call", "login"]
    assert rows[0]["detail"] == {
        "endpoint": "/agent/v1/account",
        "status_code": 200,
        "item_count": 1,
    }
    assert app_client.get("/admin/activity").status_code == 401


def test_11_unbound_verification(
    app_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = "ops-confirmation"
    row = EmailVerification(
        user_id=None,
        purpose="ops_manual",
        email="probe@example.com",
        token_hash=hashlib.sha256(token.encode()).hexdigest(),
        status="pending",
        expires_at=datetime.now(tz=ET) + timedelta(hours=1),
        last_sent_at=datetime.now(tz=ET),
        resend_count=0,
    )
    db_session.add(row)
    db_session.commit()
    monkeypatch.setattr(
        "app.services.email_verification.verify_email_verification_solution", lambda _: True
    )
    response = app_client.post(
        "/email-verifications/confirm", json={"token": token, "altcha": "solved"}
    )
    assert response.status_code == 200, response.text
    [entry] = activity(db_session, "email")
    assert entry.user_id is None
    assert entry.detail == {"action": "verified", "purpose": "ops_manual"}


def test_12_admin_and_anonymous_api(
    app_client: TestClient, db_session: Session, admin_session: None
) -> None:
    from app.models.user_activity import UserActivity

    url = "/admin/email-verifications/" + str(uuid4())
    assert app_client.get(url).status_code == 401
    assert app_client.get(url, headers=ops_headers()).status_code == 404
    entries = list(
        db_session.scalars(select(UserActivity).where(UserActivity.event == "admin_call"))
    )
    assert [row.detail["status_code"] for row in entries] == [401, 404]
    assert all(
        row.detail["endpoint"] == "/admin/email-verifications/{verification_id}" for row in entries
    )
    assert all(row.user_id is None and url not in str(row.detail) for row in entries)
    db_session.add(
        ApiAuditLog(
            user_id=None,
            occurred_at=datetime.now(tz=ET),
            endpoint="/agent/v1/account",
            params={},
            status_code=401,
            client_ip="127.0.0.1",
        )
    )
    db_session.commit()
    response = app_client.get("/admin/activity", headers=ops_headers())
    assert response.status_code == 200
    assert {row["event"] for row in response.json()} == {"admin_call", "api_call"}
    assert all(row["user_id"] is None for row in response.json())


def test_13_unknown_email(app_client: TestClient, admin_session: None) -> None:
    response = app_client.get(
        "/admin/activity", params={"email": "unknown@example.com"}, headers=ops_headers()
    )
    assert response.status_code == 200
    assert response.json() == []
