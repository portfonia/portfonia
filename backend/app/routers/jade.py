"""Authenticated Jade tools; replay reads cached data only."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.database import get_session
from app.core.deps import Principal, current_principal
from app.models.user import User
from app.routers.portfolio import BaseCurrency, BenchmarkCode
from app.schemas.jade import JadeReplayOut
from app.services import subscription
from app.services.jade_replay import compute_replay
from app.services.user_scope import report_currency_for

router = APIRouter()


def _jade_access(
    principal: Principal = Depends(current_principal), session: Session = Depends(get_session)
) -> None:
    user = session.get(User, principal.user_id)
    if user is None or not subscription.is_jade(user):
        raise HTTPException(status_code=403, detail="subscription_required")


@router.get(
    "/replay",
    response_model=JadeReplayOut,
    dependencies=[Depends(_jade_access)],
    summary="Replay today's unchanged holdings over five years (Jade)",
)
def get_replay(
    base_currency: Annotated[BaseCurrency | None, Query()] = None,
    benchmark: BenchmarkCode = "sp500",
    session: Session = Depends(get_session),
    principal: Principal = Depends(current_principal),
) -> JadeReplayOut:
    return compute_replay(
        session,
        principal.user_id,
        base_currency or report_currency_for(session, principal.user_id, "USD"),
        benchmark,
    )
