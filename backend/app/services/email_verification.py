"""Generic email-verification flow (Ring 1-Email Validation design doc, issue
#260). Core mechanism only: create + send, GET-inert status lookup, POST
confirm. Delivery-status polling (design doc §3.3 step 6) lives in
app/tasks/email_verification_tasks.py, scheduled by the caller of
create_verification, not by this module.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from app.models.email_verification import EmailVerification
from app.models.user import User
from app.services.altcha_challenge import verify_email_verification_solution
from app.services.email_sender import send_verification_email
from app.services.invites import _normalize_email

logger = logging.getLogger(__name__)

# Design doc §6 suggested 24-72h; 48h is the midpoint default. Not
# Vigil's PoW-challenge TTL (minutes) — this is an unattended email
# round-trip, not a live browser session.
TOKEN_TTL = timedelta(hours=48)
_TOKEN_NBYTES = 16  # 128 bits, base64url-encoded by secrets.token_urlsafe

# Design doc §3.4 / issue #260 Notes: "a simple per-record 60s resend
# cooldown is kept as basic hygiene" — protects against a caller (today,
# only the ADMIN_API_TOKEN-gated Ops API) create-and-sending in a tight
# loop, which would otherwise supersede-and-resend on every call.
RESEND_COOLDOWN = timedelta(seconds=60)

VERIFICATION_REJECTED_MESSAGE = "invalid or expired verification link"


class VerificationRejected(Exception):
    """Confirm failed. Message is always VERIFICATION_REJECTED_MESSAGE —
    callers must not distinguish missing / expired / already-used / bad-altcha
    / account-email-mismatch (design doc §3.3 step 4: doesn't help an
    attacker tell those apart)."""


class ResendTooSoon(Exception):
    """A prior pending record for this (user_id, purpose[, email]) scope was
    sent less than RESEND_COOLDOWN ago. Callers map this to 429."""


class VerificationSendFailed(Exception):
    """send_verification_email() failed. Raised BEFORE any DB write (review,
    PR #261 round 2 — see create_verification's docstring for why ordering
    matters here). Callers map this to 502, matching this codebase's own
    "external call failed, nothing local touched, safe to retry" convention
    (app/routers/admin.py's _auth_delete_or_502)."""


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _target_field(purpose: str) -> str | None:
    """Which `users` column a successful verification concerns. None for
    `ops_manual` — an unbound probe has no account row. Note this is NOT
    "which column gets overwritten" — see confirm_verification: only
    delivery_email is ever assigned a new value here. account_email (field
    "email") only ever confirms reachability of the address already on
    file (design doc §4.1: this section adds only a check of whether the
    account email is reachable, and does not touch the account email
    itself) — `users.email` is unique and is Supabase Auth's login
    identity, which this flow does not (and must not) update (review, PR
    #261: the original version did `setattr(user, "email", record.email)`
    unconditionally, silently changing sign-in email out from under Auth,
    or 500ing via a unique-constraint IntegrityError if the address
    belonged to someone else).
    """
    if purpose == "account_email":
        return "email"
    if purpose == "delivery_email":
        return "delivery_email"
    return None


def _find_live_pending(
    session: Session, *, purpose: str, email: str, user_id: uuid.UUID | None
) -> EmailVerification | None:
    """The current live candidate for this (user, purpose) scope, if any —
    shared by the cooldown check and the supersede step below so they agree
    on exactly the same row. See _supersede_prior_pending's old docstring
    for why an unbound ops_manual probe (user_id=None) is scoped by
    (purpose, email) instead of (user_id, purpose): purpose=ops_manual
    always carries user_id=None (§3.5 — a bound Ops call passes
    account_email/delivery_email instead), so scoping by (user_id, purpose)
    alone would group every unbound probe together regardless of address.
    """
    conditions = [EmailVerification.purpose == purpose, EmailVerification.status == "pending"]
    if user_id is not None:
        conditions.append(EmailVerification.user_id == user_id)
    else:
        conditions.append(EmailVerification.user_id.is_(None))
        conditions.append(EmailVerification.email == email)
    return session.execute(
        select(EmailVerification).where(*conditions).limit(1)
    ).scalar_one_or_none()


def _resolve_locale(session: Session, user_id: uuid.UUID | None) -> str:
    """Bare locale code (`en`/`zh`, same convention as `OUTPUT_LANG` — not
    the frontend's BCP-47 `zh-Hans` tag) for the verification email's copy.
    An unbound `ops_manual` probe has no account to read a preference from
    and defaults to `en` (ops itself reads English, per this repo's own
    language policy). `session.get(User, user_id)` here is a second lookup
    when a caller (e.g. the Ops router) already fetched the row for its own
    404 check — SQLAlchemy's identity map makes that a no-op re-query
    within the same session, not a second round trip."""
    if user_id is None:
        return "en"
    user = session.get(User, user_id)
    return user.locale if user is not None else "en"


def create_verification(
    session: Session,
    *,
    email: str,
    purpose: str,
    user_id: uuid.UUID | None = None,
) -> EmailVerification:
    """Send the verification email, and only on success create the pending
    record (superseding any live prior candidate for the same scope).

    Raises ResendTooSoon (before sending or touching the DB) if a live
    candidate for this scope was sent within RESEND_COOLDOWN. Raises
    VerificationSendFailed (before touching the DB) if the send itself
    fails.

    **Ordering matters and was wrong in an earlier version of this function
    (review, PR #261 round 2)**: persisting first (superseding the prior
    row + stamping `last_sent_at`) and only then attempting the send meant
    a failed send would destroy a still-working prior link, create a new
    record that was never actually delivered, and start the resend cooldown
    on a send that never happened — a caller's immediate retry would then
    hit `ResendTooSoon` even though nothing had actually been sent. The
    original justification ("commit before send, matching send_report_
    email's persist-first discipline, so a Resend hiccup can't lose the
    record of intent") doesn't hold here: `send_report_email` has no
    "supersede an existing record" step, so nothing else's validity depends
    on its row existing before the send succeeds. Here it does. External
    I/O that can fail goes first; local state only changes once it's known
    to have worked — same discipline as this repo's `DELETE /admin/users/
    {id}` (Auth deletion strictly before any local delete, 502 + zero local
    changes on failure, safe to retry).
    """
    normalized_email = _normalize_email(email)
    if normalized_email is None:
        # _normalize_email returns None only for a blank/whitespace-only
        # string. The Ops router validates this with a proper 422 before
        # calling in; this is a defensive backstop for this being a
        # generic, reusable service function with other future callers.
        raise ValueError("email must not be blank")

    now = datetime.now(UTC)
    prior = _find_live_pending(session, purpose=purpose, email=normalized_email, user_id=user_id)
    if prior is not None and now - prior.last_sent_at < RESEND_COOLDOWN:
        raise ResendTooSoon

    token = secrets.token_urlsafe(_TOKEN_NBYTES)
    locale = _resolve_locale(session, user_id)
    provider_message_id = send_verification_email(normalized_email, token, locale=locale)
    if provider_message_id is None:
        raise VerificationSendFailed

    # Only now — send confirmed successful — touch the DB.
    if prior is not None:
        # Conditional UPDATE + implicit no-op on rowcount==0, not a plain
        # `prior.status = "superseded"` attribute assignment (round-4 review
        # finding) — that flushes as an unconditional `UPDATE ... WHERE
        # id=:id`, the same unguarded-write shape round 3 fixed on confirm's
        # and the poll task's status transitions, just not generalized to
        # this third site. Reachable race: `prior` read here as pending, then
        # its own confirm click commits `verified` while this function's
        # Resend call is still in flight, and an unconditional write here
        # would clobber `verified` back to `superseded` — a diagnostic-only
        # lost update (no `users` write-back depends on it), but real. If the
        # row already moved on (rowcount 0), nothing to do: the new record
        # below is created regardless.
        session.execute(
            update(EmailVerification)
            .where(EmailVerification.id == prior.id, EmailVerification.status == "pending")
            .values(status="superseded")
        )
    sent_at = datetime.now(UTC)
    record = EmailVerification(
        user_id=user_id,
        purpose=purpose,
        email=normalized_email,
        token_hash=_hash_token(token),
        status="pending",
        expires_at=sent_at + TOKEN_TTL,
        last_sent_at=sent_at,
        resend_count=0,
        provider_message_id=provider_message_id,
    )
    session.add(record)
    try:
        session.commit()
    except Exception:
        # The email is already sent at this point — a persist failure here
        # (e.g. the target user gets hard-purged between the router's 404
        # pre-check and this INSERT, tripping the user_id FK; a DB outage)
        # leaves a live emailed link with no matching row, and the send-
        # failure 502's "no local data was touched, retry" guarantee no
        # longer applies (review, PR #261 round 3). Re-raised as-is (a bare
        # 500 at the router) rather than invented a new status code for
        # what should be a rare edge case — but logged loudly so whoever
        # investigates knows a naive retry would send a SECOND email, not
        # safely resend the first.
        logger.error(
            "email verification: sent to %s (provider_message_id=%s) but failed to "
            "persist the record — this link is now dead; a retry will send a NEW "
            "email, not resend this one",
            normalized_email,
            provider_message_id,
        )
        raise

    # Lazy import (matches report_tasks.py's own lazy `SessionLocal` import)
    # — keeps this service module free of a module-level dependency on the
    # Celery task graph.
    from app.tasks.email_verification_tasks import (
        POLL_DELAY_SECONDS,
        poll_email_verification_delivery,
    )

    try:
        poll_email_verification_delivery.apply_async(
            args=[str(record.id)], countdown=POLL_DELAY_SECONDS
        )
    except Exception:
        # Best-effort by design (§3.3 step 6 is a lightweight diagnostic
        # poll, not load-bearing for the confirm flow) — a broker outage
        # here must not turn an otherwise-successful create+send+persist
        # into a misleading error (review, PR #261 round 3). The record is
        # already committed and the email already sent; a missed poll just
        # means an eventual bounce won't be auto-detected.
        logger.exception(
            "email verification %s: failed to schedule delivery-status poll", record.id
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
    distinguishes which check failed in the exception message.

    **Both status transitions below are conditional `UPDATE ... WHERE
    status='pending'` writes, not plain attribute assignment on the `record`
    loaded at the top (review, PR #261 round 3)**: an earlier version did
    `record.status = "verified"` / `"expired"` directly, which flushes as an
    unconditional `UPDATE ... WHERE id=:id` with no status guard. A click
    that reads `pending` here, racing a concurrent supersede (e.g. an Ops
    resend that just passed the cooldown) or a concurrent expiry, could
    still commit `verified` after the row had already moved on — resurrecting
    a dead token, and for `purpose=delivery_email`, writing that dead
    record's (possibly stale) email into `users.delivery_email` after a
    newer resend was supposed to have superseded it. This is the exact race
    class `poll_email_verification_delivery` was fixed to avoid in round 1;
    this function just never got the same guard. The `users` write-back
    only happens after the conditional UPDATE confirms (via `rowcount`)
    that this call is the one that actually performed the pending->verified
    transition — never based on the `record` object read before that point.
    """
    if not verify_email_verification_solution(altcha_payload):
        raise VerificationRejected(VERIFICATION_REJECTED_MESSAGE)

    record = session.execute(
        select(EmailVerification).where(EmailVerification.token_hash == _hash_token(token))
    ).scalar_one_or_none()
    if record is None or record.status != "pending":
        raise VerificationRejected(VERIFICATION_REJECTED_MESSAGE)

    now = datetime.now(UTC)
    if record.expires_at < now:
        # Conditional even though the outcome for this call is the same
        # either way (reject) — this must never overwrite a row that a
        # concurrent operation already moved to something else since the
        # SELECT above.
        session.execute(
            update(EmailVerification)
            .where(EmailVerification.id == record.id, EmailVerification.status == "pending")
            .values(status="expired")
        )
        session.commit()
        raise VerificationRejected(VERIFICATION_REJECTED_MESSAGE)

    # Business-rule check on the as-read state — no DB write yet, so a
    # rejection here never touches this record's status at all.
    field = _target_field(record.purpose)
    user = session.get(User, record.user_id) if record.user_id is not None else None
    if field == "email" and (user is None or user.email != record.email):
        # account_email confirms reachability of the address already on
        # file — never assign users.email (see _target_field's docstring).
        # A mismatch (the account's email changed since this record was
        # created, or a caller probed the wrong address for this user_id)
        # is rejected with the same generic error as every other failure
        # here, not a distinct message.
        raise VerificationRejected(VERIFICATION_REJECTED_MESSAGE)

    # The one write that actually matters for the race: claim the
    # pending->verified transition atomically. rowcount==0 means some
    # concurrent operation (supersede, expiry, another confirm) already
    # moved this row off `pending` between the SELECT above and here —
    # reject, and critically, do NOT fall through to the `users` write.
    result = cast(
        CursorResult[Any],
        session.execute(
            update(EmailVerification)
            .where(EmailVerification.id == record.id, EmailVerification.status == "pending")
            .values(status="verified", verified_at=now)
        ),
    )
    if result.rowcount == 0:
        session.rollback()
        raise VerificationRejected(VERIFICATION_REJECTED_MESSAGE)

    if field == "email":
        user.email_verified_at = now  # type: ignore[union-attr]  # non-None, checked above
    elif field == "delivery_email" and user is not None:
        user.delivery_email = record.email
        user.delivery_email_verified_at = now

    session.commit()
    return record
