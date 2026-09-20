"""services/vigil/access.py — require_vigil_owner + is_vigil_owner_eligible
(issue #452, Vigil R0 P1.2).

Design (#452 comment / #450 Design section 3, incorporated by reference):
`require_vigil_owner` reuses `current_principal` unchanged, then reloads
`User` fresh by `Principal.user_id` and compares `auth_subject` against the
single configured `VIGIL_OWNER_AUTH_SUBJECT` allowlist entry — it never
compares `user_id` to the JWT `sub` and never reads `is_admin`. Exercised
through the one route wired to it so far, `GET /vigil/vault`, matching this
repo's existing pattern (test_auth_deps.py) of testing `current_principal`
through a real endpoint rather than calling the dependency directly.
"""

from __future__ import annotations

import inspect
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import get_session
from app.core.deps import current_principal
from app.main import app
from app.models.user import User
from app.services.auth_provider import AccessTokenClaims
from app.tests.conftest import TEST_USER_ID, U1_USER_ID, U2_USER_ID


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> Iterator[None]:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def raw_client(db_session: Session) -> Iterator[TestClient]:
    """Real `current_principal` (real JWT verification via a monkeypatched
    `verify_access_token`), matching test_auth_deps.py's pattern — NOT
    app_client's always-authenticated override, since this module tests
    the auth boundary itself."""

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
    email: str,
    auth_subject: str,
    status: str = "active",
    email_verified_at: datetime | None = None,
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
    )
    session.add(row)
    session.flush()
    return row


def _claims_for(sub: str, *, session_id: str = "session-vigil") -> Any:
    def _fn(_token: str) -> AccessTokenClaims:
        return AccessTokenClaims(sub=sub, email="whoever@example.com", session_id=session_id)

    return _fn


# --- GET /vigil/vault via require_vigil_owner ---------------------------


def test_no_token_is_401(raw_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIGIL_MODE", "active")
    monkeypatch.setenv("VIGIL_OWNER_AUTH_SUBJECT", "owner-sub")
    resp = raw_client.get("/vigil/vault")
    assert resp.status_code == 401


def test_unknown_sub_is_401(
    raw_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A01: `sub` with no matching `users` row -> 401, same as any other
    current_principal caller — never auto-inserted, never elevated."""
    monkeypatch.setenv("VIGIL_MODE", "active")
    monkeypatch.setenv("VIGIL_OWNER_AUTH_SUBJECT", "owner-sub")
    _add_user(db_session, user_id=TEST_USER_ID, email="owner@example.com", auth_subject="owner-sub")

    monkeypatch.setattr("app.core.deps.verify_access_token", _claims_for(str(uuid.uuid4())))
    resp = raw_client.get("/vigil/vault", headers={"Authorization": "Bearer x"})
    assert resp.status_code == 401


def test_other_active_account_is_403(
    raw_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A01: a real, valid session for a different active account is 403, not
    401 — this is an authorization failure, not an authentication one."""
    monkeypatch.setenv("VIGIL_MODE", "active")
    monkeypatch.setenv("VIGIL_OWNER_AUTH_SUBJECT", "owner-sub")
    _add_user(
        db_session,
        user_id=TEST_USER_ID,
        email="owner@example.com",
        auth_subject="owner-sub",
        email_verified_at=datetime.now(UTC),
    )
    _add_user(
        db_session, user_id=U1_USER_ID, email="stranger@example.com", auth_subject="stranger-sub"
    )

    monkeypatch.setattr("app.core.deps.verify_access_token", _claims_for("stranger-sub"))
    resp = raw_client.get("/vigil/vault", headers={"Authorization": "Bearer x"})
    assert resp.status_code == 403


def test_owner_valid_session_returns_own_status(
    raw_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A01: User.id=A, auth_subject=B — a valid session for sub B returns
    the caller's own vault status directly, no popup/handshake."""
    monkeypatch.setenv("VIGIL_MODE", "active")
    monkeypatch.setenv("VIGIL_OWNER_AUTH_SUBJECT", "owner-sub")
    _add_user(
        db_session,
        user_id=TEST_USER_ID,
        email="owner@example.com",
        auth_subject="owner-sub",
        email_verified_at=datetime.now(UTC),
    )

    monkeypatch.setattr("app.core.deps.verify_access_token", _claims_for("owner-sub"))
    resp = raw_client.get("/vigil/vault", headers={"Authorization": "Bearer x"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["vault_id"] is None
    assert body["phase"] == "DISARMED"
    assert body["revision"] == 0


def test_unverified_owner_can_still_read_status(
    raw_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A02: an unverified-email owner can still read status — only the
    (future) arm/escalation guard rejects them, not this read path."""
    monkeypatch.setenv("VIGIL_MODE", "active")
    monkeypatch.setenv("VIGIL_OWNER_AUTH_SUBJECT", "owner-sub")
    _add_user(
        db_session,
        user_id=TEST_USER_ID,
        email="owner@example.com",
        auth_subject="owner-sub",
        email_verified_at=None,
    )

    monkeypatch.setattr("app.core.deps.verify_access_token", _claims_for("owner-sub"))
    resp = raw_client.get("/vigil/vault", headers={"Authorization": "Bearer x"})
    assert resp.status_code == 200


def test_recovery_mode_allows_read(
    raw_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VIGIL_MODE", "recovery")
    monkeypatch.setenv("VIGIL_OWNER_AUTH_SUBJECT", "owner-sub")
    _add_user(
        db_session,
        user_id=TEST_USER_ID,
        email="owner@example.com",
        auth_subject="owner-sub",
        email_verified_at=datetime.now(UTC),
    )

    monkeypatch.setattr("app.core.deps.verify_access_token", _claims_for("owner-sub"))
    resp = raw_client.get("/vigil/vault", headers={"Authorization": "Bearer x"})
    assert resp.status_code == 200


def test_mode_off_is_503_even_with_owner_configured(
    raw_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VIGIL_MODE", "off")
    monkeypatch.setenv("VIGIL_OWNER_AUTH_SUBJECT", "owner-sub")
    _add_user(
        db_session,
        user_id=TEST_USER_ID,
        email="owner@example.com",
        auth_subject="owner-sub",
        email_verified_at=datetime.now(UTC),
    )

    monkeypatch.setattr("app.core.deps.verify_access_token", _claims_for("owner-sub"))
    resp = raw_client.get("/vigil/vault", headers={"Authorization": "Bearer x"})
    assert resp.status_code == 503


def test_missing_owner_subject_config_is_503(
    raw_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VIGIL_MODE", "active")
    monkeypatch.delenv("VIGIL_OWNER_AUTH_SUBJECT", raising=False)
    _add_user(
        db_session,
        user_id=TEST_USER_ID,
        email="owner@example.com",
        auth_subject="owner-sub",
        email_verified_at=datetime.now(UTC),
    )

    monkeypatch.setattr("app.core.deps.verify_access_token", _claims_for("owner-sub"))
    resp = raw_client.get("/vigil/vault", headers={"Authorization": "Bearer x"})
    assert resp.status_code == 503


def test_unavailable_takes_precedence_over_non_owner(
    raw_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Config-missing is "feature not configured, not a security failure" —
    it must not leak a 403 (which would confirm "you're just not the
    owner") to a caller when the feature isn't even on."""
    monkeypatch.setenv("VIGIL_MODE", "off")
    monkeypatch.delenv("VIGIL_OWNER_AUTH_SUBJECT", raising=False)
    _add_user(db_session, user_id=TEST_USER_ID, email="u1@example.com", auth_subject="sub-u1")

    monkeypatch.setattr("app.core.deps.verify_access_token", _claims_for("sub-u1"))
    resp = raw_client.get("/vigil/vault", headers={"Authorization": "Bearer x"})
    assert resp.status_code == 503


# --- is_vigil_owner_eligible (background/scheduled callers, no request) --


def test_eligible_true_for_active_verified_allowlisted_owner(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.vigil.access import is_vigil_owner_eligible

    monkeypatch.setenv("VIGIL_OWNER_AUTH_SUBJECT", "owner-sub")
    user = _add_user(
        db_session,
        user_id=TEST_USER_ID,
        email="owner@example.com",
        auth_subject="owner-sub",
        email_verified_at=datetime.now(UTC),
    )
    db_session.flush()

    assert is_vigil_owner_eligible(db_session, user.id) is True


def test_account_email_verification_is_not_vigil_confirmation_eligibility(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.vigil.access import is_vigil_owner_eligible

    monkeypatch.setenv("VIGIL_OWNER_AUTH_SUBJECT", "owner-sub")
    user = _add_user(
        db_session,
        user_id=TEST_USER_ID,
        email="owner@example.com",
        auth_subject="owner-sub",
        email_verified_at=None,
    )

    assert is_vigil_owner_eligible(db_session, user.id) is True


def test_eligible_false_when_inactive(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.vigil.access import is_vigil_owner_eligible

    monkeypatch.setenv("VIGIL_OWNER_AUTH_SUBJECT", "owner-sub")
    user = _add_user(
        db_session,
        user_id=TEST_USER_ID,
        email="owner@example.com",
        auth_subject="owner-sub",
        status="suspended",
        email_verified_at=datetime.now(UTC),
    )

    assert is_vigil_owner_eligible(db_session, user.id) is False


def test_eligible_false_when_not_allowlisted(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.vigil.access import is_vigil_owner_eligible

    monkeypatch.setenv("VIGIL_OWNER_AUTH_SUBJECT", "owner-sub")
    user = _add_user(
        db_session,
        user_id=U2_USER_ID,
        email="stranger@example.com",
        auth_subject="stranger-sub",
        email_verified_at=datetime.now(UTC),
    )

    assert is_vigil_owner_eligible(db_session, user.id) is False


def test_eligible_false_when_user_missing(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.vigil.access import is_vigil_owner_eligible

    monkeypatch.setenv("VIGIL_OWNER_AUTH_SUBJECT", "owner-sub")
    assert is_vigil_owner_eligible(db_session, uuid.uuid4()) is False


def test_eligible_false_when_owner_subject_not_configured(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.vigil.access import is_vigil_owner_eligible

    monkeypatch.delenv("VIGIL_OWNER_AUTH_SUBJECT", raising=False)
    user = _add_user(
        db_session,
        user_id=TEST_USER_ID,
        email="owner@example.com",
        auth_subject="owner-sub",
        email_verified_at=datetime.now(UTC),
    )

    assert is_vigil_owner_eligible(db_session, user.id) is False


def test_eligible_runs_with_no_request_scoped_state(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A03: the eligibility check is callable from scheduled/background code
    with only a Session and a user_id — no Principal, no Request, no JWT."""
    from app.services.vigil.access import is_vigil_owner_eligible

    monkeypatch.setenv("VIGIL_OWNER_AUTH_SUBJECT", "owner-sub")
    user = _add_user(
        db_session,
        user_id=TEST_USER_ID,
        email="owner@example.com",
        auth_subject="owner-sub",
        email_verified_at=datetime.now(UTC),
    )

    result = is_vigil_owner_eligible(session=db_session, user_id=user.id)
    assert result is True


def test_access_module_never_reads_delivery_email() -> None:
    """Vigil confirmation never uses the report pipeline delivery address."""
    from app.services.vigil import access

    source = inspect.getsource(access)
    assert "delivery_email" not in source


def test_access_module_never_uses_is_admin() -> None:
    """R0 is a single-owner allowlist, not a role check — never is_admin."""
    from app.services.vigil import access

    source = inspect.getsource(access)
    assert "is_admin" not in source


def test_access_module_never_compares_user_id_to_sub() -> None:
    """User.id and the JWT sub (auth_subject) are different namespaces —
    the allowlist must compare auth_subject, never user_id against claims.sub."""
    from app.services.vigil import access

    source = inspect.getsource(access)
    assert "claims.sub" not in source
    assert ".sub ==" not in source
