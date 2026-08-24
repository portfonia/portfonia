"""Resolves a user_id to the account-level facts callers need about them."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.orm import Session

from app.models.user import User


def recipient_email(session: Session, user_id: UUID) -> str | None:
    """Return the email address a report for *user_id* should be sent to.

    Prefers `delivery_email` when set, else `email`. Returns `None` for a
    missing or non-active row — callers must treat that as "do not send",
    never fall back to a default address. See `send_report_email`'s
    fail-closed handling in `email_sender.py`.
    """
    user = session.get(User, user_id)
    if user is None or user.status != "active":
        return None
    if user.delivery_email:
        return user.delivery_email
    return user.email
