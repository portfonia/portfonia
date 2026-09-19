"""Link tokens (issue #458, Vigil R0 P2.3).

Link tokens are 32 random bytes, base64url 43 chars; only SHA-256 hex is
stored. Single-use is marked on `VigilActionToken.confirmed_at` itself, in
the same DB transaction as the confirm side effects (#528, #516 finding
14) — no separate nonce table.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.vigil import VigilActionToken

LINK_TOKEN_BYTES = 32
DRILL_TTL = timedelta(hours=48)


def db_now(session: Session) -> datetime:
    """PostgreSQL `clock_timestamp()` after the caller has taken locks."""
    now = session.scalar(select(func.clock_timestamp()))
    if now is None:
        raise RuntimeError("clock_timestamp() returned null")
    current = cast(datetime, now)
    if current.tzinfo is None:
        return current.replace(tzinfo=UTC)
    return current.astimezone(UTC)


def hash_link_token(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def mint_link_token() -> tuple[str, str]:
    """Return (presented token, sha256 hex). Never persist the presented form."""
    raw = secrets.token_bytes(LINK_TOKEN_BYTES)
    token = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    return token, hash_link_token(token)


def rfc3339_z(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def invalidate_open_drill_tokens(
    session: Session,
    *,
    vault_id: UUID,
    now: datetime,
    config_id: UUID | None = None,
    object_id: UUID | None = None,
) -> list[UUID]:
    """Invalidate pending drill tokens. Caller holds User-then-vault locks."""
    stmt = select(VigilActionToken).where(
        VigilActionToken.vault_id == vault_id,
        VigilActionToken.purpose == "drill",
        VigilActionToken.confirmed_at.is_(None),
        VigilActionToken.used_at.is_(None),
        VigilActionToken.invalidated_at.is_(None),
    )
    if config_id is not None:
        stmt = stmt.where(VigilActionToken.config_id == config_id)
    if object_id is not None:
        stmt = stmt.where(VigilActionToken.object_id == object_id)
    rows = list(session.scalars(stmt.order_by(VigilActionToken.id).with_for_update()).all())
    ids: list[UUID] = []
    for row in rows:
        row.invalidated_at = now
        ids.append(row.id)
    if rows:
        session.flush()
    return ids
