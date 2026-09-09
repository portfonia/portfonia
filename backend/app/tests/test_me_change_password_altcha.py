"""Authed Altcha challenge + verify for the change-password page (issue #393)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import cast

import pytest
from altcha import v1 as altcha_v1
from altcha.v1 import AlgoType
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.database import get_session
from app.core.deps import current_principal
from app.main import app
from app.services.altcha_challenge import create_forgot_password_challenge


@pytest.fixture
def raw_client(db_session: Session) -> Iterator[TestClient]:
    def _override_session() -> Iterator[Session]:
        yield db_session

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides.pop(current_principal, None)
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _solve(challenge: dict[str, object]) -> str:
    algorithm = cast(AlgoType, challenge["algorithm"])
    solution = altcha_v1.solve_challenge(
        challenge=str(challenge["challenge"]),
        salt=str(challenge["salt"]),
        algorithm=algorithm,
        max_number=int(str(challenge["maxNumber"])),
    )
    assert solution is not None
    payload = altcha_v1.Payload(
        algorithm=algorithm,
        challenge=str(challenge["challenge"]),
        number=solution.number,
        salt=str(challenge["salt"]),
        signature=str(challenge["signature"]),
    )
    return payload.to_base64()


def test_challenge_without_token_is_401(raw_client: TestClient) -> None:
    resp = raw_client.get("/me/change-password/altcha-challenge")
    assert resp.status_code == 401


def test_verify_without_token_is_401(raw_client: TestClient) -> None:
    resp = raw_client.post(
        "/me/change-password/altcha-verify",
        json={"altcha": "anything"},
    )
    assert resp.status_code == 401


def test_challenge_shape_for_authed_caller(app_client: TestClient) -> None:
    resp = app_client.get("/me/change-password/altcha-challenge")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"algorithm", "challenge", "maxNumber", "salt", "signature"}
    assert body["algorithm"] == "SHA-256"


def test_verify_accepts_a_solved_challenge(app_client: TestClient) -> None:
    challenge = app_client.get("/me/change-password/altcha-challenge").json()
    resp = app_client.post(
        "/me/change-password/altcha-verify",
        json={"altcha": _solve(challenge)},
    )
    assert resp.status_code == 204


def test_verify_rejects_missing_or_garbage_payload(app_client: TestClient) -> None:
    missing = app_client.post("/me/change-password/altcha-verify", json={})
    assert missing.status_code == 422

    garbage = app_client.post(
        "/me/change-password/altcha-verify",
        json={"altcha": "not-a-real-solution"},
    )
    assert garbage.status_code == 400
    assert garbage.json()["detail"] == "invalid captcha"

    empty = app_client.post(
        "/me/change-password/altcha-verify",
        json={"altcha": ""},
    )
    assert empty.status_code == 400


def test_verify_rejects_a_forgot_password_solution(app_client: TestClient) -> None:
    """A payload solved against the public forgot-password challenge must
    not pass the change-password verifier (distinct HMAC purpose)."""
    payload = _solve(create_forgot_password_challenge())
    resp = app_client.post(
        "/me/change-password/altcha-verify",
        json={"altcha": payload},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "invalid captcha"
