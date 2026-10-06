"""Daily automatic invitation letters for eligible waitlist entries."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.timezones import ET, today_et
from app.models.invite import Invite
from app.models.waitlist_entry import WaitlistEntry
from app.services.email_sender import send_ops_alert as send_ops_alert
from app.services.invitation_letters import (
    LetterConflict,
    LetterLanguage,
    LetterSendFailed,
    send_letter,
)
from app.services.invites import signup_email_taken
from app.services.waitlist import views
from app.tasks import celery_app
from app.tasks.email_verification_tasks import UNDELIVERABLE_EVENTS

logger = logging.getLogger(__name__)

_LANGUAGE_NAMES: dict[LetterLanguage, str] = {
    "en": "English",
    "zh": "Simplified Chinese",
    "zh-Hant": "Traditional Chinese",
}


def _unsubscribed(session: Session, email: str) -> bool:
    return (
        session.scalar(
            select(Invite.id)
            .where(Invite.email == email, Invite.letter_unsubscribed_at.is_not(None))
            .limit(1)
        )
        is not None
    )


def _eligible(session: Session, entry: WaitlistEntry) -> bool:
    return (
        entry.status == "pending"
        and not signup_email_taken(session, entry.email)
        and not _unsubscribed(session, entry.email)
    )


def _section(lines: list[str]) -> str:
    if not lines:
        return "- none"
    return "\n".join(lines)


@celery_app.task(  # type: ignore[untyped-decorator]
    name="app.tasks.waitlist_tasks.auto_invite_waitlist",
)
def auto_invite_waitlist() -> str:
    limit = get_settings().WAITLIST_AUTO_INVITE_DAILY_LIMIT
    if limit == 0:
        return "disabled"
    from app.core.database import SessionLocal

    session = SessionLocal()
    try:
        today = today_et()
        day_start = datetime(today.year, today.month, today.day, tzinfo=ET)
        day_end = day_start + timedelta(days=1)
        sent_before = session.scalar(
            select(func.count())
            .select_from(Invite)
            .where(Invite.letter_sent_at >= day_start, Invite.letter_sent_at < day_end)
        )
        sent_count = int(sent_before or 0)
        remaining = max(0, limit - sent_count)
        pending = session.scalars(
            select(WaitlistEntry)
            .where(WaitlistEntry.status == "pending")
            .order_by(WaitlistEntry.created_at, WaitlistEntry.id)
        ).all()
        queued = [entry.email for entry in pending if _eligible(session, entry)]
        attempts = 0
        sent: list[tuple[str, LetterLanguage]] = []
        failed: list[str] = []
        for email in queued:
            if attempts >= remaining:
                break
            try:
                result = send_letter(
                    session,
                    email,
                    language=None,
                    expires_days=14,
                    created_by=UUID(get_settings().ADMIN_ID),
                    require_pending=True,
                )
            except LetterSendFailed:
                attempts += 1
                failed.append(email)
            except LetterConflict as exc:
                session.rollback()
                logger.warning(
                    "waitlist automatic invitation skipped for %s: %s", email, exc.detail
                )
            except Exception:
                session.rollback()
                logger.exception("waitlist automatic invitation failed for %s", email)
                attempts += 1
                failed.append(email)
            else:
                attempts += 1
                sent.append((email, result.language))
        waiting = sum(
            1
            for entry in session.scalars(
                select(WaitlistEntry).where(WaitlistEntry.status == "pending")
            ).all()
            if _eligible(session, entry)
        )
        watched = list(
            session.scalars(
                select(WaitlistEntry)
                .where(WaitlistEntry.status.in_(("pending", "invited")))
                .order_by(WaitlistEntry.created_at, WaitlistEntry.id)
            ).all()
        )
        states = views(session, watched)
        latest: dict[UUID, Invite] = {}
        entry_ids = [entry.id for entry in watched]
        if entry_ids:
            invites = session.scalars(
                select(Invite)
                .where(Invite.waitlist_entry_id.in_(entry_ids))
                .order_by(Invite.created_at, Invite.id)
            ).all()
            for invite in invites:
                if invite.waitlist_entry_id is not None:
                    latest[invite.waitlist_entry_id] = invite
        attention: list[tuple[str, list[str]]] = []
        for entry, state in zip(watched, states, strict=True):
            stage = state["stage"]
            if stage == "registered" or stage == "activated":
                continue
            reasons: list[str] = []
            if entry.status == "pending" and signup_email_taken(session, entry.email):
                reasons.append("already a registered user")
            if entry.status == "pending" and _unsubscribed(session, entry.email):
                reasons.append("unsubscribed from invitation letters")
            if entry.status == "invited" and entry.link_sent_at is None:
                reasons.append("link generated but not recorded as sent")
            if entry.status == "invited" and state["link_expired"] is True:
                reasons.append("link expired without signup")
            newest = latest.get(entry.id)
            event = newest.letter_delivery_event if newest is not None else None
            if entry.status == "invited" and event in UNDELIVERABLE_EVENTS:
                reasons.append(f"last invitation letter {event}")
            if reasons:
                attention.append((entry.email, reasons))
        if attempts == 0 and not attention:
            return "nothing_to_report"
        noun = "letter" if len(sent) == 1 else "letters"
        subject = f"Portfonia waitlist: {len(sent)} invitation {noun} sent on {today.isoformat()}"
        letter_lines = [f"- {email} ({_LANGUAGE_NAMES[language]})" for email, language in sent]
        failed_lines = [f"- {email}" for email in failed]
        attention_lines = [f"- {email}: {'; '.join(reasons)}" for email, reasons in attention]
        body = (
            f"Daily waitlist invitations for {today.isoformat()} (ET).\n"
            "\n"
            f"Sent by this run: {len(sent)}\n"
            f"Sent earlier today (before this run): {sent_count}\n"
            f"Daily limit: {limit}\n"
            "Failed this run (nothing was saved; retried on a later run): "
            f"{len(failed)}\n"
            f"Still waiting on the waitlist: {waiting}\n"
            "\n"
            "Letters sent:\n"
            f"{_section(letter_lines)}\n"
            "\n"
            "Failed this run:\n"
            f"{_section(failed_lines)}\n"
            "\n"
            "Needs your attention:\n"
            f"{_section(attention_lines)}"
        )
        send_ops_alert(
            subject,
            body,
            idempotency_key=f"waitlist-auto-invite:{today.isoformat()}",
            severity="INFO",
        )
        return "reported"
    finally:
        session.close()
