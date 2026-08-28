"""current_principal JWT auth (Ring 1-B design.md §6.5, B-UAT-9/10)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.database import get_session
from app.core.deps import current_principal
from app.main import app
from app.models.user import User
from app.services.auth_provider import AccessTokenClaims, InvalidAccessToken
from app.tests.conftest import TEST_USER_ID, U1_USER_ID, U2_USER_ID


def _unauthenticated_client(db_session: Session) -> Iterator[TestClient]:
    def _override_session() -> Iterator[Session]:
        yield db_session

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides.pop(current_principal, None)
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def raw_client(db_session: Session) -> Iterator[TestClient]:
    yield from _unauthenticated_client(db_session)


def _add_user(
    session: Session,
    *,
    user_id: uuid.UUID,
    email: str,
    auth_subject: str,
) -> User:
    row = User(
        id=user_id,
        auth_provider="supabase",
        auth_subject=auth_subject,
        email=email,
        status="active",
        locale="zh",
        base_currency="USD",
        report_cadence="mwf",
    )
    session.add(row)
    session.flush()
    return row


def test_verify_access_token_rejects_legacy_hs256() -> None:
    """New Supabase projects sign with ES256/RS256 JWKS. HS256 (the 2026-05
    JWT_SECRET plan) must not be accepted — Ring 1-B §6.5."""
    import inspect

    from app.core.config import Settings
    from app.services.auth_provider import verify_access_token

    source = inspect.getsource(verify_access_token)
    assert 'algorithms=["ES256", "RS256"]' in source
    assert "JWT_SECRET" not in Settings.model_fields


def test_get_current_user_id_hard_fails() -> None:
    from app.core.deps import get_current_user_id

    with pytest.raises(RuntimeError, match="current_principal"):
        get_current_user_id()


def test_holdings_without_token_is_401(raw_client: TestClient) -> None:
    resp = raw_client.get("/holdings")
    assert resp.status_code == 401


def test_reports_without_token_is_401(raw_client: TestClient) -> None:
    resp = raw_client.get("/reports")
    assert resp.status_code == 401


def test_forged_bearer_token_is_401(
    raw_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _reject(_token: str) -> AccessTokenClaims:
        raise InvalidAccessToken("bad signature")

    monkeypatch.setattr("app.core.deps.verify_access_token", _reject)
    resp = raw_client.get("/holdings", headers={"Authorization": "Bearer forged.token"})
    assert resp.status_code == 401


def test_expired_token_is_401(raw_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def _reject(_token: str) -> AccessTokenClaims:
        raise InvalidAccessToken("expired")

    monkeypatch.setattr("app.core.deps.verify_access_token", _reject)
    resp = raw_client.get("/holdings", headers={"Authorization": "Bearer expired.token"})
    assert resp.status_code == 401


def test_known_sub_without_users_row_is_401_and_does_not_insert(
    raw_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hard constraint (§6.9): never auto-create a users row from a valid sub."""
    sub = str(uuid.uuid4())

    def _ok(_token: str) -> AccessTokenClaims:
        return AccessTokenClaims(sub=sub, email="stranger@example.com")

    monkeypatch.setattr("app.core.deps.verify_access_token", _ok)
    before = db_session.execute(select(func.count()).select_from(User)).scalar_one()

    resp = raw_client.get("/holdings", headers={"Authorization": "Bearer valid-but-unknown"})
    assert resp.status_code == 401

    db_session.expire_all()
    after = db_session.execute(select(func.count()).select_from(User)).scalar_one()
    assert after == before
    assert (
        db_session.execute(select(User).where(User.auth_subject == sub)).scalar_one_or_none()
        is None
    )


def test_valid_token_with_users_row_returns_own_holdings(
    raw_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    sub = "supabase-sub-u1"
    _add_user(db_session, user_id=TEST_USER_ID, email="u1@example.com", auth_subject=sub)

    def _ok(_token: str) -> AccessTokenClaims:
        return AccessTokenClaims(sub=sub, email="u1@example.com")

    monkeypatch.setattr("app.core.deps.verify_access_token", _ok)
    resp = raw_client.get("/holdings", headers={"Authorization": "Bearer good.token"})
    assert resp.status_code == 200


def test_active_session_within_idle_window_stays_authenticated(
    raw_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """issue #235: two requests inside the 15-minute idle window both succeed,
    and the second extends the window rather than being measured against
    the first request's timestamp."""
    from app.core import idle_activity

    sub = "supabase-sub-idle-active"
    _add_user(db_session, user_id=TEST_USER_ID, email="idle-active@example.com", auth_subject=sub)

    def _ok(_token: str) -> AccessTokenClaims:
        return AccessTokenClaims(sub=sub, email="idle-active@example.com")

    monkeypatch.setattr("app.core.deps.verify_access_token", _ok)

    # A mutable "current time" rather than an iterator: unrelated library
    # code (e.g. httpx's cookiejar) also calls time.time() during a
    # request, so a call-counted iterator would exhaust on the wrong call.
    clock = {"now": 1_000.0}
    monkeypatch.setattr("app.core.idle_activity.time.time", lambda: clock["now"])

    first = raw_client.get("/holdings", headers={"Authorization": "Bearer good.token"})
    clock["now"] = 1_000.0 + idle_activity.IDLE_TIMEOUT_SECONDS - 1
    second = raw_client.get("/holdings", headers={"Authorization": "Bearer good.token"})
    assert first.status_code == 200
    assert second.status_code == 200


def test_idle_session_beyond_window_is_401(
    raw_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """issue #235: a token that is still cryptographically valid is rejected
    once 15+ minutes pass with no request — the actual server-side
    enforcement the frontend timer alone could never provide."""
    from app.core import idle_activity

    sub = "supabase-sub-idle-expired"
    _add_user(db_session, user_id=TEST_USER_ID, email="idle-expired@example.com", auth_subject=sub)

    def _ok(_token: str) -> AccessTokenClaims:
        return AccessTokenClaims(sub=sub, email="idle-expired@example.com")

    monkeypatch.setattr("app.core.deps.verify_access_token", _ok)

    clock = {"now": 1_000.0}
    monkeypatch.setattr("app.core.idle_activity.time.time", lambda: clock["now"])

    first = raw_client.get("/holdings", headers={"Authorization": "Bearer good.token"})
    clock["now"] = 1_000.0 + idle_activity.IDLE_TIMEOUT_SECONDS + 1
    second = raw_client.get("/holdings", headers={"Authorization": "Bearer good.token"})
    assert first.status_code == 200
    assert second.status_code == 401


def test_relogin_after_idle_401_succeeds_immediately(
    raw_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PR #240 review (blacktomb42) ship-blocker: after an idle 401, a real
    re-login must work right away, not stay 401 until the 24h GC TTL. The
    old token is idle-rejected; a fresh token for the same user (newer
    iat) succeeds and resets the window."""
    from app.core import idle_activity

    sub = "supabase-sub-relogin"
    _add_user(db_session, user_id=TEST_USER_ID, email="relogin@example.com", auth_subject=sub)

    old_token_iat = 1_000.0

    def _old_token(_token: str) -> AccessTokenClaims:
        return AccessTokenClaims(sub=sub, email="relogin@example.com", iat=int(old_token_iat))

    monkeypatch.setattr("app.core.deps.verify_access_token", _old_token)
    clock = {"now": old_token_iat}
    monkeypatch.setattr("app.core.idle_activity.time.time", lambda: clock["now"])

    first = raw_client.get("/holdings", headers={"Authorization": "Bearer old.token"})
    assert first.status_code == 200

    clock["now"] = old_token_iat + idle_activity.IDLE_TIMEOUT_SECONDS + 1
    idle = raw_client.get("/holdings", headers={"Authorization": "Bearer old.token"})
    assert idle.status_code == 401

    new_token_iat = clock["now"] + 5

    def _new_token(_token: str) -> AccessTokenClaims:
        return AccessTokenClaims(sub=sub, email="relogin@example.com", iat=int(new_token_iat))

    monkeypatch.setattr("app.core.deps.verify_access_token", _new_token)
    clock["now"] = new_token_iat + 1
    relogin = raw_client.get("/holdings", headers={"Authorization": "Bearer new.token"})
    assert relogin.status_code == 200


def test_u2_cannot_read_u1_report(
    raw_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B-UAT-10: valid token, other user's report_id → 404, same as unknown id."""
    from app.models.report import Report

    _add_user(db_session, user_id=U1_USER_ID, email="u1@example.com", auth_subject="sub-u1")
    _add_user(db_session, user_id=U2_USER_ID, email="u2@example.com", auth_subject="sub-u2")
    report = Report(
        user_id=U1_USER_ID,
        report_date=date(2026, 8, 19),
        report_type="incremental",
        session_node="after_close",
        status="success",
        report_md="# u1",
    )
    db_session.add(report)
    db_session.flush()

    def _as_u2(_token: str) -> AccessTokenClaims:
        return AccessTokenClaims(sub="sub-u2", email="u2@example.com")

    monkeypatch.setattr("app.core.deps.verify_access_token", _as_u2)
    other = raw_client.get(f"/reports/{report.id}", headers={"Authorization": "Bearer u2.token"})
    missing = raw_client.get(
        f"/reports/{uuid.uuid4()}", headers={"Authorization": "Bearer u2.token"}
    )
    assert other.status_code == 404
    assert missing.status_code == 404
    assert other.json() == missing.json()
