"""Vigil R0 P2.3 — account drill and atomic arm (issue #458).

Real Postgres. Acceptance P2.3-A01..A04.
"""

from __future__ import annotations

import base64
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from altcha import v1 as altcha_v1
from altcha.v1 import AlgoType
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.user import User
from app.models.vigil import (
    VigilActionToken,
    VigilAuditEvent,
    VigilConsumedNonce,
    VigilObject,
    VigilVault,
)
from app.services.vigil import dns_check
from app.services.vigil.configuration import (
    validate_configuration_input,
    write_pending_configuration,
)
from app.services.vigil.drills import peek_outbox_token_for_tests
from app.services.vigil.objects import init_object, upload_object
from app.services.vigil.tokens import mint_signed_nonce
from app.tests.conftest import TEST_USER_ID

_REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(autouse=True)
def _vigil_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIGIL_MODE", "active")
    monkeypatch.setenv("VIGIL_OWNER_AUTH_SUBJECT", "owner-sub")
    monkeypatch.setenv("VIGIL_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("VIGIL_NOTIFICATION_KEY", Fernet.generate_key().decode())
    get_settings.cache_clear()
    monkeypatch.setattr(dns_check, "_domain_has_mail_route", lambda domain: True)
    monkeypatch.setattr(
        "app.tasks.vigil_tasks.dispatch_vigil_outbox_task.delay", lambda *a, **k: None
    )


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


def _age_drills(db_session: Session) -> None:
    db_session.execute(
        update(VigilActionToken).values(created_at=datetime.now(UTC) - timedelta(seconds=61))
    )
    db_session.commit()


def _origin() -> dict[str, str]:
    return {"Origin": get_settings().FRONTEND_URL.rstrip("/")}


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _manifest(vault_id: uuid.UUID, object_id: uuid.UUID) -> dict[str, Any]:
    return {
        "version": 1,
        "algorithm": "AES-256-GCM",
        "vault_id": str(vault_id),
        "object_id": str(object_id),
        "has_password": False,
        "file_nonce": _b64url(b"0" * 12),
        "salt": None,
        "kdf": None,
        "inner_nonce": None,
    }


def _seed_ready(
    db_session: Session, *, expected_revision: int = 0, filename: str = "will.pdf"
) -> tuple[uuid.UUID, uuid.UUID, int]:
    cfg = write_pending_configuration(
        db_session,
        owner_user_id=TEST_USER_ID,
        owner_email="owner@example.com",
        expected_revision=expected_revision,
        normalized=validate_configuration_input(
            interval_days=30,
            grace_hours=72,
            recipients=[{"email": "a@example.com", "email_confirm": "a@example.com"}],
            message="",
        ),
    )
    db_session.flush()
    init = init_object(
        db_session,
        owner_user_id=TEST_USER_ID,
        expected_revision=cfg.revision,
        config_id=cfg.config_id,
        request_id=uuid.uuid4(),
        filename=filename,
        plaintext_size=4,
    )
    db_session.flush()
    upload_object(
        db_session,
        owner_user_id=TEST_USER_ID,
        expected_revision=init.revision,
        object_id=init.object_id,
        config_id=cfg.config_id,
        manifest=_manifest(init.vault_id, init.object_id),
        inner_b64=_b64url(b"D" * 32),
        ciphertext=b"C" * 20,
    )
    db_session.flush()
    vault = db_session.execute(
        select(VigilVault).where(VigilVault.owner_user_id == TEST_USER_ID)
    ).scalar_one()
    return cfg.config_id, init.object_id, vault.revision


def _solved_vigil_altcha(app_client: TestClient) -> str:
    challenge_resp = app_client.get("/vigil/public/altcha-challenge", headers=_origin())
    assert challenge_resp.status_code == 200, challenge_resp.text
    challenge = challenge_resp.json()
    algorithm = cast(AlgoType, challenge["algorithm"])
    solution = altcha_v1.solve_challenge(
        challenge=challenge["challenge"],
        salt=challenge["salt"],
        algorithm=algorithm,
        max_number=challenge["maxNumber"],
    )
    assert solution is not None
    payload = altcha_v1.Payload(
        algorithm=algorithm,
        challenge=challenge["challenge"],
        number=solution.number,
        salt=challenge["salt"],
        signature=challenge["signature"],
    )
    return payload.to_base64()


def _post_drill(
    app_client: TestClient, config_id: uuid.UUID, object_id: uuid.UUID, revision: int
) -> Response:
    return app_client.post(
        "/vigil/drills",
        json={
            "expected_revision": revision,
            "config_id": str(config_id),
            "object_id": str(object_id),
        },
    )


def _confirm(app_client: TestClient, token: str, nonce: str, altcha: str) -> Any:
    return app_client.post(
        "/vigil/public/confirm",
        headers=_origin(),
        json={"token": token, "nonce": nonce, "altcha": altcha},
    )


def _mint_http_nonce(app_client: TestClient, token: str) -> str:
    resp = app_client.post(
        "/vigil/public/status",
        headers=_origin(),
        json={"token": token, "action": "confirm"},
    )
    assert resp.status_code == 200, resp.text
    nonce = resp.json()["nonce"]
    assert isinstance(nonce, str)
    return nonce


# --- Issue #515 -------------------------------------------------------------


def test_public_endpoints_do_not_require_origin_header(
    app_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real browser/proxy traffic does not reliably send Origin on a GET, and
    none of these four endpoints needs it: the two GETs return no secret and
    cause no state change, and the two POSTs are already gated by the
    single-use token hash, minted nonce, and solved Altcha challenge."""
    monkeypatch.setattr("app.services.vigil.arm.live_activation_allowed", lambda: True)

    config_id, object_id, revision = _seed_ready(db_session)
    db_session.commit()
    drill = _post_drill(app_client, config_id, object_id, revision)
    token = peek_outbox_token_for_tests(db_session, uuid.UUID(drill.json()["drill_id"]))

    challenge_resp = app_client.get("/vigil/public/altcha-challenge")
    assert challenge_resp.status_code == 200, challenge_resp.text
    challenge = challenge_resp.json()
    algorithm = cast(AlgoType, challenge["algorithm"])
    solution = altcha_v1.solve_challenge(
        challenge=challenge["challenge"],
        salt=challenge["salt"],
        algorithm=algorithm,
        max_number=challenge["maxNumber"],
    )
    assert solution is not None
    altcha = altcha_v1.Payload(
        algorithm=algorithm,
        challenge=challenge["challenge"],
        number=solution.number,
        salt=challenge["salt"],
        signature=challenge["signature"],
    ).to_base64()

    status_resp = app_client.get("/vigil/public/status", params={"token": token})
    assert status_resp.status_code == 200, status_resp.text

    nonce_resp = app_client.post("/vigil/public/status", json={"token": token, "action": "confirm"})
    assert nonce_resp.status_code == 200, nonce_resp.text
    nonce = nonce_resp.json()["nonce"]

    confirm_resp = app_client.post(
        "/vigil/public/confirm", json={"token": token, "nonce": nonce, "altcha": altcha}
    )
    assert confirm_resp.status_code == 200, confirm_resp.text


# --- P2.3-A01 / A05 --------------------------------------------------------


def test_p23_a01_b_drill_then_c_init_cannot_activate_without_explicit_arm(
    app_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.services.vigil.arm.live_activation_allowed", lambda: True)

    config_a, object_a, rev_a = _seed_ready(db_session, filename="a.pdf")
    db_session.commit()
    drill_a = _post_drill(app_client, config_a, object_a, rev_a)
    token_a = peek_outbox_token_for_tests(db_session, uuid.UUID(drill_a.json()["drill_id"]))
    assert (
        _confirm(
            app_client,
            token_a,
            _mint_http_nonce(app_client, token_a),
            _solved_vigil_altcha(app_client),
        ).status_code
        == 200
    )
    armed_a = app_client.post(
        "/vigil/arm",
        json={
            "expected_revision": drill_a.json()["revision"],
            "config_id": str(config_a),
            "object_id": str(object_a),
        },
    )
    assert armed_a.status_code == 200, armed_a.text
    _age_drills(db_session)

    config_b, object_b, rev_b = _seed_ready(
        db_session, expected_revision=armed_a.json()["revision"], filename="b.pdf"
    )
    db_session.commit()
    drill_b = _post_drill(app_client, config_b, object_b, rev_b)
    assert drill_b.status_code == 202, drill_b.text
    token_b = peek_outbox_token_for_tests(db_session, uuid.UUID(drill_b.json()["drill_id"]))
    nonce_b = _mint_http_nonce(app_client, token_b)
    _age_drills(db_session)

    config_c, object_c, rev_c = _seed_ready(
        db_session, expected_revision=drill_b.json()["revision"], filename="c.pdf"
    )
    db_session.commit()

    confirm_b = _confirm(app_client, token_b, nonce_b, _solved_vigil_altcha(app_client))
    assert confirm_b.status_code == 410

    arm_b = app_client.post(
        "/vigil/arm",
        json={
            "expected_revision": rev_c,
            "config_id": str(config_b),
            "object_id": str(object_b),
        },
    )
    assert arm_b.status_code == 422

    arm_c_without_drill = app_client.post(
        "/vigil/arm",
        json={
            "expected_revision": rev_c,
            "config_id": str(config_c),
            "object_id": str(object_c),
        },
    )
    assert arm_c_without_drill.status_code == 422

    vault = db_session.execute(
        select(VigilVault).where(VigilVault.owner_user_id == TEST_USER_ID)
    ).scalar_one()
    db_session.refresh(vault)
    assert vault.phase == "ARMED"
    assert vault.active_object_id == object_a

    drill_c = _post_drill(app_client, config_c, object_c, rev_c)
    assert drill_c.status_code == 202, drill_c.text
    token_c = peek_outbox_token_for_tests(db_session, uuid.UUID(drill_c.json()["drill_id"]))
    nonce_c = _mint_http_nonce(app_client, token_c)
    confirm_c = _confirm(app_client, token_c, nonce_c, _solved_vigil_altcha(app_client))
    assert confirm_c.status_code == 200
    assert confirm_c.json()["result"] == "confirmed"
    db_session.refresh(vault)
    assert vault.phase == "ARMED"
    assert vault.active_object_id == object_a
    assert vault.last_owner_confirmed_at is None

    arm_c = app_client.post(
        "/vigil/arm",
        json={
            "expected_revision": drill_c.json()["revision"],
            "config_id": str(config_c),
            "object_id": str(object_c),
        },
    )
    assert arm_c.status_code == 200, arm_c.text
    assert arm_c.json()["phase"] == "ARMED"
    db_session.refresh(vault)
    assert vault.phase == "ARMED"
    assert vault.active_object_id == object_c
    obj_a = db_session.get(VigilObject, object_a)
    assert obj_a is not None
    assert obj_a.status == "retired"
    assert obj_a.ciphertext is None
    assert obj_a.outer_cipher is None
    obj_b = db_session.get(VigilObject, object_b)
    assert obj_b is not None
    assert obj_b.status == "retired"
    assert obj_b.ciphertext is None
    assert obj_b.outer_cipher is None


def test_p23_a01_failed_new_drill_leaves_prior_armed_state(
    app_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.services.vigil.arm.live_activation_allowed", lambda: True)
    config_a, object_a, rev_a = _seed_ready(db_session, filename="a.pdf")
    db_session.commit()
    drill_a = _post_drill(app_client, config_a, object_a, rev_a)
    token_a = peek_outbox_token_for_tests(db_session, uuid.UUID(drill_a.json()["drill_id"]))
    nonce_a = _mint_http_nonce(app_client, token_a)
    assert (
        _confirm(app_client, token_a, nonce_a, _solved_vigil_altcha(app_client)).status_code == 200
    )
    armed = app_client.post(
        "/vigil/arm",
        json={
            "expected_revision": drill_a.json()["revision"],
            "config_id": str(config_a),
            "object_id": str(object_a),
        },
    )
    assert armed.status_code == 200, armed.text

    failed = app_client.post(
        "/vigil/drills",
        json={
            "expected_revision": armed.json()["revision"],
            "config_id": str(uuid.uuid4()),
            "object_id": str(uuid.uuid4()),
        },
    )
    assert failed.status_code == 422
    vault = db_session.execute(
        select(VigilVault).where(VigilVault.owner_user_id == TEST_USER_ID)
    ).scalar_one()
    db_session.refresh(vault)
    assert vault.phase == "ARMED"
    assert vault.active_object_id == object_a
    obj_a = db_session.get(VigilObject, object_a)
    assert obj_a is not None
    assert obj_a.ciphertext is not None


# --- P2.3-A02 / A06 --------------------------------------------------------


def test_p23_a02_get_status_is_read_only_and_nonce_replay_fails(
    app_client: TestClient, db_session: Session
) -> None:
    config_id, object_id, revision = _seed_ready(db_session)
    db_session.commit()
    drill = _post_drill(app_client, config_id, object_id, revision)
    assert drill.status_code == 202, drill.text
    token = peek_outbox_token_for_tests(db_session, uuid.UUID(drill.json()["drill_id"]))

    nonce_count_before = (
        db_session.scalar(select(func.count()).select_from(VigilConsumedNonce)) or 0
    )
    token_row = db_session.execute(
        select(VigilActionToken).where(VigilActionToken.id == uuid.UUID(drill.json()["drill_id"]))
    ).scalar_one()
    for _ in range(100):
        resp = app_client.get(
            "/vigil/public/status",
            params={"token": token},
            headers=_origin(),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["available"] is True
        assert body.get("nonce") is None
        assert "account_email" not in body
        assert "recipients" not in body
    db_session.refresh(token_row)
    assert token_row.confirmed_at is None
    assert token_row.used_at is None
    assert (
        db_session.scalar(select(func.count()).select_from(VigilConsumedNonce)) or 0
    ) == nonce_count_before

    expired = mint_signed_nonce(
        token_hash=token_row.token_hash,
        action="confirm",
        object_id=object_id,
        now=datetime.now(UTC) - timedelta(minutes=5),
    )
    expired_resp = _confirm(app_client, token, expired.compact, _solved_vigil_altcha(app_client))
    assert expired_resp.status_code == 422

    wrong = mint_signed_nonce(
        token_hash=token_row.token_hash,
        action="revoke",
        object_id=object_id,
        now=datetime.now(UTC),
    )
    wrong_resp = _confirm(app_client, token, wrong.compact, _solved_vigil_altcha(app_client))
    assert wrong_resp.status_code == 422

    nonce = _mint_http_nonce(app_client, token)
    first = _confirm(app_client, token, nonce, _solved_vigil_altcha(app_client))
    assert first.status_code == 200
    assert first.json()["result"] == "confirmed"
    replay = _confirm(app_client, token, nonce, _solved_vigil_altcha(app_client))
    assert replay.status_code == 422


# --- P2.3-A03 / D5 ---------------------------------------------------------


def test_p23_a03_first_arm_sets_retention_later_drill_does_not_refresh(
    app_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.services.vigil.arm.live_activation_allowed", lambda: True)
    config_a, object_a, rev_a = _seed_ready(db_session, filename="a.pdf")
    db_session.commit()
    drill_a = _post_drill(app_client, config_a, object_a, rev_a)
    token_a = peek_outbox_token_for_tests(db_session, uuid.UUID(drill_a.json()["drill_id"]))
    assert (
        _confirm(
            app_client,
            token_a,
            _mint_http_nonce(app_client, token_a),
            _solved_vigil_altcha(app_client),
        ).status_code
        == 200
    )
    armed = app_client.post(
        "/vigil/arm",
        json={
            "expected_revision": drill_a.json()["revision"],
            "config_id": str(config_a),
            "object_id": str(object_a),
        },
    )
    assert armed.status_code == 200, armed.text
    vault = db_session.execute(
        select(VigilVault).where(VigilVault.owner_user_id == TEST_USER_ID)
    ).scalar_one()
    db_session.refresh(vault)
    first_armed = vault.first_armed_at
    retention = vault.retention_anchor_at
    assert first_armed is not None
    assert retention == first_armed
    assert vault.last_owner_confirmed_at is None
    _age_drills(db_session)

    config_b, object_b, rev_b = _seed_ready(
        db_session, expected_revision=armed.json()["revision"], filename="b.pdf"
    )
    db_session.commit()
    drill_b = _post_drill(app_client, config_b, object_b, rev_b)
    token_b = peek_outbox_token_for_tests(db_session, uuid.UUID(drill_b.json()["drill_id"]))
    assert (
        _confirm(
            app_client,
            token_b,
            _mint_http_nonce(app_client, token_b),
            _solved_vigil_altcha(app_client),
        ).status_code
        == 200
    )
    armed_b = app_client.post(
        "/vigil/arm",
        json={
            "expected_revision": drill_b.json()["revision"],
            "config_id": str(config_b),
            "object_id": str(object_b),
        },
    )
    assert armed_b.status_code == 200, armed_b.text
    db_session.refresh(vault)
    assert vault.first_armed_at == first_armed
    assert vault.retention_anchor_at == retention
    assert vault.last_owner_confirmed_at is None


# --- P2.3-A04 --------------------------------------------------------------


def test_p23_a04_backup_field_422_and_live_arm_blocked_without_stop_hooks(
    app_client: TestClient, db_session: Session
) -> None:
    config_id, object_id, revision = _seed_ready(db_session)
    db_session.commit()
    backup = app_client.post(
        "/vigil/drills",
        json={
            "expected_revision": revision,
            "config_id": str(config_id),
            "object_id": str(object_id),
            "backup_email": "backup@example.com",
        },
    )
    assert backup.status_code == 422

    drill = _post_drill(app_client, config_id, object_id, revision)
    assert drill.status_code == 202, drill.text
    token = peek_outbox_token_for_tests(db_session, uuid.UUID(drill.json()["drill_id"]))
    assert (
        _confirm(
            app_client, token, _mint_http_nonce(app_client, token), _solved_vigil_altcha(app_client)
        ).status_code
        == 200
    )
    live_arm = app_client.post(
        "/vigil/arm",
        json={
            "expected_revision": drill.json()["revision"],
            "config_id": str(config_id),
            "object_id": str(object_id),
        },
    )
    assert live_arm.status_code == 503
    assert "stop/recovery" in live_arm.json()["detail"]
    vault = db_session.execute(
        select(VigilVault).where(VigilVault.owner_user_id == TEST_USER_ID)
    ).scalar_one()
    db_session.refresh(vault)
    assert vault.phase == "DISARMED"
    assert vault.first_armed_at is None


def test_p23_a04_unverified_account_email_cannot_drill(
    app_client: TestClient, db_session: Session
) -> None:
    config_id, object_id, revision = _seed_ready(db_session)
    db_session.commit()
    owner = db_session.get(User, TEST_USER_ID)
    assert owner is not None
    owner.email_verified_at = None
    db_session.commit()
    resp = _post_drill(app_client, config_id, object_id, revision)
    assert resp.status_code == 503
    vault = db_session.execute(
        select(VigilVault).where(VigilVault.owner_user_id == TEST_USER_ID)
    ).scalar_one()
    db_session.refresh(vault)
    assert vault.phase == "DISARMED"
    assert db_session.scalar(select(func.count()).select_from(VigilActionToken)) == 0


def test_public_status_rejects_unknown_action(app_client: TestClient, db_session: Session) -> None:
    config_id, object_id, revision = _seed_ready(db_session)
    db_session.commit()
    drill = _post_drill(app_client, config_id, object_id, revision)
    token = peek_outbox_token_for_tests(db_session, uuid.UUID(drill.json()["drill_id"]))
    resp = app_client.post(
        "/vigil/public/status",
        headers=_origin(),
        json={"token": token, "action": "backup"},
    )
    assert resp.status_code == 422


def test_purge_removes_action_tokens_and_consumed_nonces(
    app_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.user_purge import purge_user

    monkeypatch.setattr("app.services.vigil.arm.live_activation_allowed", lambda: True)
    config_id, object_id, revision = _seed_ready(db_session)
    db_session.commit()
    drill = _post_drill(app_client, config_id, object_id, revision)
    token = peek_outbox_token_for_tests(db_session, uuid.UUID(drill.json()["drill_id"]))
    nonce = _mint_http_nonce(app_client, token)
    assert _confirm(app_client, token, nonce, _solved_vigil_altcha(app_client)).status_code == 200
    armed = app_client.post(
        "/vigil/arm",
        json={
            "expected_revision": drill.json()["revision"],
            "config_id": str(config_id),
            "object_id": str(object_id),
        },
    )
    assert armed.status_code == 200, armed.text
    assert db_session.scalar(select(func.count()).select_from(VigilAuditEvent)) == 1
    result = purge_user(db_session, TEST_USER_ID)
    db_session.rollback()
    assert result.vigil_action_tokens == 1
    assert result.vigil_consumed_nonces == 1
    assert result.vigil_audit_events == 1


def test_no_new_compose_service_or_domain() -> None:
    compose = yaml.safe_load((_REPO_ROOT / "docker-compose.yml").read_text())
    assert set(compose["services"]) == {
        "postgres",
        "redis",
        "migrate",
        "backend",
        "celery-worker",
        "celery-beat",
        "frontend",
        "caddy",
    }
    caddy = (_REPO_ROOT / "Caddyfile").read_text()
    assert "vigil." not in caddy
    assert "portfonia.com" in caddy
