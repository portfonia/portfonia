"""Shared invitation-letter send path for the Ops endpoint and the daily task."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.timezones import ET
from app.models.invite import Invite
from app.models.waitlist_entry import WaitlistEntry
from app.services.email_sender import send_invitation_letter
from app.services.invitation_unsubscribe import create_token as create_invitation_unsubscribe_token
from app.services.invites import EmailAlreadyRegistered, create_invite, signup_email_taken
from app.services.waitlist import view as waitlist_view
from app.tasks.email_verification_tasks import POLL_DELAY_SECONDS
from app.tasks.invitation_letter_tasks import poll_invitation_letter_delivery

logger = logging.getLogger(__name__)

LetterLanguage = Literal["en", "zh", "zh-Hant"]


class LetterConflict(Exception):
    """detail strings are exactly the endpoint's current 409 details."""

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)


class LetterSendFailed(Exception):
    """Resend returned no provider id; the committed invite stays unsent."""

    def __init__(self, invite_id: UUID) -> None:
        self.invite_id = invite_id
        super().__init__(str(invite_id))


@dataclass(frozen=True)
class LetterResult:
    invite_id: UUID
    email: str
    language: LetterLanguage
    waitlist_entry_id: UUID | None
    expires_at: datetime
    invite_url: str
    letter_sent_at: datetime
    provider_message_id: str


def _revoke_waitlist_links(session: Session, entry_id: UUID, now: datetime) -> None:
    session.execute(
        update(Invite)
        .where(
            Invite.waitlist_entry_id == entry_id,
            Invite.used_at.is_(None),
            Invite.revoked_at.is_(None),
            Invite.expires_at > now,
        )
        .values(revoked_at=now)
    )


def send_letter(
    session: Session,
    email: str,
    *,
    language: LetterLanguage | None,
    expires_days: int,
) -> LetterResult:
    email_n = email.strip().lower()
    if (
        session.scalar(
            select(Invite.id)
            .where(Invite.email == email_n, Invite.letter_unsubscribed_at.is_not(None))
            .limit(1)
        )
        is not None
    ):
        raise LetterConflict("recipient unsubscribed from invitation letters")
    if signup_email_taken(session, email_n):
        raise LetterConflict("email already belongs to an existing user")
    entry = session.scalar(
        select(WaitlistEntry).where(WaitlistEntry.email == email_n).with_for_update()
    )
    now = datetime.now(tz=ET)
    resolved: LetterLanguage = language or "en"
    language_locales: dict[str, tuple[LetterLanguage, str]] = {
        "en": ("en", "en"),
        "zh": ("zh", "zh-Hans"),
        "zh-Hant": ("zh-Hant", "zh-Hant"),
    }
    resolved, ui_locale = language_locales[resolved]
    if entry is not None:
        state = waitlist_view(session, entry)
        if state["stage"] in ("registered", "activated"):
            raise LetterConflict("entry already registered")
        if entry.status == "rejected":
            raise LetterConflict("entry rejected")
        _revoke_waitlist_links(session, entry.id, now)
        waitlist_languages: dict[str, LetterLanguage] = {
            "en": "en",
            "zh-Hans": "zh",
            "zh-Hant": "zh-Hant",
        }
        resolved, ui_locale = language_locales[waitlist_languages.get(entry.locale, "en")]
    try:
        # Function-local import: admin imports this module.
        from app.routers.admin import ops_invite_created_by

        issued = create_invite(
            session,
            created_by=ops_invite_created_by(),
            email=email_n,
            expires_days=expires_days,
            waitlist_entry_id=entry.id if entry else None,
        )
    except EmailAlreadyRegistered:
        session.rollback()
        raise LetterConflict("email already belongs to an existing user") from None
    if entry is not None:
        entry.status = "invited"
        entry.link_sent_at = None
        entry.status_changed_at = now
    session.commit()
    settings = get_settings()
    invite_url = f"{settings.FRONTEND_URL}/signup?invite={issued.token}&lang={ui_locale}"
    unsubscribe_token = create_invitation_unsubscribe_token(issued.id, resolved)
    unsubscribe_url = (
        f"{settings.FRONTEND_URL}/api/invitation-letters/unsubscribe?token={unsubscribe_token}"
    )
    provider_id = send_invitation_letter(
        email_n,
        invite_url,
        unsubscribe_url,
        locale=resolved,
        idempotency_key=f"invitation-letter:{issued.id}",
    )
    if provider_id is None:
        logger.error("invitation letter send failed for invite %s", issued.id)
        raise LetterSendFailed(issued.id)
    sent_at = datetime.now(tz=ET)
    try:
        invite = session.get(Invite, issued.id)
        assert invite is not None
        if entry is not None:
            current_entry = session.scalar(
                select(WaitlistEntry)
                .where(WaitlistEntry.id == entry.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            session.refresh(invite)
            if (
                current_entry is not None
                and current_entry.status == "invited"
                and invite.revoked_at is None
            ):
                current_entry.link_sent_at = sent_at
            else:
                logger.warning(
                    "invitation letter %s sent after waitlist entry changed; link_sent_at unchanged",
                    issued.id,
                )
        invite.letter_sent_at = sent_at
        invite.letter_provider_message_id = provider_id
        session.commit()
    except Exception:
        logger.exception("letter sent, record not persisted: provider id %s", provider_id)
        raise
    try:
        poll_invitation_letter_delivery.apply_async(
            args=[str(issued.id)], countdown=POLL_DELAY_SECONDS
        )
    except Exception:
        logger.exception("invitation letter poll enqueue failed for %s", issued.id)
    return LetterResult(
        invite_id=issued.id,
        email=email_n,
        language=resolved,
        waitlist_entry_id=entry.id if entry else None,
        expires_at=issued.expires_at,
        invite_url=invite_url,
        letter_sent_at=sent_at,
        provider_message_id=provider_id,
    )
