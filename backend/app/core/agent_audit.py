"""HTTP audit middleware, scoped to the agent namespace only."""

from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import cast

from fastapi import Request, Response
from sqlalchemy import delete, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.core.deps import _bearer_token
from app.models.api_audit_log import ApiAuditLog
from app.services import api_tokens


def _record(request: Request, status_code: int) -> None:
    from app.core.database import SessionLocal

    plaintext = _bearer_token(request.headers.get("authorization"))
    with SessionLocal() as session:
        user_id = getattr(request.state, "agent_user_id", None)
        token_id = getattr(request.state, "agent_token_id", None)
        if (
            not getattr(request.state, "agent_identity_checked", False)
            and plaintext
            and plaintext.startswith("pfa_")
        ):
            token = api_tokens.lookup_token(session, plaintext)
            if token is not None:
                user_id, token_id = token.user_id, token.id
        route = request.scope.get("route")
        endpoint = route.path if route is not None else request.url.path[:200]
        user_agent = request.headers.get("user-agent")
        session.add(
            ApiAuditLog(
                occurred_at=api_tokens.now_et(),
                user_id=user_id,
                token_id=token_id,
                token_prefix=plaintext[:12] if plaintext else None,
                endpoint=endpoint,
                params={
                    key: request.query_params[key]
                    for key in ("start", "end")
                    if key in request.query_params
                },
                status_code=status_code,
                item_count=getattr(request.state, "agent_item_count", None),
                client_ip=request.client.host if request.client else "unknown",
                user_agent=user_agent[:512] if user_agent is not None else None,
            )
        )
        session.commit()


async def audit_agent_request(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    if not request.url.path.startswith("/agent/v1"):
        return await call_next(request)
    try:
        response = await call_next(request)
    except Exception:
        await run_in_threadpool(_record, request, 500)
        raise
    await run_in_threadpool(_record, request, response.status_code)
    return response


def cleanup_api_audit(session: Session) -> int:
    cutoff = api_tokens.now_et() - timedelta(days=90)
    total = 0
    while True:
        ids = session.scalars(
            select(ApiAuditLog.id).where(ApiAuditLog.occurred_at < cutoff).limit(1000)
        ).all()
        if not ids:
            return total
        result = cast(
            CursorResult[tuple[()]],
            session.execute(delete(ApiAuditLog).where(ApiAuditLog.id.in_(ids))),
        )
        total += int(result.rowcount)
        session.commit()
