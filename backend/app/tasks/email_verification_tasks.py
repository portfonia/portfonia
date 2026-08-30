"""Delivery-status poll for email verifications (Ring 1-Email Validation
design doc §3.3 step 6, issue #260).

Deliberately a poll, not a webhook receiver — Resend's GET /emails/{id} needs
a `full_access`-scoped key (RESEND_ALL_ACCESS_API_KEY), separate from the
`sending_access` key the send path uses; see app/core/config.py's comment on
that setting.
"""

from __future__ import annotations

import logging
from typing import Any, cast
from uuid import UUID

import httpx
from sqlalchemy import update
from sqlalchemy.engine import CursorResult

from app.core.config import get_settings
from app.tasks import celery_app

logger = logging.getLogger(__name__)

_RESEND_EMAIL_URL = "https://api.resend.com/emails/{id}"

# design doc §3.3 step 6: "5-10 分钟" — give Resend's own bounce/complaint
# pipeline time to report before polling.
POLL_DELAY_SECONDS = 600

# last_event values that unambiguously mean "will not reach the recipient" —
# see app/core/config.py's RESEND_ALL_ACCESS_API_KEY comment for where these
# came from (Resend's own docs, WebFetch-verified 2026-08-29).
_UNDELIVERABLE_EVENTS = frozenset({"bounced", "complained", "failed", "suppressed"})


@celery_app.task(  # type: ignore[untyped-decorator]
    name="app.tasks.email_verification_tasks.send_account_email_verification_task",
    bind=True,
    max_retries=3,
    default_retry_delay=120,
)
def send_account_email_verification_task(self: Any, user_id: str) -> str:
    """Create and send the account-email verification for a freshly signed-up
    user (issue #262; Ring 1-Profile Page.md §8.7, Email Validation.md §4.1).

    Runs on Celery, never inside the signup request: create_verification
    makes a synchronous Resend HTTP call (15s timeout), which must not sit
    on the signup response path. Its failure modes are already
    self-contained — send-first ordering means VerificationSendFailed
    leaves zero DB writes, and a persist failure logs loudly before
    re-raising — so this task just lets exceptions propagate for Celery's
    retry machinery (Profile Page.md §8.7: account creation itself must
    never be dragged down by whether the verification email went out).
    """
    from app.core.database import SessionLocal
    from app.models.user import User
    from app.services.email_verification import create_verification

    session = SessionLocal()
    try:
        user = session.get(User, UUID(user_id))
        if user is None:
            # The signup row was rolled back or the user purged between
            # enqueue and execution — a steady state, not an error.
            logger.warning("account-email verification task: user %s not found", user_id)
            return "skipped_not_found"
        create_verification(session, email=user.email, purpose="account_email", user_id=user.id)
        return "sent"
    finally:
        session.close()


@celery_app.task(  # type: ignore[untyped-decorator]
    name="app.tasks.email_verification_tasks.poll_email_verification_delivery",
    bind=True,
    max_retries=3,
    default_retry_delay=120,
)
def poll_email_verification_delivery(self: Any, verification_id: str) -> str:
    """Poll Resend once for `verification_id`'s delivery outcome. Marks the
    record `undeliverable` on a hard negative signal; otherwise a no-op —
    this task does not retry-poll for a positive outcome (the click-confirm
    path is the source of truth for "verified", not this poll).

    Returns a short status string for task-result inspection; never raises
    on a missing key/record (those are expected steady states, not errors).
    """
    from app.core.database import SessionLocal
    from app.models.email_verification import EmailVerification

    settings = get_settings()
    if settings.RESEND_ALL_ACCESS_API_KEY is None:
        logger.info("email verification poll skipped: RESEND_ALL_ACCESS_API_KEY unset")
        return "skipped_no_key"

    session = SessionLocal()
    try:
        record = session.get(EmailVerification, UUID(verification_id))
        if record is None:
            logger.warning("email verification poll: record %s not found", verification_id)
            return "skipped_not_found"
        if record.status != "pending":
            # Already verified/expired/superseded by the time this fired —
            # nothing to do, and never downgrade a terminal status.
            return f"skipped_status_{record.status}"
        if not record.provider_message_id:
            logger.warning(
                "email verification poll: record %s has no provider_message_id",
                verification_id,
            )
            return "skipped_no_provider_id"

        api_key = settings.RESEND_ALL_ACCESS_API_KEY.get_secret_value()
        with httpx.Client(timeout=15.0) as client:
            resp = client.get(
                _RESEND_EMAIL_URL.format(id=record.provider_message_id),
                headers={"Authorization": f"Bearer {api_key}"},
            )
        if resp.status_code == 404:
            # Resend has no record of this id — not the same as a delivery
            # failure; leave the row pending rather than guessing.
            return "skipped_not_found_at_provider"
        resp.raise_for_status()
        last_event = resp.json().get("last_event")
        if last_event in _UNDELIVERABLE_EVENTS:
            # Conditional UPDATE, not an assign-then-commit on the loaded
            # `record` (review, PR #261): this task runs on its own
            # SessionLocal(), a separate connection from whatever session a
            # concurrent confirm click commits through. Under READ
            # COMMITTED, the `record.status != "pending"` check above can
            # read stale — a click that verifies (or expires/supersedes)
            # the row in the ~10-minute gap between that read and this
            # write would otherwise get silently overwritten back to
            # undeliverable. The WHERE clause makes this row-level: it can
            # only ever move a row that is STILL pending at write time, no
            # matter what this task read earlier.
            result = cast(
                CursorResult[Any],
                session.execute(
                    update(EmailVerification)
                    .where(EmailVerification.id == record.id, EmailVerification.status == "pending")
                    .values(status="undeliverable")
                ),
            )
            session.commit()
            if result.rowcount == 0:
                logger.info(
                    "email verification %s: bounce detected but the row moved on "
                    "(no longer pending) before this write — not overwriting",
                    verification_id,
                )
                return "skipped_no_longer_pending_at_write"
            logger.info(
                "email verification %s marked undeliverable (last_event=%s)",
                verification_id,
                last_event,
            )
            return f"undeliverable_{last_event}"
        return f"ok_{last_event}"
    except httpx.HTTPError as exc:
        logger.exception("email verification poll failed for %s", verification_id)
        raise self.retry() from exc
    finally:
        session.close()
