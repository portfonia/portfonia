"""Self-hosted Altcha proof-of-work challenge, used by POST /auth/forgot-password
(issue #231).

Pure library (`altcha` on PyPI, the official altcha-org/altcha-lib-py), no
hosted verification service (no Sentinel) and no external CDN for the widget
JS — the widget bundle is vendored into frontend/public/altcha.js and served
same-origin (see that file's header comment).

Protocol version: the v1 API (`create_challenge_v1`/`verify_solution_v1`) —
the classic SHA-256 counting PoW that the pinned frontend widget
(`altcha` npm package, pinned to ^2, NOT the v3 "next-gen" KDF-based
protocol) speaks via its `challengeurl`/`maxnumber` attributes. The
challenge is self-contained and stateless: `create_challenge_v1` embeds an
expiry into the HMAC-signed salt, and `verify_solution_v1` recomputes the
expected challenge from the same HMAC key and checks both the signature and
the expiry — no Redis/DB round trip needed to verify a solution.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast

from altcha import v1 as altcha_v1

from app.core.config import get_settings

# Long enough for a slow client/CPU to solve the PoW and submit the form,
# short enough that a scraped challenge is useless shortly after.
CHALLENGE_TTL = timedelta(minutes=3)


def _hmac_key() -> str:
    # No new secret: APP_SECRET_KEY is already a required, server-only value.
    return get_settings().APP_SECRET_KEY.get_secret_value()


def create_forgot_password_challenge() -> dict[str, object]:
    """Return the JSON shape GET /auth/altcha-challenge hands to the widget.

    Uses the `ChallengeOptions` overload of `create_challenge` (not the
    keyword-args overload) — the keyword-args overload's type stub requires
    every field explicitly (no defaults), unlike the runtime function
    itself, so `ChallengeOptions`'s own real defaults are the only way to
    satisfy `mypy --strict` without hardcoding altcha's internal constants
    here.
    """
    options = altcha_v1.ChallengeOptions(
        hmac_key=_hmac_key(),
        expires=datetime.now(UTC) + CHALLENGE_TTL,
    )
    challenge: altcha_v1.Challenge = altcha_v1.create_challenge(options)
    # Challenge.to_dict() is typed as returning bare `dict` upstream (not
    # dict[str, object]) — cast rather than let `Any` leak through under
    # mypy --strict; to_dict()'s actual keys are checked structurally by
    # test_challenge_shape_matches_the_pinned_widget_protocol.
    return cast(dict[str, object], challenge.to_dict())


def verify_forgot_password_solution(payload_b64: str) -> bool:
    """Verify the widget's solved-challenge payload (base64 JSON string).

    Returns False (never raises) for anything malformed, expired, or
    tampered with — callers map a False result to a plain 400, not a 500.
    """
    if not payload_b64:
        return False
    try:
        ok, _err = altcha_v1.verify_solution(payload_b64, _hmac_key(), check_expires=True)
    except Exception:
        return False
    return ok


def create_email_verification_challenge() -> dict[str, object]:
    """Return the JSON shape GET /email-verifications/altcha-challenge hands
    to the widget (issue #260). Same HMAC key, TTL, and stateless design as
    create_forgot_password_challenge above — a fresh function per call site
    is this file's existing convention (see the module docstring), not a
    shared name across unrelated flows.
    """
    options = altcha_v1.ChallengeOptions(
        hmac_key=_hmac_key(),
        expires=datetime.now(UTC) + CHALLENGE_TTL,
    )
    challenge: altcha_v1.Challenge = altcha_v1.create_challenge(options)
    return cast(dict[str, object], challenge.to_dict())


def verify_email_verification_solution(payload_b64: str) -> bool:
    """Verify the widget's solved-challenge payload for the email-verification
    confirm flow (issue #260). Same semantics as
    verify_forgot_password_solution above — never raises."""
    if not payload_b64:
        return False
    try:
        ok, _err = altcha_v1.verify_solution(payload_b64, _hmac_key(), check_expires=True)
    except Exception:
        return False
    return ok
