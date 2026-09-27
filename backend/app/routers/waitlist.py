"""Public waitlist submission (issue #566)."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.database import get_session
from app.core.rate_limit import rate_limit_waitlist
from app.core.timezones import ET
from app.models.waitlist_entry import WaitlistEntry
from app.services.altcha_challenge import create_waitlist_challenge, verify_waitlist_solution
from app.services.invites import signup_email_taken
from app.services.waitlist import view
from app.tasks.admin_tasks import send_admin_alert_task

logger = logging.getLogger(__name__)
router = APIRouter()

LANGUAGE_NAMES = {
    "en": "English",
    "zh-Hans": "Simplified Chinese",
    "zh-Hant": "Traditional Chinese",
}
STATUS_SENTENCES = {
    "pending": "not handled yet.",
    "invited": "an invite link was generated but not yet marked as sent.",
    "sent": "an invite link was sent.",
    "registered": "already registered an account.",
    "activated": "already registered and verified their email.",
    "rejected": "rejected earlier. It stays rejected; nothing was changed automatically.",
}
EXISTING_ACCOUNT_NOTE = (
    "Note: this email already belongs to a registered Portfonia account, "
    "so an invite link cannot be generated for it."
)


class WaitlistRequest(BaseModel):
    email: str
    locale: Literal["en", "zh-Hans", "zh-Hant"]
    altcha: str


class WaitlistResponse(BaseModel):
    received: bool


@router.get("/altcha-challenge")
def altcha_challenge() -> dict[str, object]:
    return create_waitlist_challenge()


def _et_time(value: datetime) -> str:
    return value.astimezone(ET).strftime("%Y-%m-%d %H:%M ET")


def _notice(
    entry: WaitlistEntry, *, kind: str, existing_user: bool, state: dict[str, object]
) -> None:
    if kind == "new":
        subject = f"Portfonia waitlist: new request from {entry.email}"
        body = (
            "Someone joined the waitlist.\n\n"
            f"Email: {entry.email}\n"
            f"Language: {LANGUAGE_NAMES[entry.locale]}\n"
            f"Submitted: {_et_time(entry.created_at)}\n\n"
            "Nothing has been done with this request yet."
        )
    else:
        subject = f"Portfonia waitlist: {entry.email} asked again"
        sentence = STATUS_SENTENCES[str(state["stage"])]
        if state["stage"] in ("invited", "sent") and state["link_expired"]:
            sentence += " The link has expired."
        body = (
            "This email is already on the waitlist and was submitted again.\n\n"
            f"Email: {entry.email}\n"
            f"Language: {LANGUAGE_NAMES[entry.locale]}\n"
            f"First joined: {_et_time(entry.created_at)}\n"
            f"Current status: {sentence}"
        )
    if existing_user:
        body += f"\n\n{EXISTING_ACCOUNT_NOTE}"
    try:
        send_admin_alert_task.delay(subject, body, severity="INFO")
    except Exception:
        logger.exception("waitlist: failed to enqueue admin notice")


@router.post("", response_model=WaitlistResponse)
def submit(
    req: WaitlistRequest,
    request: Request,
    session: Session = Depends(get_session),
) -> WaitlistResponse:
    email = req.email.strip().lower()
    if not email:
        raise HTTPException(status_code=422, detail="email is required")
    if not verify_waitlist_solution(req.altcha):
        raise HTTPException(status_code=400, detail="invalid captcha")
    rate_limit_waitlist(request, email)
    inserted_id = session.scalar(
        insert(WaitlistEntry)
        .values(email=email, locale=req.locale)
        .on_conflict_do_nothing(index_elements=[WaitlistEntry.email])
        .returning(WaitlistEntry.id)
    )
    kind = "new" if inserted_id is not None else "repeat"
    entry = (
        session.get(WaitlistEntry, inserted_id)
        if inserted_id is not None
        else session.scalar(select(WaitlistEntry).where(WaitlistEntry.email == email))
    )
    assert entry is not None
    existing_user = signup_email_taken(session, email)
    state = view(session, entry)
    session.commit()
    _notice(entry, kind=kind, existing_user=existing_user, state=state)
    return WaitlistResponse(received=True)
