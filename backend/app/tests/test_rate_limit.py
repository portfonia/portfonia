"""Signup / invite-mint rate limits (issue #190)."""

from __future__ import annotations

import uuid
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.core import rate_limit
from app.core.rate_limit import (
    RATE_LIMIT_DETAIL,
    SIGNUP_IP_MINUTE_LIMIT,
    SIGNUP_IP_MINUTE_TTL,
    SIGNUP_TOKEN_FAIL_LIMIT,
    UNAVAILABLE_DETAIL,
    InMemoryBackend,
    canonical_client_id,
    client_id_from_request,
    guard_known_invite_token,
    rate_limit_signup,
)
from app.services.invites import create_invite

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CREATOR = uuid.UUID("00000000-0000-0000-0000-0000000000aa")


@pytest.fixture
def backend() -> InMemoryBackend:
    mem = InMemoryBackend()
    rate_limit.set_backend(mem)
    return mem


def test_canonical_ipv6_uses_slash_64() -> None:
    a = canonical_client_id("2001:db8:1:2:aaaa:bbbb:cccc:dddd")
    b = canonical_client_id("2001:db8:1:2:1111:2222:3333:4444")
    assert a == b
    assert a == "2001:db8:1:2::"


def test_canonical_ipv4_is_full_address() -> None:
    assert canonical_client_id("203.0.113.9") == "203.0.113.9"
    assert canonical_client_id("203.0.113.10") != canonical_client_id("203.0.113.9")


def test_canonical_ipv4_mapped_ipv6() -> None:
    assert canonical_client_id("::ffff:203.0.113.9") == "203.0.113.9"


def test_incr_sets_ttl_only_on_first_hit(backend: InMemoryBackend) -> None:
    n1 = backend.incr_with_ttl("k", 60)
    ttl1 = backend.ttl("k")
    backend.advance(5)
    n2 = backend.incr_with_ttl("k", 60)
    ttl2 = backend.ttl("k")
    assert n1 == 1
    assert n2 == 2
    assert ttl1 == 60
    assert ttl2 == 55


def test_signup_sixth_request_in_one_minute_is_429(
    app_client: TestClient,
    db_session: Session,
    backend: InMemoryBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create = MagicMock(side_effect=lambda *_a, **_k: f"sub-{uuid.uuid4()}")
    monkeypatch.setattr("app.routers.auth.create_auth_user", create)
    monkeypatch.setattr("app.routers.auth.delete_auth_user", MagicMock())
    delay = MagicMock()
    monkeypatch.setattr("app.core.rate_limit.send_admin_alert_task.delay", delay)

    def _post() -> int:
        issued = create_invite(db_session, created_by=_CREATOR)
        db_session.commit()
        return app_client.post(
            "/auth/signup",
            json={
                "invite_token": issued.token,
                "email": f"{uuid.uuid4().hex}@example.com",
                "password": "a-long-enough-password",
            },
        ).status_code

    codes = [_post() for _ in range(SIGNUP_IP_MINUTE_LIMIT)]
    blocked = app_client.post(
        "/auth/signup",
        json={
            "invite_token": "x",
            "email": "blocked@example.com",
            "password": "a-long-enough-password",
        },
    )
    assert codes == [201] * SIGNUP_IP_MINUTE_LIMIT
    assert blocked.status_code == 429
    assert blocked.json()["detail"] == RATE_LIMIT_DETAIL
    assert int(blocked.headers["retry-after"]) >= 1
    delay.assert_called()


def test_unknown_token_does_not_create_per_token_key(
    app_client: TestClient, backend: InMemoryBackend
) -> None:
    app_client.post(
        "/auth/signup",
        json={
            "invite_token": "totally-unknown-token",
            "email": "n@example.com",
            "password": "a-long-enough-password",
        },
    )
    assert all("signup:token:" not in k for k in backend.stored_keys())


def test_eleventh_failure_on_known_invite_is_429(
    app_client: TestClient,
    db_session: Session,
    backend: InMemoryBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.routers.auth.create_auth_user", MagicMock())
    issued = create_invite(db_session, created_by=_CREATOR, email="bound@example.com")
    db_session.commit()
    payload = {
        "invite_token": issued.token,
        "email": "wrong@example.com",
        "password": "a-long-enough-password",
    }
    for i in range(SIGNUP_TOKEN_FAIL_LIMIT):
        resp = app_client.post("/auth/signup", json=payload)
        assert resp.status_code == 400
        # Keep this test on the token bucket, not the per-IP minute cap.
        if (i + 1) % 4 == 0:
            backend.advance(SIGNUP_IP_MINUTE_TTL + 1)
    blocked = app_client.post("/auth/signup", json=payload)
    assert blocked.status_code == 429
    assert any("signup:token:" in k for k in backend.stored_keys())


def test_create_invite_eleventh_in_one_minute_is_429(
    app_client: TestClient, backend: InMemoryBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.tests.test_admin_router import _headers

    monkeypatch.setattr("app.core.rate_limit.send_admin_alert_task.delay", MagicMock())
    for i in range(10):
        resp = app_client.post("/admin/invites", headers=_headers(), json={})
        assert resp.status_code == 201, i
    blocked = app_client.post("/admin/invites", headers=_headers(), json={})
    assert blocked.status_code == 429
    listed = app_client.get("/admin/invites", headers=_headers())
    assert listed.status_code == 200


def test_backend_error_is_503_not_429(
    backend: InMemoryBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Boom(InMemoryBackend):
        def incr_with_ttl(self, key: str, ttl_seconds: int) -> int:
            raise rate_limit.RateLimitUnavailable

    rate_limit.set_backend(Boom())
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "method": "POST",
        "path": "/auth/signup",
        "headers": [],
        "client": ("203.0.113.9", 12345),
        "scheme": "http",
        "server": ("test", 80),
    }
    request = Request(scope)
    with pytest.raises(HTTPException) as exc:
        rate_limit_signup(request)
    assert exc.value.status_code == 503
    assert exc.value.detail == UNAVAILABLE_DETAIL


def test_alert_enqueued_once_per_window(
    backend: InMemoryBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    delay = MagicMock()
    monkeypatch.setattr("app.core.rate_limit.send_admin_alert_task.delay", delay)
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "method": "POST",
        "path": "/auth/signup",
        "headers": [],
        "client": ("198.51.100.7", 1),
        "scheme": "http",
        "server": ("test", 80),
    }
    request = Request(scope)
    for _ in range(SIGNUP_IP_MINUTE_LIMIT):
        rate_limit_signup(request)
    with pytest.raises(HTTPException) as first:
        rate_limit_signup(request)
    with pytest.raises(HTTPException) as second:
        rate_limit_signup(request)
    assert first.value.status_code == 429
    assert second.value.status_code == 429
    delay.assert_called_once()


def test_client_id_from_request_uses_peer() -> None:
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "method": "POST",
        "path": "/",
        "headers": [],
        "client": ("2001:db8:1:2::abcd", 443),
        "scheme": "http",
        "server": ("test", 80),
    }
    assert client_id_from_request(Request(scope)) == "2001:db8:1:2::"


def test_guard_skips_unknown_token(db_session: Session, backend: InMemoryBackend) -> None:
    guard_known_invite_token(db_session, "no-such")
    assert backend.stored_keys() == []


def test_dockerfile_uvicorn_trusts_caddy_proxy_headers() -> None:
    text = (_REPO_ROOT / "backend" / "Dockerfile").read_text()
    assert "--proxy-headers" in text
    assert "--forwarded-allow-ips" in text
    # Backend publishes no host port; * is the compose-network allow-list.
    assert '"*"' in text or ', "*"' in text or '--forwarded-allow-ips", "*"' in text
