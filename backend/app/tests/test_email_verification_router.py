"""GET/POST /email-verifications/* (issue #260) — unauthenticated confirm
flow, same shape as /auth/forgot-password's Altcha-gated endpoints."""

from __future__ import annotations

from typing import cast
from unittest.mock import MagicMock

import pytest
from altcha import v1 as altcha_v1
from altcha.v1 import AlgoType
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.email_verification import EmailVerification
from app.services.email_verification import create_verification, get_verification_status


def _solved_altcha(app_client: TestClient) -> str:
    challenge_resp = app_client.get("/email-verifications/altcha-challenge")
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


def _create_and_capture_token(
    db_session: Session, monkeypatch: pytest.MonkeyPatch, *, email: str, purpose: str
) -> tuple[EmailVerification, str]:
    sender = MagicMock(return_value="test-provider-id")
    monkeypatch.setattr("app.services.email_verification.send_verification_email", sender)
    record = create_verification(db_session, email=email, purpose=purpose)
    return record, sender.call_args.args[1]


def test_altcha_challenge_returns_a_solvable_challenge(app_client: TestClient) -> None:
    resp = app_client.get("/email-verifications/altcha-challenge")
    assert resp.status_code == 200
    body = resp.json()
    assert {"algorithm", "challenge", "salt", "signature", "maxNumber"} <= body.keys()


def test_status_unknown_token_reports_not_found(app_client: TestClient) -> None:
    resp = app_client.get("/email-verifications/status", params={"token": "garbage"})
    assert resp.status_code == 200
    assert resp.json() == {"found": False, "status": None, "email": None}


def test_status_pending_token_reports_email_and_status(
    app_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _record, token = _create_and_capture_token(
        db_session, monkeypatch, email="a@example.com", purpose="ops_manual"
    )
    resp = app_client.get("/email-verifications/status", params={"token": token})
    assert resp.status_code == 200
    assert resp.json() == {"found": True, "status": "pending", "email": "a@example.com"}


def test_confirm_with_valid_token_and_altcha_succeeds(
    app_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _record, token = _create_and_capture_token(
        db_session, monkeypatch, email="a@example.com", purpose="ops_manual"
    )
    altcha_payload = _solved_altcha(app_client)

    resp = app_client.post(
        "/email-verifications/confirm", json={"token": token, "altcha": altcha_payload}
    )

    assert resp.status_code == 200
    assert resp.json() == {"email": "a@example.com"}
    assert get_verification_status(db_session, token=token).status == "verified"


def test_confirm_with_bad_altcha_returns_generic_400(
    app_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _record, token = _create_and_capture_token(
        db_session, monkeypatch, email="a@example.com", purpose="ops_manual"
    )

    resp = app_client.post(
        "/email-verifications/confirm", json={"token": token, "altcha": "not-a-real-solution"}
    )

    assert resp.status_code == 400
    assert resp.json()["detail"] == "invalid or expired verification link"


def test_confirm_with_unknown_token_returns_the_same_generic_400(
    app_client: TestClient,
) -> None:
    altcha_payload = _solved_altcha(app_client)
    resp = app_client.post(
        "/email-verifications/confirm",
        json={"token": "does-not-exist", "altcha": altcha_payload},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "invalid or expired verification link"
