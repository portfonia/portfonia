"""Resolves a user_id to the account-level facts callers need about them.

B3: there is no `users` table yet, so `recipient_email` is the one place in
`app/services/**` allowed to reference `DEV_USER_ID` — a deliberate, narrow
shim that keeps the pre-B3 single-user email destination working
byte-for-byte while every caller switches to passing `user_id` explicitly.
B4 replaces the body with a `users` table lookup; the signature (and every
call site) stays unchanged (Ring 1-B design doc §5.3).
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.orm import Session

from app.core.config import get_settings


def recipient_email(session: Session, user_id: UUID) -> str | None:
    """Return the email address a report for *user_id* should be sent to.

    Returns `None` for any user_id this shim can't resolve — callers must
    treat that as "do not send", never fall back to a default address. See
    `send_report_email`'s fail-closed handling in `email_sender.py`.
    """
    settings = get_settings()
    if user_id == UUID(settings.DEV_USER_ID):
        return settings.DEV_USER_EMAIL
    return None
