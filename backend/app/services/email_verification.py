"""Generic email-verification flow (Ring 1-Email Validation design doc, issue
#260). Core mechanism only: create + send, GET-inert status lookup, POST
confirm. Delivery-status polling (design doc §3.3 step 6) lives in
app/tasks/email_verification_tasks.py, scheduled by the caller of
create_verification, not by this module.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models.email_verification import EmailVerification
from app.models.user import User
from app.services.altcha_challenge import verify_email_verification_solution
from app.services.email_sender import send_verification_email

# Design doc §六 suggested 24-72h; 48h is the midpoint default. Not
# Vigil's PoW-challenge TTL (minutes) — this is an unattended email
# round-trip, not a live browser session.
TOKEN_TTL = timedelta(hours=48)
_TOKEN_NBYTES = 16  # 128 bits, base64url-encoded by secrets.token_urlsafe

VERIFICATION_REJECTED_MESSAGE = "invalid or expired verification link"


class VerificationRejected(Exception):
    """Confirm failed. Message is always VERIFICATION_REJECTED_MESSAGE —
    callers must not distinguish missing / expired / already-used / bad-altcha
    (design doc §3.3 step 4: doesn't help an attacker tell those apart)."""


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _target_field(purpose: str) -> str | None:
    """Which `users` column a successful verification writes the address
    into. None for `ops_manual` — an unbound probe has no account row."""
    if purpose == "account_email":
        return "email"
    if purpose == "delivery_email":
        return "delivery_email"
    return None


def _supersede_prior_pending(
    session: Session, *, purpose: str, email: str, user_id: uuid.UUID | None
) -> None:
    """Retire this (user, purpose)'s previous live candidate before creating
    a new one — design doc §3.2: at most one pending record per purpose.

    `purpose=ops_manual` always carries user_id=None (§3.5: a bound Ops API
    call passes account_email/delivery_email instead), so scoping by
    (user_id, purpose) alone would group EVERY unbound probe together
    regardless of which address was probed — wrong: probing b@y.com must not
    retire a still-pending probe of a@x.com. Scope by (purpose, email)
    instead when there's no account to scope by.
    """
    conditions = [EmailVerification.purpose == purpose, EmailVerification.status == "pending"]
    if user_id is not None:
        conditions.append(EmailVerification.user_id == user_id)
    else:
        conditions.append(EmailVerification.user_id.is_(None))
        conditions.append(EmailVerification.email == email)
    session.execute(update(EmailVerification).where(*conditions).values(status="superseded"))


def create_verification(
    session: Session,
    *,
    email: str,
    purpose: str,
    user_id: uuid.UUID | None = None,
) -> EmailVerification:
    """Create a pending verification record and send the verification email.

    The record is committed BEFORE the send attempt (matching
    send_report_email's persist-first discipline) — a Resend API hiccup must
    not lose the record of intent; it only leaves provider_message_id unset.
    """
    now = datetime.now(UTC)
    _supersede_prior_pending(session, purpose=purpose, email=email, user_id=user_id)

    token = secrets.token_urlsafe(_TOKEN_NBYTES)
    record = EmailVerification(
        user_id=user_id,
        purpose=purpose,
        email=email,
        token_hash=_hash_token(token),
        status="pending",
        expires_at=now + TOKEN_TTL,
        last_sent_at=now,
        resend_count=0,
    )
    session.add(record)
    session.commit()

    provider_message_id = send_verification_email(email, token)
    if provider_message_id is not None:
        record.provider_message_id = provider_message_id
        session.commit()
        # Lazy import (matches report_tasks.py's own lazy `SessionLocal`
        # import) — keeps this service module free of a module-level
        # dependency on the Celery task graph.
        from app.tasks.email_verification_tasks import (
            POLL_DELAY_SECONDS,
            poll_email_verification_delivery,
        )

        poll_email_verification_delivery.apply_async(
            args=[str(record.id)], countdown=POLL_DELAY_SECONDS
        )
    return record


@dataclass
class VerificationStatusResult:
    found: bool
    status: str | None
    email: str | None


def get_verification_status(session: Session, *, token: str) -> VerificationStatusResult:
    """Inert lookup for the confirm page's initial GET render. No writes —
    an expired-but-still-`pending` row is reported as "expired" here without
    persisting that transition; only confirm_verification (a POST) writes
    the expired status, keeping this GET side-effect-free (design doc §3.3
    step 2 / Vigil §4.2)."""
    record = session.execute(
        select(EmailVerification).where(EmailVerification.token_hash == _hash_token(token))
    ).scalar_one_or_none()
    if record is None:
        return VerificationStatusResult(found=False, status=None, email=None)
    effective_status = record.status
    if effective_status == "pending" and record.expires_at < datetime.now(UTC):
        effective_status = "expired"
    return VerificationStatusResult(found=True, status=effective_status, email=record.email)


def confirm_verification(session: Session, *, token: str, altcha_payload: str) -> EmailVerification:
    """Consume a confirm click (the only state-changing step — design doc
    §3.3 step 4). Raises VerificationRejected for any failure; never
    distinguishes which check failed in the exception message."""
    if not verify_email_verification_solution(altcha_payload):
        raise VerificationRejected(VERIFICATION_REJECTED_MESSAGE)

    record = session.execute(
        select(EmailVerification).where(EmailVerification.token_hash == _hash_token(token))
    ).scalar_one_or_none()
    if record is None or record.status != "pending":
        raise VerificationRejected(VERIFICATION_REJECTED_MESSAGE)

    now = datetime.now(UTC)
    if record.expires_at < now:
        record.status = "expired"
        session.commit()
        raise VerificationRejected(VERIFICATION_REJECTED_MESSAGE)

    record.status = "verified"
    record.verified_at = now

    field = _target_field(record.purpose)
    if field is not None and record.user_id is not None:
        user = session.get(User, record.user_id)
        if user is not None:
            setattr(user, field, record.email)
            verified_field = (
                "email_verified_at" if field == "email" else "delivery_email_verified_at"
            )
            setattr(user, verified_field, now)

    session.commit()
    return record
