"""Best-effort activity recording in the caller's transaction."""

import logging
from datetime import datetime
from uuid import UUID

from fastapi import Request
from sqlalchemy.orm import Session

from app.core.timezones import ET
from app.models.user_activity import UserActivity

logger = logging.getLogger(__name__)


def record(
    session: Session,
    user_id: UUID | None,
    event: str,
    detail: dict[str, object] | None = None,
    request: Request | None = None,
) -> None:
    try:
        user_agent = request.headers.get("user-agent") if request is not None else None
        session.add(
            UserActivity(
                occurred_at=datetime.now(tz=ET),
                user_id=user_id,
                event=event,
                detail=detail if detail is not None else {},
                client_ip=request.client.host if request is not None and request.client else None,
                user_agent=user_agent[:512].replace("\x00", "\ufffd")
                if user_agent is not None
                else None,
            )
        )
    except Exception:
        logger.error("Account activity recording failed")
