"""One-shot Resend delivery poll for invitation letters."""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

import httpx

from app.core.config import get_settings
from app.core.timezones import ET
from app.models.invite import Invite
from app.services.email_sender import send_ops_alert
from app.tasks import celery_app
from app.tasks.email_verification_tasks import (
    RESEND_EMAIL_URL,
    UNDELIVERABLE_EVENTS,
    alert_resend_all_access_key_issue,
)

logger = logging.getLogger(__name__)


@celery_app.task(  # type: ignore[untyped-decorator]
    name="app.tasks.invitation_letter_tasks.poll_invitation_letter_delivery",
    bind=True,
    max_retries=3,
    default_retry_delay=120,
)
def poll_invitation_letter_delivery(self: Any, invite_id: str) -> str:
    from app.core.database import SessionLocal

    settings = get_settings()
    if settings.RESEND_ALL_ACCESS_API_KEY is None:
        alert_resend_all_access_key_issue("missing")
        return "skipped_no_key"
    session = SessionLocal()
    try:
        invite = session.get(Invite, UUID(invite_id))
        if invite is None or not invite.letter_provider_message_id:
            logger.warning("invitation letter poll: invite %s missing or unsent", invite_id)
            return "skipped_no_provider_id"
        with httpx.Client(timeout=15.0) as client:
            response = client.get(
                RESEND_EMAIL_URL.format(id=invite.letter_provider_message_id),
                headers={
                    "Authorization": f"Bearer {settings.RESEND_ALL_ACCESS_API_KEY.get_secret_value()}"
                },
            )
        if response.status_code == 404:
            return "skipped_not_found_at_provider"
        if response.status_code == 401:
            alert_resend_all_access_key_issue("unauthorized")
            return "skipped_unauthorized"
        response.raise_for_status()
        event = response.json().get("last_event")
        if not isinstance(event, str) or not event:
            raise ValueError("Resend response has no last_event")
        invite.letter_delivery_event = event
        session.commit()
        if event in UNDELIVERABLE_EVENTS:
            logger.warning("invitation letter %s delivery event %s", invite_id, event)
            sent_at = (
                invite.letter_sent_at.astimezone(ET).strftime("%Y-%m-%d %H:%M ET")
                if invite.letter_sent_at
                else "unknown"
            )
            send_ops_alert(
                subject=f"Portfonia ops: invitation letter to {invite.email} {event}",
                body=(
                    f"Recipient: {invite.email}\nEvent: {event}\nSent at (ET): {sent_at}\n"
                    f"Waitlist: {'on the waitlist' if invite.waitlist_entry_id else 'not on the waitlist'}\n"
                    "No automatic action was taken; the invite link will simply go unused."
                ),
                severity="ALERT",
                idempotency_key=f"invitation-letter-delivery:{invite_id}:{event}",
            )
        elif event == "delivery_delayed":
            logger.warning("invitation letter %s delivery delayed", invite_id)
        else:
            logger.info("invitation letter %s delivery event %s", invite_id, event)
        return f"ok_{event}"
    except (httpx.HTTPError, ValueError) as exc:
        logger.exception("invitation letter poll failed for %s", invite_id)
        raise self.retry() from exc
    finally:
        session.close()
