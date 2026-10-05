"""Read-only API for a user's own agent (#651)."""

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app.core.agent_auth import AgentPrincipal, agent_principal
from app.core.agent_limits import enforce_agent_limits
from app.core.database import get_session
from app.core.rate_limit import UNAVAILABLE_DETAIL, RateLimitUnavailable, get_backend
from app.schemas.portfolio import SnapshotExportOut
from app.services import api_tokens
from app.services.portfolio_snapshot_export import export_snapshot_range
from app.services.subscription import is_advanced
from app.tasks.notification_tasks import send_api_access_notice_task


def agent_limits(request: Request, principal: AgentPrincipal = Depends(agent_principal)) -> None:
    enforce_agent_limits(str(principal.user_id), request.scope["route"].path, api_tokens.now_et())


def snapshot_access(
    principal: AgentPrincipal = Depends(agent_principal), _limits: None = Depends(agent_limits)
) -> None:
    if not is_advanced(principal.user):
        raise HTTPException(403, "subscription_required")


router = APIRouter(dependencies=[Depends(agent_limits)])


@router.get(
    "/snapshots",
    response_model=SnapshotExportOut,
    dependencies=[Depends(snapshot_access)],
    summary="Read your complete daily holding snapshots (Advanced)",
)
def snapshots(
    request: Request,
    start: Annotated[date, Query()],
    end: Annotated[date, Query()],
    principal: AgentPrincipal = Depends(agent_principal),
    session: Session = Depends(get_session),
) -> SnapshotExportOut:
    result = export_snapshot_range(session, principal.user_id, start, end)
    now = api_tokens.now_et()
    try:
        claimed = get_backend().set_nx(
            f"agent:notice:{principal.user_id}:{now.date().isoformat()}", 129600
        )
    except RateLimitUnavailable:
        raise HTTPException(503, UNAVAILABLE_DETAIL) from None
    if claimed:
        send_api_access_notice_task.delay(str(principal.user_id))
    request.state.agent_item_count = len(result.days)
    return result
