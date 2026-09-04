"""Delivery-status poll for reports (issue #104, Ring 1-Email Validation
design doc's 2026-09-03 section "硬退信/投诉处理").

Same concurrency-safe polling shape as app/tasks/email_verification_tasks.py's
poll_email_verification_delivery (RESEND_EMAIL_URL, POLL_DELAY_SECONDS,
UNDELIVERABLE_EVENTS, and the missing/invalid-key ops alert are all imported
from there rather than re-derived — frozen requirement #7 keeps both tasks
behaving identically on a key issue). What differs: a hard bounce/complaint
here has real side effects — the corresponding users.*_verified_at is
cleared and an `email_verifications` audit row (status=auto_revoked) is
appended, distinguishable from a user-initiated unsubscribe (status=revoked,
issue #257) by status alone. `reports` itself gains no delivery-status
field (explicit non-goal) — state lives only in `users`/`email_verifications`.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import UTC, datetime
from typing import Any, cast

import httpx
from sqlalchemy import update
from sqlalchemy.engine import CursorResult

from app.core.config import get_settings
from app.tasks import celery_app
from app.tasks.email_verification_tasks import (
    RESEND_EMAIL_URL,
    UNDELIVERABLE_EVENTS,
    alert_resend_all_access_key_issue,
)

logger = logging.getLogger(__name__)

# report.recipient_purpose -> (users address column, users verified-at
# column). Only these two purposes ever appear there (reports.
# recipient_purpose CHECK constraint) — ops_manual never applies, a report
# is never sent to an Ops-probed address.
_RECIPIENT_FIELD_MAP: dict[str, tuple[str, str]] = {
    "account_email": ("email", "email_verified_at"),
    "delivery_email": ("delivery_email", "delivery_email_verified_at"),
}


@celery_app.task(  # type: ignore[untyped-decorator]
    name="app.tasks.report_delivery_tasks.poll_report_delivery",
    bind=True,
    max_retries=3,
    default_retry_delay=120,
)
def poll_report_delivery(self: Any, report_id: str) -> str:
    """Poll Resend once for *report_id*'s delivery outcome.

    On a hard bounce/complaint/failure/suppression (issue #104 requirement
    #3, no branching between them): clears the user's corresponding
    *_verified_at field, but ONLY if it still points at exactly the address
    THIS send used (requirement #2's rationale — the user may have changed
    their delivery address between send and now, and clearing the wrong
    field would be a precision miss, not a safety one) and appends an
    `auto_revoked` audit row. `complained` additionally fires one
    `send_ops_alert` (per event, no rate computation — issue #337 owns
    that). Never raises on a missing/incomplete record; retries only on a
    transient httpx error, matching poll_email_verification_delivery.
    """
    from app.core.database import SessionLocal
    from app.models.email_verification import EmailVerification
    from app.models.report import Report
    from app.models.user import User
    from app.services.email_sender import send_ops_alert

    settings = get_settings()
    if settings.RESEND_ALL_ACCESS_API_KEY is None:
        logger.info("report delivery poll skipped: RESEND_ALL_ACCESS_API_KEY unset")
        alert_resend_all_access_key_issue("missing")
        return "skipped_no_key"

    session = SessionLocal()
    try:
        report = session.get(Report, uuid.UUID(report_id))
        if report is None:
            logger.warning("report delivery poll: report %s not found", report_id)
            return "skipped_not_found"
        if not report.provider_message_id:
            logger.warning("report delivery poll: report %s has no provider_message_id", report_id)
            return "skipped_no_provider_id"
        if not report.recipient_email or not report.recipient_purpose:
            # A report sent before this feature shipped (issue #104
            # requirement #8: no backfill of historical sends) — nothing
            # recorded to act on.
            logger.info(
                "report delivery poll: report %s has no recorded recipient "
                "(sent before issue #104 shipped) — skipping",
                report_id,
            )
            return "skipped_no_recipient_recorded"

        api_key = settings.RESEND_ALL_ACCESS_API_KEY.get_secret_value()
        with httpx.Client(timeout=15.0) as client:
            resp = client.get(
                RESEND_EMAIL_URL.format(id=report.provider_message_id),
                headers={"Authorization": f"Bearer {api_key}"},
            )
        if resp.status_code == 404:
            # Resend has no record of this id — not the same as a delivery
            # failure; nothing to act on.
            return "skipped_not_found_at_provider"
        if resp.status_code == 401:
            # The key itself is invalid/revoked — retrying will not help,
            # unlike a transient httpx.HTTPError below.
            logger.error("report delivery poll: RESEND_ALL_ACCESS_API_KEY unauthorized (401)")
            alert_resend_all_access_key_issue("unauthorized")
            return "skipped_unauthorized"
        resp.raise_for_status()
        last_event = resp.json().get("last_event")
        if last_event not in UNDELIVERABLE_EVENTS:
            return f"ok_{last_event}"

        address_col, verified_col = _RECIPIENT_FIELD_MAP[report.recipient_purpose]
        # Conditional UPDATE, not a read-then-write on a loaded `user` object
        # (same discipline as poll_email_verification_delivery's own
        # conditional UPDATE): the WHERE clause both (a) guards against
        # acting on a stale address per requirement #2's rationale, and (b)
        # makes a redelivered task idempotent — a second run finds the
        # column already NULL and matches zero rows, so it cannot append a
        # duplicate audit row below.
        result = cast(
            CursorResult[Any],
            session.execute(
                update(User)
                .where(
                    User.id == report.user_id,
                    getattr(User, address_col) == report.recipient_email,
                    getattr(User, verified_col).is_not(None),
                )
                .values(**{verified_col: None})
            ),
        )
        if result.rowcount == 0:
            session.commit()
            logger.info(
                "report delivery poll: report %s last_event=%s but user_id=%s's %s no "
                "longer points at %s (already cleared, or the user has since changed "
                "the address) — no action taken",
                report_id,
                last_event,
                report.user_id,
                address_col,
                report.recipient_email,
            )
            return f"skipped_stale_recipient_{last_event}"

        now = datetime.now(UTC)
        session.add(
            EmailVerification(
                user_id=report.user_id,
                purpose=report.recipient_purpose,
                email=report.recipient_email,
                # Synthetic, never-sent token — this row records a system-
                # detected event, not an outstanding confirm link. sha256
                # hex, same format as _hash_token's real tokens, to satisfy
                # the column's UNIQUE + NOT NULL constraints without ever
                # colliding with (or being usable as) an actual token.
                token_hash=hashlib.sha256(
                    f"auto-revoked:{report.id}:{uuid.uuid4()}".encode()
                ).hexdigest(),
                status="auto_revoked",
                revoke_reason=last_event,
                expires_at=now,
                last_sent_at=now,
            )
        )
        session.commit()
        logger.info(
            "report delivery poll: report %s last_event=%s — cleared user_id=%s's %s",
            report_id,
            last_event,
            report.user_id,
            verified_col,
        )

        if last_event == "complained":
            send_ops_alert(
                subject="Portfonia ops: report recipient complained",
                body=(
                    f"report_id={report.id} user_id={report.user_id} "
                    f"recipient_email={report.recipient_email} ({report.recipient_purpose}) "
                    "marked a Portfonia report as spam/complaint via Resend. The "
                    "corresponding verified-at timestamp has been cleared; future "
                    "reports will not be sent to this address until it is re-verified."
                ),
            )

        return f"auto_revoked_{last_event}"
    except httpx.HTTPError as exc:
        logger.exception("report delivery poll failed for %s", report_id)
        raise self.retry() from exc
    finally:
        session.close()
