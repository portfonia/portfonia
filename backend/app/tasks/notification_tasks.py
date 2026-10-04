"""Explicit, user-triggered notification emails (issue #202).

Distinct from report_tasks.py: nothing here is a scheduled/formal report —
no `reports` row, no `user_watermark()`/period_start/period_end. Each task
is dispatched from a user clicking a button, not from Beat or a report
fan-out.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from app.services.email_sender import send_api_access_notice, send_portfolio_overview_email
from app.tasks import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(  # type: ignore[untyped-decorator]
    name="app.tasks.notification_tasks.send_portfolio_overview_email_task",
    bind=True,
    max_retries=0,
)
def send_portfolio_overview_email_task(self: Any, user_id: str, base_currency: str) -> None:
    """Fire-and-forget send for POST /portfolio/send-overview.

    No retry: the router has already claimed the 15-minute cooldown slot
    before dispatching this (issue #202) — retrying a failed send here
    would either burn that claim on a delivery that never happened, or
    (if retried past the cooldown window) violate the very rate limit the
    endpoint just enforced. A failed send is visible via the ops alerts
    `send_portfolio_overview_email` already fires on an unresolved
    recipient, and via Resend's own dashboard for a transport failure.
    """
    from app.core.database import SessionLocal

    session = SessionLocal()
    try:
        send_portfolio_overview_email(session, UUID(user_id), base_currency)
    finally:
        session.close()


@celery_app.task(name="app.tasks.notification_tasks.send_api_access_notice_task")  # type: ignore[untyped-decorator]
def send_api_access_notice_task(user_id: str) -> None:
    """Resolve a verified address and send one claimed ET-day access notice."""
    from app.core.database import SessionLocal
    from app.models.user import User
    from app.services.api_token_revoke_link import create_link
    from app.services.user_directory import recipient_email_with_purpose

    with SessionLocal() as session:
        uid = UUID(user_id)
        recipient = recipient_email_with_purpose(session, uid)
        if recipient is None:
            logger.info("API access notice skipped: no verified recipient user_id=%s", uid)
            return
        user = session.get(User, uid)
        assert user is not None
        revoke_url = f"https://portfonia.com/agent/revoke?t={create_link(uid)}"
        send_api_access_notice(recipient[0], revoke_url, locale=user.locale)
