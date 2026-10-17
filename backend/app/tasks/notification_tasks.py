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

from celery import Task  # type: ignore[import-untyped]

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


@celery_app.task(  # type: ignore[untyped-decorator]
    name="app.tasks.notification_tasks.send_api_access_notice_task", bind=True, max_retries=1
)
def send_api_access_notice_task(self: Any, user_id: str) -> None:
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
        if not send_api_access_notice(recipient[0], revoke_url, locale=user.locale):
            if self.request.retries < self.max_retries:
                raise self.retry(countdown=300)
            logger.error("API access notice delivery failed after retry user_id=%s", uid)


@celery_app.task(  # type: ignore[untyped-decorator]
    name="app.tasks.notification_tasks.send_api_modification_notice_task", bind=True, max_retries=1
)
def send_api_modification_notice_task(self: Any, user_id: str) -> None:
    """One independently claimed modification notice with the existing retry policy."""
    from app.core.database import SessionLocal
    from app.models.user import User
    from app.services.api_token_revoke_link import create_link
    from app.services.email_sender import send_api_modification_notice
    from app.services.user_directory import recipient_email_with_purpose

    with SessionLocal() as session:
        uid = UUID(user_id)
        recipient = recipient_email_with_purpose(session, uid)
        if recipient is None:
            logger.info("API modification notice skipped: no verified recipient user_id=%s", uid)
            return
        user = session.get(User, uid)
        assert user is not None
        revoke_url = f"https://portfonia.com/agent/revoke?t={create_link(uid)}"
        if not send_api_modification_notice(recipient[0], revoke_url, locale=user.locale):
            if self.request.retries < self.max_retries:
                raise self.retry(countdown=300)
            logger.error("API modification notice delivery failed after retry user_id=%s", uid)


@celery_app.task(  # type: ignore[untyped-decorator]
    name="app.tasks.notification_tasks.send_holdings_change_alert_task", bind=True, max_retries=1
)
def send_holdings_change_alert_task(
    self: Task, user_id: str, group_id: str, claimed: bool = False
) -> None:
    """Evaluate one consistent reconstruction snapshot and retry delivery once."""
    from app.core.database import SessionLocal
    from app.services.holding_change_alerts import evaluate_change_alert

    try:
        with SessionLocal() as session:
            session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            delivered = evaluate_change_alert(
                session, UUID(user_id), UUID(group_id), claimed=claimed
            )
    except Exception:
        logger.error("Holdings change alert evaluation failed user_id=%s", user_id)
        return
    if not delivered:
        if self.request.retries < self.max_retries:
            raise self.retry(kwargs={"claimed": True}, countdown=300)
        logger.error("Holdings change alert delivery failed after retry user_id=%s", user_id)
