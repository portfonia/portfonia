"""POST /auth/forgot-password (issue #231): PoW + rate limit + local-DB
exists/not-exists response + backend-mediated Supabase reset trigger."""

from __future__ import annotations

import uuid
from typing import cast
from unittest.mock import MagicMock

import pytest
from altcha import v1 as altcha_v1
from altcha.v1 import AlgoType
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core import rate_limit
from app.core.rate_limit import (
    FORGOT_PASSWORD_EMAIL_HOUR_LIMIT,
    FORGOT_PASSWORD_IP_MINUTE_LIMIT,
    RATE_LIMIT_DETAIL,
    UNAVAILABLE_DETAIL,
    InMemoryBackend,
)
from app.models.user import User
from app.services.auth_provider import AuthProviderError


@pytest.fixture
def backend() -> InMemoryBackend:
    mem = InMemoryBackend()
    rate_limit.set_backend(mem)
    return mem


def _solved_altcha(app_client: TestClient) -> str:
    challenge_resp = app_client.get("/auth/altcha-challenge")
    assert challenge_resp.status_code == 200
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


@pytest.fixture
def _fake_reset_trigger(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    trigger = MagicMock()
    monkeypatch.setattr("app.routers.auth.request_password_reset", trigger)
    return trigger


def test_unknown_email_reports_account_not_found_and_never_calls_supabase(
    app_client: TestClient,
    backend: InMemoryBackend,
    _fake_reset_trigger: MagicMock,
) -> None:
    altcha_payload = _solved_altcha(app_client)
    resp = app_client.post(
        "/auth/forgot-password",
        json={"email": "nobody@example.com", "altcha": altcha_payload},
    )
    assert resp.status_code == 200
    assert resp.json() == {"account_found": False}
    _fake_reset_trigger.assert_not_called()


def test_known_email_reports_found_and_triggers_supabase_reset(
    app_client: TestClient,
    db_session: Session,
    backend: InMemoryBackend,
    _fake_reset_trigger: MagicMock,
) -> None:
    user = User(
        id=uuid.uuid4(),
        auth_provider="supabase",
        auth_subject=f"sub-{uuid.uuid4()}",
        email="known@example.com",
        status="active",
        locale="en",
        base_currency="USD",
        report_cadence="weekly",
    )
    db_session.add(user)
    db_session.flush()

    altcha_payload = _solved_altcha(app_client)
    resp = app_client.post(
        "/auth/forgot-password",
        json={"email": "Known@Example.com", "altcha": altcha_payload},
    )
    assert resp.status_code == 200
    assert resp.json() == {"account_found": True}
    _fake_reset_trigger.assert_called_once()
    called_email, kwargs = _fake_reset_trigger.call_args[0][0], _fake_reset_trigger.call_args[1]
    assert called_email == "known@example.com"
    assert kwargs["redirect_to"].endswith("/reset-password")


def test_missing_or_garbage_altcha_payload_is_rejected_before_db_lookup(
    app_client: TestClient,
    backend: InMemoryBackend,
    _fake_reset_trigger: MagicMock,
) -> None:
    resp = app_client.post(
        "/auth/forgot-password",
        json={"email": "someone@example.com", "altcha": "not-a-real-solution"},
    )
    assert resp.status_code == 400
    _fake_reset_trigger.assert_not_called()


def test_reused_altcha_challenge_still_verifies_within_ttl(
    app_client: TestClient,
    backend: InMemoryBackend,
    _fake_reset_trigger: MagicMock,
) -> None:
    """The challenge is stateless (no server-side single-use tracking, by
    design — see altcha_challenge.py) — this documents that tradeoff rather
    than asserting single-use, which the current design does not provide."""
    altcha_payload = _solved_altcha(app_client)
    r1 = app_client.post(
        "/auth/forgot-password",
        json={"email": "a@example.com", "altcha": altcha_payload},
    )
    r2 = app_client.post(
        "/auth/forgot-password",
        json={"email": "b@example.com", "altcha": altcha_payload},
    )
    assert r1.status_code == 200
    assert r2.status_code == 200


def test_ip_rate_limit_trips_after_the_per_minute_limit(
    app_client: TestClient,
    backend: InMemoryBackend,
    _fake_reset_trigger: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delay = MagicMock()
    monkeypatch.setattr("app.core.rate_limit.send_admin_alert_task.delay", delay)

    def _post(email: str) -> int:
        return app_client.post(
            "/auth/forgot-password",
            json={"email": email, "altcha": _solved_altcha(app_client)},
        ).status_code

    codes = [_post(f"{i}@example.com") for i in range(FORGOT_PASSWORD_IP_MINUTE_LIMIT)]
    blocked = app_client.post(
        "/auth/forgot-password",
        json={"email": "blocked@example.com", "altcha": _solved_altcha(app_client)},
    )

    assert all(c == 200 for c in codes)
    assert blocked.status_code == 429
    assert blocked.json()["detail"] == RATE_LIMIT_DETAIL
    assert "Retry-After" in blocked.headers


def test_email_rate_limit_trips_independently_of_ip(
    app_client: TestClient,
    backend: InMemoryBackend,
    _fake_reset_trigger: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delay = MagicMock()
    monkeypatch.setattr("app.core.rate_limit.send_admin_alert_task.delay", delay)

    same_email = "repeat@example.com"
    codes = [
        app_client.post(
            "/auth/forgot-password",
            json={"email": same_email, "altcha": _solved_altcha(app_client)},
        ).status_code
        for _ in range(FORGOT_PASSWORD_EMAIL_HOUR_LIMIT)
    ]
    blocked = app_client.post(
        "/auth/forgot-password",
        json={"email": same_email, "altcha": _solved_altcha(app_client)},
    )

    assert all(c == 200 for c in codes)
    assert blocked.status_code == 429


def test_redis_down_fails_closed_with_503(
    app_client: TestClient,
    _fake_reset_trigger: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.rate_limit import RateLimitUnavailable

    class _BoomBackend:
        def incr_with_ttl(self, key: str, ttl_seconds: int) -> int:
            raise RateLimitUnavailable

        def ttl(self, key: str) -> int:
            raise RateLimitUnavailable

        def set_nx(self, key: str, ttl_seconds: int) -> bool:
            raise RateLimitUnavailable

    rate_limit.set_backend(_BoomBackend())
    try:
        resp = app_client.post(
            "/auth/forgot-password",
            json={"email": "anyone@example.com", "altcha": _solved_altcha(app_client)},
        )
    finally:
        rate_limit.set_backend(None)
    assert resp.status_code == 503
    assert resp.json()["detail"] == UNAVAILABLE_DETAIL
    _fake_reset_trigger.assert_not_called()


def test_supabase_trigger_failure_surfaces_as_503(
    app_client: TestClient,
    db_session: Session,
    backend: InMemoryBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = User(
        id=uuid.uuid4(),
        auth_provider="supabase",
        auth_subject=f"sub-{uuid.uuid4()}",
        email="broken@example.com",
        status="active",
        locale="en",
        base_currency="USD",
        report_cadence="weekly",
    )
    db_session.add(user)
    db_session.flush()
    monkeypatch.setattr(
        "app.routers.auth.request_password_reset",
        MagicMock(side_effect=AuthProviderError("boom")),
    )

    resp = app_client.post(
        "/auth/forgot-password",
        json={"email": "broken@example.com", "altcha": _solved_altcha(app_client)},
    )
    assert resp.status_code == 503
