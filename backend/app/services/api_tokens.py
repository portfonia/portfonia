"""Token hashing, validity, creation and revocation (#651)."""

import hashlib
import secrets
from datetime import datetime, timedelta
from typing import Literal, cast
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from app.core.timezones import ET
from app.models.api_token import ApiToken


def now_et() -> datetime:
    return datetime.now(tz=ET)


def token_hash(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode()).hexdigest()


def token_status(token: ApiToken, now: datetime) -> str:
    if token.revoked_at is not None:
        return "revoked"
    if token.expires_at is not None and token.expires_at <= now:
        return "expired"
    if now - (token.last_used_at or token.created_at) > timedelta(days=60):
        return "expired_unused"
    return "active"


def lookup_token(session: Session, plaintext: str) -> ApiToken | None:
    return session.scalar(select(ApiToken).where(ApiToken.token_hash == token_hash(plaintext)))


def new_token(
    session: Session, user_id: UUID, name: str, expires_at: datetime | None
) -> tuple[ApiToken, str]:
    plaintext = "pfa_" + secrets.token_urlsafe(32)
    row = ApiToken(
        user_id=user_id,
        name=name,
        token_hash=token_hash(plaintext),
        token_prefix=plaintext[:12],
        created_at=now_et(),
        expires_at=expires_at,
    )
    session.add(row)
    session.flush()
    return row, plaintext


def revoke_all(session: Session, user_id: UUID, actor: Literal["user", "email_link", "ops"]) -> int:
    result = cast(
        CursorResult[tuple[()]],
        session.execute(
            update(ApiToken)
            .where(ApiToken.user_id == user_id, ApiToken.revoked_at.is_(None))
            .values(revoked_at=now_et(), revoked_by=actor)
        ),
    )
    return int(result.rowcount)
