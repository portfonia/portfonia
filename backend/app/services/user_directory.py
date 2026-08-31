"""Resolves a user_id to the account-level facts callers need about them."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.user import User

RecipientPurpose = Literal["account_email", "delivery_email"]


def recipient_email_with_purpose(
    session: Session, user_id: UUID
) -> tuple[str, RecipientPurpose] | None:
    """Return the address a report for *user_id* should be sent to, plus
    which `users` field it came from (issue #257).

    Prefers `delivery_email` when set, else `email`. The purpose is the
    matching `email_verifications.purpose` value so an unsubscribe token
    can revoke the same field without a second user-row lookup.
    """
    user = session.get(User, user_id)
    if user is None or user.status != "active":
        return None
    if user.delivery_email:
        return user.delivery_email, "delivery_email"
    return user.email, "account_email"


def recipient_email(session: Session, user_id: UUID) -> str | None:
    """Return the email address a report for *user_id* should be sent to.

    Prefers `delivery_email` when set, else `email`. Returns `None` for a
    missing or non-active row — callers must treat that as "do not send",
    never fall back to a default address. See `send_report_email`'s
    fail-closed handling in `email_sender.py`.
    """
    resolved = recipient_email_with_purpose(session, user_id)
    return None if resolved is None else resolved[0]
