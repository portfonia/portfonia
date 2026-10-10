"""Authenticated Jade tools; replay reads cached data only."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.database import get_session
from app.core.deps import Principal, current_principal
from app.models.user import User
from app.routers.portfolio import BaseCurrency, BenchmarkCode
from app.schemas.jade import JadeReplayOut, JadeStressOut, JadeStyleOut, JadeTailRiskOut
from app.services import subscription
from app.services.jade_replay import compute_replay
from app.services.jade_replay_config import DEFAULT_REPLAY_RANGE, ReplayRange
from app.services.jade_stress import compute_stress
from app.services.jade_style import compute_style
from app.services.jade_tail_risk import compute_tail_risk
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
    summary="Replay today's unchanged holdings over a selected span up to five years (Jade)",
)
def get_replay(
    base_currency: Annotated[BaseCurrency | None, Query()] = None,
    benchmark: BenchmarkCode = "sp500",
    range: ReplayRange = DEFAULT_REPLAY_RANGE,
    session: Session = Depends(get_session),
    principal: Principal = Depends(current_principal),
) -> JadeReplayOut:
    return compute_replay(
        session,
        principal.user_id,
        base_currency or report_currency_for(session, principal.user_id, "USD"),
        benchmark,
        range,
    )


@router.get(
    "/tail-risk",
    response_model=JadeTailRiskOut,
    dependencies=[Depends(_jade_access)],
    summary="Tail risk (VaR / CVaR) of today's holdings over the five-year replay (Jade)",
)
def get_tail_risk(
    base_currency: Annotated[BaseCurrency | None, Query()] = None,
    benchmark: BenchmarkCode = "sp500",
    session: Session = Depends(get_session),
    principal: Principal = Depends(current_principal),
) -> JadeTailRiskOut:
    return compute_tail_risk(
        session,
        principal.user_id,
        base_currency or report_currency_for(session, principal.user_id, "USD"),
        benchmark,
    )


@router.get(
    "/stress",
    response_model=JadeStressOut,
    dependencies=[Depends(_jade_access)],
    summary="Historical stress scenarios for today's holdings (Jade)",
)
def get_stress(
    base_currency: Annotated[BaseCurrency | None, Query()] = None,
    benchmark: BenchmarkCode = "sp500",
    session: Session = Depends(get_session),
    principal: Principal = Depends(current_principal),
) -> JadeStressOut:
    return compute_stress(
        session,
        principal.user_id,
        base_currency or report_currency_for(session, principal.user_id, "USD"),
        benchmark,
    )


@router.get(
    "/style",
    response_model=JadeStyleOut,
    dependencies=[Depends(_jade_access)],
    summary="Returns-based style exposure of today's holdings over three months (Jade)",
)
def get_style(
    base_currency: Annotated[BaseCurrency | None, Query()] = None,
    benchmark: BenchmarkCode = "sp500",
    session: Session = Depends(get_session),
    principal: Principal = Depends(current_principal),
) -> JadeStyleOut:
    return compute_style(
        session,
        principal.user_id,
        base_currency or report_currency_for(session, principal.user_id, "USD"),
        benchmark,
    )
