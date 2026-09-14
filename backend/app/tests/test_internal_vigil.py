"""P1.2: GET /internal/vigil/principals/{auth_subject} (issue #452)."""

from __future__ import annotations

import secrets as secrets_module
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import get_session
from app.core.deps import current_principal, require_vigil_identity_token
from app.main import app
from app.models.user import User


def _identity_token() -> str:
    return get_settings().VIGIL_IDENTITY_SERVICE_TOKEN.get_secret_value()


def _auth_header(token: str | None = None) -> dict[str, str]:
    return {"Authorization": f"Bearer {token or _identity_token()}"}


@pytest.fixture
def vigil_client(db_session: Session) -> Iterator[TestClient]:
    def _override_session() -> Iterator[Session]:
        yield db_session

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides.pop(current_principal, None)
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _add_user(
    session: Session,
    *,
    user_id: uuid.UUID,
    auth_subject: str,
    email: str,
    status: str = "active",
    email_verified_at: datetime | None = None,
    delivery_email: str | None = "reports-only@example.com",
    delivery_email_verified_at: datetime | None = None,
) -> User:
    row = User(
        id=user_id,
        auth_provider="supabase",
        auth_subject=auth_subject,
        email=email,
        status=status,
        locale="zh",
        base_currency="USD",
        report_cadence="mwf",
        email_verified_at=email_verified_at,
        delivery_email=delivery_email,
        delivery_email_verified_at=delivery_email_verified_at,
    )
    session.add(row)
    session.flush()
    return row


def test_missing_authorization_header_rejected() -> None:
    with pytest.raises(HTTPException) as exc_info:
        require_vigil_identity_token(authorization=None)
    assert exc_info.value.status_code == 401


def test_wrong_token_rejected() -> None:
    with pytest.raises(HTTPException) as exc_info:
        require_vigil_identity_token(authorization="Bearer not-the-real-token")
    assert exc_info.value.status_code == 401


def test_correct_token_accepted() -> None:
    require_vigil_identity_token(authorization=f"Bearer {_identity_token()}")


def test_comparison_uses_compare_digest_not_eq(monkeypatch: pytest.MonkeyPatch) -> None:
    spy = MagicMock(wraps=secrets_module.compare_digest)
    monkeypatch.setattr("app.core.deps.secrets.compare_digest", spy)
    require_vigil_identity_token(authorization=f"Bearer {_identity_token()}")
    assert spy.called


def test_non_ascii_bearer_token_rejected_not_raised() -> None:
    with pytest.raises(HTTPException) as exc_info:
        require_vigil_identity_token(authorization="Bearer café")
    assert exc_info.value.status_code == 401


def test_vigil_identity_token_blank_fails_at_settings_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VIGIL_IDENTITY_SERVICE_TOKEN", "")
    get_settings.cache_clear()
    try:
        with pytest.raises(ValidationError):
            get_settings()
    finally:
        get_settings.cache_clear()


def test_no_prev_rotation_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    spy = MagicMock(wraps=secrets_module.compare_digest)
    monkeypatch.setattr("app.core.deps.secrets.compare_digest", spy)
    require_vigil_identity_token(authorization=f"Bearer {_identity_token()}")
    assert spy.call_count == 1


def test_wrong_token_http_is_401(vigil_client: TestClient) -> None:
    resp = vigil_client.get(
        "/internal/vigil/principals/any-sub",
        headers=_auth_header("wrong-token"),
    )
    assert resp.status_code == 401


def test_missing_user_is_ineligible_not_404(vigil_client: TestClient) -> None:
    resp = vigil_client.get(
        "/internal/vigil/principals/missing-sub",
        headers=_auth_header(),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "auth_subject": "missing-sub",
        "eligible": False,
        "account_email": None,
        "email_verified_at": None,
        "observed_at": body["observed_at"],
    }
    assert body["observed_at"].endswith("Z")
    assert resp.headers.get("cache-control") == "no-store"


def test_active_verified_user_is_eligible(vigil_client: TestClient, db_session: Session) -> None:
    user_id = uuid.uuid4()
    sub = "owner-sub-b"
    verified = datetime(2026, 3, 4, 5, 6, 7, tzinfo=UTC)
    _add_user(
        db_session,
        user_id=user_id,
        auth_subject=sub,
        email="owner@example.com",
        email_verified_at=verified,
        delivery_email="delivery@example.com",
        delivery_email_verified_at=verified,
    )
    resp = vigil_client.get(f"/internal/vigil/principals/{sub}", headers=_auth_header())
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {
        "auth_subject",
        "eligible",
        "account_email",
        "email_verified_at",
        "observed_at",
    }
    assert body["auth_subject"] == sub
    assert body["eligible"] is True
    assert body["account_email"] == "owner@example.com"
    assert body["email_verified_at"] == "2026-03-04T05:06:07Z"
    assert "delivery_email" not in body
    assert "holdings" not in body
    blob = resp.text.lower()
    assert "delivery@example.com" not in blob
    assert str(user_id) not in blob


def test_lookup_is_by_auth_subject_never_user_id(
    vigil_client: TestClient, db_session: Session
) -> None:
    user_id = uuid.uuid4()
    _add_user(
        db_session,
        user_id=user_id,
        auth_subject="owner-sub-b",
        email="owner@example.com",
        email_verified_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    as_id = vigil_client.get(
        f"/internal/vigil/principals/{user_id}",
        headers=_auth_header(),
    )
    assert as_id.status_code == 200
    assert as_id.json()["eligible"] is False
    assert as_id.json()["account_email"] is None

    as_sub = vigil_client.get(
        "/internal/vigil/principals/owner-sub-b",
        headers=_auth_header(),
    )
    assert as_sub.status_code == 200
    assert as_sub.json()["eligible"] is True


@pytest.mark.parametrize("status", ["deleted", "suspended"])
def test_inactive_user_hides_email(
    vigil_client: TestClient, db_session: Session, status: str
) -> None:
    _add_user(
        db_session,
        user_id=uuid.uuid4(),
        auth_subject=f"sub-{status}",
        email=f"{status}@example.com",
        status=status,
        email_verified_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    resp = vigil_client.get(
        f"/internal/vigil/principals/sub-{status}",
        headers=_auth_header(),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["eligible"] is False
    assert body["account_email"] is None
    assert body["email_verified_at"] is None
    assert f"{status}@example.com" not in resp.text


def test_active_unverified_returns_email_without_verification(
    vigil_client: TestClient, db_session: Session
) -> None:
    _add_user(
        db_session,
        user_id=uuid.uuid4(),
        auth_subject="unverified-sub",
        email="unverified@example.com",
        email_verified_at=None,
    )
    resp = vigil_client.get(
        "/internal/vigil/principals/unverified-sub",
        headers=_auth_header(),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["eligible"] is False
    assert body["account_email"] == "unverified@example.com"
    assert body["email_verified_at"] is None


def test_read_does_not_mutate_user(vigil_client: TestClient, db_session: Session) -> None:
    user = _add_user(
        db_session,
        user_id=uuid.uuid4(),
        auth_subject="stable-sub",
        email="stable@example.com",
        email_verified_at=datetime(2026, 2, 2, tzinfo=UTC),
    )
    before_login = user.last_login_at
    before_updated = user.updated_at
    resp = vigil_client.get(
        "/internal/vigil/principals/stable-sub",
        headers=_auth_header(),
    )
    assert resp.status_code == 200
    db_session.refresh(user)
    assert user.last_login_at == before_login
    assert user.updated_at == before_updated


def test_db_failure_is_503_not_ineligible(
    vigil_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(*_args: object, **_kwargs: object) -> None:
        raise SQLAlchemyError("db down")

    monkeypatch.setattr(db_session, "execute", _boom)
    resp = vigil_client.get(
        "/internal/vigil/principals/any-sub",
        headers=_auth_header(),
    )
    assert resp.status_code == 503
    assert resp.json().get("eligible") is None


def test_internal_vigil_routes_do_not_use_current_principal() -> None:
    from fastapi.routing import APIRoute

    from app.core.deps import require_vigil_identity_token as token_dep

    routes = [
        r for r in app.routes if isinstance(r, APIRoute) and r.path.startswith("/internal/vigil")
    ]
    assert routes, "expected /internal/vigil routes"
    for route in routes:
        dep_calls = {dep.call for dep in route.dependant.dependencies}
        assert token_dep in dep_calls
        assert current_principal not in dep_calls
