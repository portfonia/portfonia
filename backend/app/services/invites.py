"""Invite issuance and atomic redeem (Ring 1-B design.md §6.4)."""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from app.models.invite import Invite

INVITE_REJECTED_MESSAGE = "invalid invite"
_DEFAULT_TTL = timedelta(days=14)


class InviteRejected(Exception):
    """Invite cannot be used. Message is always INVITE_REJECTED_MESSAGE —
    callers must not distinguish missing / used / expired / mismatched."""


@dataclass(frozen=True)
class IssuedInvite:
    id: uuid.UUID
    token: str
    email: str | None
    expires_at: datetime
    created_at: datetime


def hash_invite_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _normalize_email(email: str | None) -> str | None:
    if email is None:
        return None
    stripped = email.strip().lower()
    return stripped or None


def create_invite(
    session: Session,
    *,
    created_by: uuid.UUID,
    email: str | None = None,
    expires_at: datetime | None = None,
    expires_days: int = 14,
) -> IssuedInvite:
    token = secrets.token_urlsafe(24)
    created_at = datetime.now(tz=UTC)
    if expires_at is None:
        expires_at = created_at + timedelta(days=expires_days)
    row = Invite(
        token_hash=hash_invite_token(token),
        email=_normalize_email(email),
        created_by=created_by,
        expires_at=expires_at,
        created_at=created_at,
    )
    session.add(row)
    session.flush()
    return IssuedInvite(
        id=row.id,
        token=token,
        email=row.email,
        expires_at=row.expires_at,
        created_at=row.created_at,
    )


def redeem_invite(session: Session, token: str, *, used_by: uuid.UUID, email: str) -> uuid.UUID:
    """Atomically consume an unused, unrevoked, unexpired invite.

    The `used_at IS NULL` predicate lives in the UPDATE WHERE, not in a
    prior SELECT — two concurrent redeemers cannot both succeed.
    """
    email_n = _normalize_email(email)
    stmt = (
        update(Invite)
        .where(
            Invite.token_hash == hash_invite_token(token),
            Invite.used_at.is_(None),
            Invite.revoked_at.is_(None),
            Invite.expires_at > func.now(),
            or_(Invite.email.is_(None), Invite.email == email_n),
        )
        .values(used_at=func.now(), used_by_user_id=used_by)
        .returning(Invite.id)
    )
    invite_id = session.execute(stmt).scalar_one_or_none()
    if invite_id is None:
        raise InviteRejected(INVITE_REJECTED_MESSAGE)
    return invite_id


def revoke_invite(session: Session, invite_id: uuid.UUID) -> None:
    row = session.get(Invite, invite_id)
    if row is None:
        raise LookupError("invite not found")
    if row.revoked_at is None:
        row.revoked_at = datetime.now(tz=UTC)
        session.flush()


def list_invites(session: Session) -> list[Invite]:
    return list(session.execute(select(Invite).order_by(Invite.created_at.desc())).scalars().all())
