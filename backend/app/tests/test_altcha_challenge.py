"""Self-hosted Altcha PoW challenge for /auth/forgot-password (issue #231)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from altcha import v1 as altcha_v1
from altcha.v1 import AlgoType

from app.core.config import get_settings
from app.services.altcha_challenge import (
    create_change_password_challenge,
    create_email_verification_challenge,
    create_forgot_password_challenge,
    verify_change_password_solution,
    verify_email_verification_solution,
    verify_forgot_password_solution,
)


def _solve(challenge: dict[str, object]) -> str:
    """Brute-force the same way the widget/client would, for test purposes."""
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


def test_challenge_shape_matches_the_pinned_widget_protocol() -> None:
    challenge = create_forgot_password_challenge()
    assert set(challenge) == {"algorithm", "challenge", "maxNumber", "salt", "signature"}
    assert challenge["algorithm"] == "SHA-256"


def test_valid_solution_verifies() -> None:
    challenge = create_forgot_password_challenge()
    payload = _solve(challenge)
    assert verify_forgot_password_solution(payload) is True


def test_tampered_number_fails_verification() -> None:
    challenge = create_forgot_password_challenge()
    payload = _solve(challenge)
    tampered = altcha_v1.Payload.from_base64(payload)
    tampered.number += 1
    assert verify_forgot_password_solution(tampered.to_base64()) is False


def test_expired_challenge_fails_verification() -> None:
    hmac_key = get_settings().APP_SECRET_KEY.get_secret_value()
    options = altcha_v1.ChallengeOptions(
        hmac_key=hmac_key,
        expires=datetime.now(UTC) - timedelta(minutes=1),
    )
    challenge = altcha_v1.create_challenge(options).to_dict()
    payload = _solve(challenge)
    assert verify_forgot_password_solution(payload) is False


@pytest.mark.parametrize("garbage", ["", "not-base64-json", "eyJub3QiOiJhIHNvbHV0aW9uIn0="])
def test_malformed_payload_never_raises(garbage: str) -> None:
    assert verify_forgot_password_solution(garbage) is False


def test_change_password_challenge_shape_matches_the_pinned_widget_protocol() -> None:
    challenge = create_change_password_challenge()
    assert set(challenge) == {"algorithm", "challenge", "maxNumber", "salt", "signature"}
    assert challenge["algorithm"] == "SHA-256"


def test_change_password_valid_solution_verifies() -> None:
    payload = _solve(create_change_password_challenge())
    assert verify_change_password_solution(payload) is True


def test_change_password_malformed_payload_never_raises() -> None:
    assert verify_change_password_solution("") is False
    assert verify_change_password_solution("not-a-solution") is False


def test_change_password_hmac_is_distinct_from_other_purposes() -> None:
    """A solved forgot-password or email-verification challenge must not
    satisfy the change-password verifier (issue #393 — distinct purpose,
    not a silent reuse of the other flows' HMAC)."""
    forgot_payload = _solve(create_forgot_password_challenge())
    email_payload = _solve(create_email_verification_challenge())
    change_payload = _solve(create_change_password_challenge())

    assert verify_change_password_solution(forgot_payload) is False
    assert verify_change_password_solution(email_payload) is False
    assert verify_forgot_password_solution(change_payload) is False
    assert verify_email_verification_solution(change_payload) is False
    assert verify_change_password_solution(change_payload) is True
