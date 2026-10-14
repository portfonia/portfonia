"""24-hour replay persistence; callers hold the user's shared writer lock."""

import hashlib
import json
import re
from datetime import UTC, timedelta
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.agent_idempotency import AgentIdempotency
from app.services import api_tokens


def key_hash(header: str | None) -> str:
    key = header.strip() if header is not None else ""
    if re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", key) is None:
        raise HTTPException(
            422,
            {
                "code": "invalid_idempotency_key",
                "message": "Supply a 1-128 character ASCII Idempotency-Key.",
            },
        )
    return hashlib.sha256(key.encode("ascii")).hexdigest()


def canonical_hash(body: dict[str, object]) -> str:
    # Pydantic's JSON-mode mapping contains canonical decimals/defaults only.
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def lookup(session: Session, uid: UUID, operation: str, hashed_key: str) -> AgentIdempotency | None:
    record = session.scalar(
        select(AgentIdempotency).where(
            AgentIdempotency.user_id == uid,
            AgentIdempotency.operation == operation,
            AgentIdempotency.key_hash == hashed_key,
        )
    )
    if record is not None and api_tokens.now_et().astimezone(UTC) >= record.expires_at.astimezone(
        UTC
    ):
        session.delete(record)
        session.flush()
        return None
    return record


def save(
    session: Session,
    uid: UUID,
    operation: str,
    hashed_key: str,
    request_hash: str,
    status: int,
    body: dict[str, object],
) -> None:
    now = api_tokens.now_et().astimezone(UTC)
    session.add(
        AgentIdempotency(
            user_id=uid,
            operation=operation,
            key_hash=hashed_key,
            request_hash=request_hash,
            response_status=status,
            response_json=json.dumps(body, separators=(",", ":")),
            created_at=now,
            expires_at=now + timedelta(hours=24),
        )
    )
