"""Jade A5 session and agent snapshot access."""

from collections.abc import Iterator
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.services.auth_provider import AccessTokenClaims
from app.tests.conftest import TEST_USER_ID, seed_user
from app.tests.test_agent_api import bearer, create
from app.tests.test_auth_deps import _unauthenticated_client


@pytest.fixture
def real_auth_client(db_session: Session) -> Iterator[TestClient]:
    yield from _unauthenticated_client(db_session)


@pytest.mark.parametrize(
    "plan,advanced,jade", [("jade", True, True), ("daily", True, False), ("weekly", False, False)]
)
def test_a5_session(
    real_auth_client: TestClient,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
    plan: str,
    advanced: bool,
    jade: bool,
) -> None:
    u = seed_user(db_session, TEST_USER_ID)
    u.auth_subject = "jade-session"
    u.subscription_status = "active"
    u.subscription_type = plan
    u.report_cadence = "weekly"
    db_session.flush()
    monkeypatch.setattr(
        "app.core.deps.verify_access_token",
        lambda _: AccessTokenClaims(sub="jade-session", email=u.email, session_id="jade-session"),
    )
    r = real_auth_client.get("/auth/session-status", headers={"Authorization": "Bearer test.token"})
    assert r.status_code == 200
    assert r.json() == {"advanced": advanced, "jade": jade}


def test_a5_agent_snapshot(app_client: TestClient, db_session: Session) -> None:
    u = seed_user(db_session, TEST_USER_ID)
    u.subscription_status = "active"
    u.subscription_type = "jade"
    u.report_cadence = "weekly"
    db_session.flush()
    token = create(app_client)
    day = date(2026, 10, 8).isoformat()
    r = app_client.get(
        "/agent/v1/snapshots", params={"start": day, "end": day}, headers=bearer(token)
    )
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("pending", [False, True])
def test_jade_access_includes_pending(db_session: Session, pending: bool) -> None:
    from app.services.subscription import is_advanced, is_jade

    u = seed_user(db_session, TEST_USER_ID)
    u.subscription_status = "active"
    u.subscription_type = "jade"
    u.subscription_cancel_pending = pending
    assert is_jade(u) and is_advanced(u)


def test_d10_8_cancelled_session_status(
    real_auth_client: TestClient,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.subscription import run_cadence_checks
    from app.tests.test_jade_subscription import jade_user

    u = jade_user(db_session)
    u.auth_subject = "jade-cancelled-session"
    u.subscription_cancel_pending = True
    db_session.commit()
    run_cadence_checks(db_session, "mwf", date(2026, 11, 18))
    monkeypatch.setattr(
        "app.core.deps.verify_access_token",
        lambda _: AccessTokenClaims(
            sub="jade-cancelled-session", email=u.email, session_id="jade-cancelled-session"
        ),
    )
    r = real_auth_client.get("/auth/session-status", headers={"Authorization": "Bearer test.token"})
    assert r.status_code == 200
    assert r.json() == {"advanced": False, "jade": False}
