"""GET /vault DISARMED view and owner-gated create (issue #452)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from vigil_app.core import auth as auth_mod
from vigil_app.core.auth import AccessTokenClaims
from vigil_app.core.config import get_settings
from vigil_app.core.database import get_session
from vigil_app.main import app
from vigil_app.models.runtime_heartbeat import RuntimeHeartbeat
from vigil_app.models.vault import Vault
from vigil_app.services import session_status as session_status_mod
from vigil_app.services.session_status import SessionExpired, SessionStatusUnavailable


@pytest.fixture
def client(db_session: Session) -> Iterator[TestClient]:
    def _override_session() -> Iterator[Session]:
        yield db_session

    app.dependency_overrides[get_session] = _override_session
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _owner_claims(token: str = "owner-jwt") -> AccessTokenClaims:
    return AccessTokenClaims(
        sub=get_settings().OWNER_AUTH_SUBJECT,
        session_id="sess-owner",
        token=token,
    )


def _patch_owner(monkeypatch: pytest.MonkeyPatch, claims: AccessTokenClaims) -> None:
    monkeypatch.setattr(auth_mod, "verify_access_token", lambda _token: claims)


def _patch_session_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(session_status_mod, "check_session_status", lambda _token, **_k: None)


def test_get_vault_without_token_is_401(client: TestClient) -> None:
    assert client.get("/vault").status_code == 401


def test_get_vault_other_subject_is_403(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_owner(
        monkeypatch,
        AccessTokenClaims(sub="other-sub", session_id="s", token="t"),
    )
    _patch_session_ok(monkeypatch)
    resp = client.get("/vault", headers={"Authorization": "Bearer t"})
    assert resp.status_code == 403


def test_get_vault_user_id_as_sub_cannot_read(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_owner(
        monkeypatch,
        AccessTokenClaims(sub="00000000-0000-0000-0000-000000000001", session_id="s", token="t"),
    )
    _patch_session_ok(monkeypatch)
    resp = client.get("/vault", headers={"Authorization": "Bearer t"})
    assert resp.status_code == 403


def test_expired_app_session_denied_with_valid_jwt(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_owner(monkeypatch, _owner_claims())

    def _expired(_token: str, **_k: object) -> None:
        raise SessionExpired()

    monkeypatch.setattr(session_status_mod, "check_session_status", _expired)
    resp = client.get("/vault", headers={"Authorization": "Bearer owner-jwt"})
    assert resp.status_code == 401


def test_session_status_transport_is_503(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_owner(monkeypatch, _owner_claims())

    def _down(_token: str, **_k: object) -> None:
        raise SessionStatusUnavailable()

    monkeypatch.setattr(session_status_mod, "check_session_status", _down)
    resp = client.get("/vault", headers={"Authorization": "Bearer owner-jwt"})
    assert resp.status_code == 503


def test_get_vault_without_row_is_disarmed_and_does_not_insert(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_owner(monkeypatch, _owner_claims())
    _patch_session_ok(monkeypatch)
    before = db_session.execute(select(func.count()).select_from(Vault)).scalar_one()
    resp = client.get("/vault", headers={"Authorization": "Bearer owner-jwt"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["phase"] == "DISARMED"
    assert body["revision"] == 0
    assert body.get("active_object_id") is None
    db_session.expire_all()
    after = db_session.execute(select(func.count()).select_from(Vault)).scalar_one()
    assert after == before


def test_post_vault_creates_once(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_owner(monkeypatch, _owner_claims())
    _patch_session_ok(monkeypatch)
    created = client.post("/vault", headers={"Authorization": "Bearer owner-jwt"})
    assert created.status_code == 201
    body = created.json()
    assert body["phase"] == "DISARMED"
    row = db_session.execute(
        select(Vault).where(Vault.owner_auth_subject == get_settings().OWNER_AUTH_SUBJECT)
    ).scalar_one()
    assert row.phase == "DISARMED"
    again = client.post("/vault", headers={"Authorization": "Bearer owner-jwt"})
    assert again.status_code == 409


def test_non_owner_cannot_create_vault(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_owner(
        monkeypatch,
        AccessTokenClaims(sub="intruder", session_id="s", token="t"),
    )
    _patch_session_ok(monkeypatch)
    resp = client.post("/vault", headers={"Authorization": "Bearer t"})
    assert resp.status_code == 403
    assert db_session.execute(select(func.count()).select_from(Vault)).scalar_one() == 0


def _assert_p13_shape(body: dict[str, object]) -> None:
    assert "active_config_id" in body
    assert "deadline_at" in body
    assert body["deadline_at"] is None
    heartbeat = body["heartbeat"]
    assert isinstance(heartbeat, dict)
    assert set(heartbeat) == {
        "last_scan_completed_at",
        "last_dependency_check_at",
        "health",
        "reason",
    }


def test_get_vault_disarmed_view_includes_heartbeat_and_null_placeholders(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_owner(monkeypatch, _owner_claims())
    _patch_session_ok(monkeypatch)
    resp = client.get("/vault", headers={"Authorization": "Bearer owner-jwt"})
    assert resp.status_code == 200
    body = resp.json()
    _assert_p13_shape(body)
    assert body["active_config_id"] is None
    assert body["active_object_id"] is None
    assert body["heartbeat"]["health"] == "held"
    assert body["heartbeat"]["last_scan_completed_at"] is None
    assert body["heartbeat"]["last_dependency_check_at"] is None
    assert "pending_object" not in body


def test_get_vault_serializes_active_config_id_and_live_heartbeat(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_owner(monkeypatch, _owner_claims())
    _patch_session_ok(monkeypatch)
    config_id = uuid4()
    object_id = uuid4()
    scanned = datetime(2026, 9, 14, 13, 0, tzinfo=UTC)
    db_session.add(
        Vault(
            owner_auth_subject=get_settings().OWNER_AUTH_SUBJECT,
            phase="DISARMED",
            revision=3,
            active_config_id=config_id,
            active_object_id=object_id,
        )
    )
    beat = db_session.get(RuntimeHeartbeat, 1)
    assert beat is not None
    beat.health = "ok"
    beat.reason = None
    beat.last_scan_completed_at = scanned
    beat.last_dependency_check_at = scanned
    db_session.commit()

    resp = client.get("/vault", headers={"Authorization": "Bearer owner-jwt"})
    assert resp.status_code == 200
    body = resp.json()
    _assert_p13_shape(body)
    assert body["revision"] == 3
    assert body["active_config_id"] == str(config_id)
    assert body["active_object_id"] == str(object_id)
    assert body["heartbeat"]["health"] == "ok"
    assert body["heartbeat"]["last_scan_completed_at"] == "2026-09-14T13:00:00Z"


def test_post_vault_response_includes_p13_fields(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_owner(monkeypatch, _owner_claims())
    _patch_session_ok(monkeypatch)
    created = client.post("/vault", headers={"Authorization": "Bearer owner-jwt"})
    assert created.status_code == 201
    _assert_p13_shape(created.json())
