"""Report-email unsubscribe (issue #257, design doc §3.7).

GET-inert status lookup (token decode only — no DB) plus the one
state-changing confirm that clears the matching `users.*_verified_at`
field and appends a `status=revoked` audit row.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.models.email_verification import EmailVerification
from app.models.user import User
from app.services.unsubscribe_token import UnsubscribeClaims, verify_token

UNSUBSCRIBE_REJECTED_MESSAGE = "invalid or expired unsubscribe link"


class UnsubscribeRejected(Exception):
    """Confirm failed. Message is always UNSUBSCRIBE_REJECTED_MESSAGE —
    callers must not distinguish missing / expired / tampered / unknown user."""


@dataclass
class UnsubscribeStatusResult:
    found: bool
    email: str | None


def get_unsubscribe_status(token: str) -> UnsubscribeStatusResult:
    """Inert lookup for the confirm page's initial GET render. No writes
    and no DB reads — the token is self-contained. An email security
    gateway prefetch therefore cannot change any account state (design
    doc §3.7 / Vigil §4.2)."""
    claims = verify_token(token)
    if claims is None:
        return UnsubscribeStatusResult(found=False, email=None)
    return UnsubscribeStatusResult(found=True, email=claims.email)


def confirm_unsubscribe(session: Session, *, token: str) -> UnsubscribeClaims:
    claims = verify_token(token)
    if claims is None:
        raise UnsubscribeRejected(UNSUBSCRIBE_REJECTED_MESSAGE)

    user = session.get(User, claims.user_id)
    if user is None:
        raise UnsubscribeRejected(UNSUBSCRIBE_REJECTED_MESSAGE)

    # Scope is the address in the token, not the purpose and not the
    # account. If both `users.email` and `users.delivery_email` currently
    # hold that mailbox, both timestamps clear — otherwise a
    # delivery-purpose click would leave account-email verified and
    # #276's future gate would keep mailing the same inbox (PR #279
    # review). A replaced address (column no longer equals claims.email)
    # is left untouched.
    if user.email == claims.email:
        user.email_verified_at = None
    if user.delivery_email == claims.email:
        user.delivery_email_verified_at = None

    now = datetime.now(UTC)
    session.add(
        EmailVerification(
            user_id=claims.user_id,
            purpose=claims.purpose,
            email=claims.email,
            token_hash=hashlib.sha256(secrets.token_urlsafe(32).encode()).hexdigest(),
            status="revoked",
            expires_at=now,
            last_sent_at=now,
            resend_count=0,
        )
    )
    session.commit()
    return claims
