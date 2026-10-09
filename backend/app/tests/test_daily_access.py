"""Advanced access and authenticated session response acceptance (#650)."""

from collections.abc import Iterator
from datetime import date, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.core.timezones import ET, today_et
from app.services import subscription as s
from app.services.auth_provider import AccessTokenClaims
from app.services.credit_ledger import adjust_by_admin
from app.tests.conftest import TEST_USER_ID, seed_user
from app.tests.test_auth_deps import _unauthenticated_client


@pytest.mark.parametrize(
    "status,plan,pending,expected",
    [
        ("active", "daily", False, 200),
        ("active", "daily", True, 200),
        ("active", "weekly", False, 403),
        ("active", "mwf", False, 403),
        ("expired", "daily", False, 403),
        ("inactive", "daily", False, 403),
        ("cancelled", "daily", False, 403),
    ],
)
def test_daily_acceptance_10_snapshot_access(
    app_client: TestClient,
    db_session: Session,
    status: str,
    plan: str,
    pending: bool,
    expected: int,
) -> None:
    user = seed_user(db_session, TEST_USER_ID)
    user.subscription_status = status
    user.subscription_type = user.report_cadence = plan
    user.subscription_cancel_pending = pending
    user.subscription_expires_on = today_et()
    db_session.flush()
    today = today_et().isoformat()
    response = app_client.get("/portfolio/snapshots", params={"start": today, "end": today})
    assert response.status_code == expected
    if expected == 403:
        assert response.json() == {"detail": "subscription_required"}
    else:
        assert response.json() == {"start": today, "end": today, "days": []}


def test_daily_downgrade_removes_advanced(db_session: Session, app_client: TestClient) -> None:
    user = seed_user(db_session, TEST_USER_ID)
    user.email_verified_at = datetime(2026, 10, 1, tzinfo=ET)
    db_session.flush()
    adjust_by_admin(
        db_session,
        user_id=user.id,
        amount=Decimal("5.00"),
        note="fixture",
        idempotency_key="daily-access-funding",
    )
    s.set_plan(db_session, user.id, date(2026, 10, 1), "daily")
    db_session.commit()
    today = today_et().isoformat()
    assert (
        app_client.get("/portfolio/snapshots", params={"start": today, "end": today}).status_code
        == 200
    )
    s.set_plan(db_session, user.id, date(2026, 10, 8), "weekly")
    db_session.commit()
    response = app_client.get("/portfolio/snapshots", params={"start": today, "end": today})
    assert response.status_code == 403 and response.json()["detail"] == "subscription_required"


@pytest.fixture
def real_auth_client(db_session: Session) -> Iterator[TestClient]:
    yield from _unauthenticated_client(db_session)


@pytest.mark.parametrize("plan,advanced", [("daily", True), ("weekly", False)])
def test_daily_acceptance_11_session_status(
    real_auth_client: TestClient,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
    plan: str,
    advanced: bool,
) -> None:
    user = seed_user(db_session, TEST_USER_ID)
    user.auth_subject = "daily-session-sub"
    user.subscription_status = "active"
    user.subscription_type = user.report_cadence = plan
    db_session.flush()
    monkeypatch.setattr(
        "app.core.deps.verify_access_token",
        lambda _: AccessTokenClaims(
            sub="daily-session-sub", email=user.email, session_id="daily-session"
        ),
    )
    statements: list[str] = []

    def record(
        connection: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        statements.append(statement)

    connection = db_session.connection()
    event.listen(connection, "before_cursor_execute", record)
    try:
        response = real_auth_client.get(
            "/auth/session-status", headers={"Authorization": "Bearer test.token"}
        )
    finally:
        event.remove(connection, "before_cursor_execute", record)
    assert response.status_code == 200
    assert response.json() == {"advanced": advanced, "jade": False}
    assert len([statement for statement in statements if "FROM users" in statement]) == 1
    assert real_auth_client.get("/auth/session-status").status_code == 401


@pytest.mark.parametrize("status", ["expired", "cancelled", "inactive"])
def test_daily_inactive_session_not_advanced(
    real_auth_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    user = seed_user(db_session, TEST_USER_ID)
    user.subscription_type = "daily"
    user.subscription_status = status
    user.auth_subject = "daily-inactive-sub"
    db_session.flush()
    monkeypatch.setattr(
        "app.core.deps.verify_access_token",
        lambda _: AccessTokenClaims(
            sub="daily-inactive-sub", email=user.email, session_id="daily-session"
        ),
    )
    assert real_auth_client.get(
        "/auth/session-status", headers={"Authorization": "Bearer test.token"}
    ).json() == {"advanced": False, "jade": False}
