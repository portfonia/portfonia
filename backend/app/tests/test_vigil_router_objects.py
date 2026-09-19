"""POST /vigil/objects/init and POST /vigil/objects/upload HTTP contract
(issue #454, Vigil R0 P2.1)."""

from __future__ import annotations

import base64
import io
import uuid
from datetime import UTC, datetime

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.user import User
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


def _seed_config(app_client: TestClient) -> tuple[str, int]:
    resp = app_client.post(
        "/vigil/configurations",
        json={
            "expected_revision": 0,
            "recipients": [{"email": "a@example.com", "email_confirm": "a@example.com"}],
        },
    )
    body = resp.json()
    return body["config_id"], body["revision"]


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def test_object_init_returns_201(app_client: TestClient) -> None:
    config_id, revision = _seed_config(app_client)
    resp = app_client.post(
        "/vigil/objects/init",
        json={
            "expected_revision": revision,
            "config_id": config_id,
            "request_id": str(uuid.uuid4()),
            "filename": "will.pdf",
            "plaintext_size": 1000,
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert "object_id" in body
    assert body["revision"] == revision + 1


def test_object_init_replay_returns_200_same_id(app_client: TestClient) -> None:
    config_id, revision = _seed_config(app_client)
    request_id = str(uuid.uuid4())
    payload = {
        "expected_revision": revision,
        "config_id": config_id,
        "request_id": request_id,
        "filename": "will.pdf",
        "plaintext_size": 1000,
    }
    first = app_client.post("/vigil/objects/init", json=payload)
    replay = app_client.post("/vigil/objects/init", json=payload)
    assert replay.status_code == 200, replay.text
    assert replay.json()["object_id"] == first.json()["object_id"]


def test_object_init_changed_replay_returns_409(app_client: TestClient) -> None:
    config_id, revision = _seed_config(app_client)
    request_id = str(uuid.uuid4())
    app_client.post(
        "/vigil/objects/init",
        json={
            "expected_revision": revision,
            "config_id": config_id,
            "request_id": request_id,
            "filename": "will.pdf",
            "plaintext_size": 1000,
        },
    )
    resp = app_client.post(
        "/vigil/objects/init",
        json={
            "expected_revision": revision,
            "config_id": config_id,
            "request_id": request_id,
            "filename": "changed.pdf",
            "plaintext_size": 1000,
        },
    )
    assert resp.status_code == 409


def test_object_init_unknown_config_id_returns_404(app_client: TestClient) -> None:
    _config_id, revision = _seed_config(app_client)
    resp = app_client.post(
        "/vigil/objects/init",
        json={
            "expected_revision": revision,
            "config_id": str(uuid.uuid4()),
            "request_id": str(uuid.uuid4()),
            "filename": "will.pdf",
            "plaintext_size": 1000,
        },
    )
    assert resp.status_code == 404


def _init_object(app_client: TestClient, plaintext_size: int = 4) -> tuple[str, str, int]:
    config_id, revision = _seed_config(app_client)
    resp = app_client.post(
        "/vigil/objects/init",
        json={
            "expected_revision": revision,
            "config_id": config_id,
            "request_id": str(uuid.uuid4()),
            "filename": "will.pdf",
            "plaintext_size": plaintext_size,
        },
    )
    body = resp.json()
    return config_id, body["object_id"], body["revision"]


def _manifest_json(vault_id: str, object_id: str) -> str:
    import json as _json

    return _json.dumps(
        {
            "version": 1,
            "algorithm": "AES-256-GCM",
            "vault_id": vault_id,
            "object_id": object_id,
            "has_password": False,
            "file_nonce": _b64url(b"0" * 12),
            "salt": None,
            "kdf": None,
            "inner_nonce": None,
        }
    )


def test_object_upload_returns_201_and_ready(app_client: TestClient) -> None:
    config_id, object_id, revision = _init_object(app_client, plaintext_size=4)
    vault_resp = app_client.get("/vigil/vault").json()
    vault_id = vault_resp["vault_id"]

    resp = app_client.post(
        "/vigil/objects/upload",
        data={
            "expected_revision": str(revision),
            "config_id": config_id,
            "object_id": object_id,
            "manifest": _manifest_json(vault_id, object_id),
            "inner": _b64url(b"D" * 32),
        },
        files={"file": ("blob", io.BytesIO(b"C" * 20), "application/octet-stream")},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "ready"
    assert body["object_id"] == object_id


def test_object_upload_size_mismatch_returns_422(app_client: TestClient) -> None:
    config_id, object_id, revision = _init_object(app_client, plaintext_size=4)
    vault_id = app_client.get("/vigil/vault").json()["vault_id"]

    resp = app_client.post(
        "/vigil/objects/upload",
        data={
            "expected_revision": str(revision),
            "config_id": config_id,
            "object_id": object_id,
            "manifest": _manifest_json(vault_id, object_id),
            "inner": _b64url(b"D" * 32),
        },
        files={"file": ("blob", io.BytesIO(b"C" * 19), "application/octet-stream")},
    )
    assert resp.status_code == 422


def test_object_upload_exact_replay_returns_200(app_client: TestClient) -> None:
    config_id, object_id, revision = _init_object(app_client, plaintext_size=4)
    vault_id = app_client.get("/vigil/vault").json()["vault_id"]
    manifest = _manifest_json(vault_id, object_id)
    inner = _b64url(b"D" * 32)

    first = app_client.post(
        "/vigil/objects/upload",
        data={
            "expected_revision": str(revision),
            "config_id": config_id,
            "object_id": object_id,
            "manifest": manifest,
            "inner": inner,
        },
        files={"file": ("blob", io.BytesIO(b"C" * 20), "application/octet-stream")},
    )
    assert first.status_code == 201

    replay = app_client.post(
        "/vigil/objects/upload",
        data={
            "expected_revision": "999999",  # deliberately stale/wrong
            "config_id": config_id,
            "object_id": object_id,
            "manifest": manifest,
            "inner": inner,
        },
        files={"file": ("blob", io.BytesIO(b"C" * 20), "application/octet-stream")},
    )
    assert replay.status_code == 200, replay.text


def test_object_upload_mismatched_replay_returns_409(app_client: TestClient) -> None:
    config_id, object_id, revision = _init_object(app_client, plaintext_size=4)
    vault_id = app_client.get("/vigil/vault").json()["vault_id"]
    manifest = _manifest_json(vault_id, object_id)
    inner = _b64url(b"D" * 32)

    app_client.post(
        "/vigil/objects/upload",
        data={
            "expected_revision": str(revision),
            "config_id": config_id,
            "object_id": object_id,
            "manifest": manifest,
            "inner": inner,
        },
        files={"file": ("blob", io.BytesIO(b"C" * 20), "application/octet-stream")},
    )

    resp = app_client.post(
        "/vigil/objects/upload",
        data={
            "expected_revision": str(revision),
            "config_id": config_id,
            "object_id": object_id,
            "manifest": manifest,
            "inner": inner,
        },
        files={"file": ("blob", io.BytesIO(b"D" * 20), "application/octet-stream")},
    )
    assert resp.status_code == 409
