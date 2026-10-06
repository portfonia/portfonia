"""Derived waitlist progress from invite redemption and email verification."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.timezones import ET
from app.models.invite import Invite
from app.models.user import User
from app.models.waitlist_entry import WaitlistEntry


def activated_at_for(verified_at: datetime | None, user: User | None) -> datetime | None:
    """Current activation policy: a verified account is activated."""
    return verified_at


def views(session: Session, entries: list[WaitlistEntry]) -> list[dict[str, object]]:
    """Load invites and users in two queries after the caller's entry query."""
    if not entries:
        return []
    entry_ids = [entry.id for entry in entries]
    invites = session.scalars(select(Invite).where(Invite.waitlist_entry_id.in_(entry_ids))).all()
    by_entry: dict[uuid.UUID, list[Invite]] = {entry_id: [] for entry_id in entry_ids}
    for invite in invites:
        if invite.waitlist_entry_id is not None:
            by_entry[invite.waitlist_entry_id].append(invite)
    used_ids = [
        invite.used_by_user_id
        for invite in invites
        if invite.used_at is not None and invite.used_by_user_id is not None
    ]
    referrer_ids = [entry.referrer_user_id for entry in entries if entry.referrer_user_id]
    users = session.scalars(select(User).where(User.id.in_(used_ids + referrer_ids))).all()
    by_user = {user.id: user for user in users}
    now = datetime.now(tz=ET)
    result: list[dict[str, object]] = []
    for entry in entries:
        linked = by_entry[entry.id]
        latest = max(linked, key=lambda item: item.created_at, default=None)
        used = next((item for item in linked if item.used_at is not None), None)
        registered_at = used.used_at if used else None
        user_id = used.used_by_user_id if used else None
        user = by_user.get(user_id) if user_id else None
        verified_at = user.email_verified_at if user else None
        activated_at = activated_at_for(verified_at, user)
        expired = bool(
            latest and used is None and (latest.revoked_at is not None or latest.expires_at <= now)
        )
        if registered_at is not None:
            stage = "activated" if activated_at is not None else "registered"
        elif entry.status == "rejected":
            stage = "rejected"
        elif entry.status == "invited":
            stage = "sent" if entry.link_sent_at is not None else "invited"
        else:
            stage = "pending"
        result.append(
            {
                "id": entry.id,
                "source": entry.source,
                "referrer_email": by_user[entry.referrer_user_id].email
                if entry.referrer_user_id in by_user
                else None,
                "email": entry.email,
                "locale": entry.locale,
                "stage": stage,
                "status": entry.status,
                "created_at": entry.created_at,
                "status_changed_at": entry.status_changed_at,
                "link_generated_at": latest.created_at if latest else None,
                "link_expires_at": latest.expires_at if latest else None,
                "link_expired": expired,
                "link_sent_at": entry.link_sent_at,
                "registered_at": registered_at,
                "verified_at": verified_at,
                "activated_at": activated_at,
                "user_id": user_id,
            }
        )
    return result


def view(session: Session, entry: WaitlistEntry) -> dict[str, object]:
    return views(session, [entry])[0]
