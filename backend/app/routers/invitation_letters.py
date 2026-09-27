"""Public confirmation and one-click unsubscribe for invitation letters."""

from __future__ import annotations

from datetime import datetime
from html import escape

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse
from sqlalchemy import update
from sqlalchemy.orm import Session

from app.core.database import get_session
from app.core.timezones import ET
from app.models.invite import Invite
from app.services.invitation_unsubscribe import verify_token

router = APIRouter()

_COPY = {
    "en": {
        "confirm": "Unsubscribe from Portfonia invitation emails?",
        "button": "Unsubscribe",
        "success": "You have been unsubscribed from Portfonia invitation emails.",
        "invalid": "This unsubscribe link is invalid.",
    },
    "zh": {
        "confirm": "确定退订 Portfonia 邀请邮件吗？",  # noqa: RUF001
        "button": "退订",
        "success": "您已退订 Portfonia 邀请邮件。",
        "invalid": "此退订链接无效。",
    },
}


def _page(message: str, *, button: str | None = None, status_code: int = 200) -> HTMLResponse:
    form = (
        f'<form method="post"><button type="submit">{escape(button)}</button></form>'
        if button
        else ""
    )
    return HTMLResponse(
        f'<html><body style="font-family:sans-serif;max-width:36rem;margin:4rem auto">'
        f"<p>{escape(message)}</p>{form}</body></html>",
        status_code=status_code,
    )


def _invalid() -> HTMLResponse:
    return _page(_COPY["en"]["invalid"], status_code=400)


@router.get("/unsubscribe", response_class=HTMLResponse)
def unsubscribe_confirm(token: str = "") -> HTMLResponse:
    claims = verify_token(token)
    if claims is None:
        return _invalid()
    return _page(_COPY[claims[1]]["confirm"], button=_COPY[claims[1]]["button"])


@router.post("/unsubscribe", response_class=HTMLResponse)
def unsubscribe(token: str = "", session: Session = Depends(get_session)) -> HTMLResponse:
    claims = verify_token(token)
    if claims is None:
        return _invalid()
    invite_id, locale = claims
    if session.get(Invite, invite_id) is None:
        return _invalid()
    session.execute(
        update(Invite)
        .where(Invite.id == invite_id, Invite.letter_unsubscribed_at.is_(None))
        .values(letter_unsubscribed_at=datetime.now(tz=ET))
    )
    session.commit()
    return _page(_COPY[locale]["success"])
