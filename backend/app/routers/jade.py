"""Authenticated Jade tools; replay reads cached data only."""

from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.database import get_session
from app.core.deps import Principal, current_principal
from app.core.rate_limit import check_jade_what_if_cooldown
from app.models.user import User
from app.routers.portfolio import BaseCurrency, BenchmarkCode
from app.schemas.jade import (
    JadeBenchmarkIn,
    JadeBenchmarkOut,
    JadeReplayOut,
    JadeStressOut,
    JadeStyleOut,
    JadeTailRiskOut,
    JadeTrackingOut,
    JadeWhatIfIn,
    JadeWhatIfOut,
    WhatIfColumn,
)
from app.services import subscription
from app.services.jade_replay import build_replay, compute_replay
from app.services.jade_replay_config import DEFAULT_REPLAY_RANGE, RebalanceRule, ReplayRange
from app.services.jade_stress import compute_stress
from app.services.jade_style import compute_style
from app.services.jade_tail_risk import compute_tail_risk
from app.services.jade_tracking import compute_tracking
from app.services.jade_what_if import validate_versions, what_if_holdings
from app.services.portfolio_calculator import HoldingValue, compute_portfolio
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


@router.put(
    "/benchmark",
    response_model=JadeBenchmarkOut,
    dependencies=[Depends(_jade_access)],
    summary="Save the caller's custom reference portfolio (Jade)",
)
def save_benchmark(
    body: JadeBenchmarkIn,
    session: Session = Depends(get_session),
    principal: Principal = Depends(current_principal),
) -> JadeBenchmarkOut:
    user = session.get(User, principal.user_id)
    assert user is not None
    user.jade_benchmark = {"components": [c.model_dump() for c in body.components]}
    session.commit()
    return JadeBenchmarkOut(components=body.components, saved=True)


@router.get(
    "/tracking",
    response_model=JadeTrackingOut,
    dependencies=[Depends(_jade_access)],
    summary="Tracking against the caller's daily-rebalanced reference (Jade, cached data only)",
)
def get_tracking(
    base_currency: Annotated[BaseCurrency | None, Query()] = None,
    range: ReplayRange = DEFAULT_REPLAY_RANGE,
    session: Session = Depends(get_session),
    principal: Principal = Depends(current_principal),
) -> JadeTrackingOut:
    return compute_tracking(
        session,
        principal.user_id,
        base_currency or report_currency_for(session, principal.user_id, "USD"),
        range,
    )


@router.post("/what-if", response_model=JadeWhatIfOut, dependencies=[Depends(_jade_access)])
def post_what_if(
    body: JadeWhatIfIn,
    session: Session = Depends(get_session),
    principal: Principal = Depends(current_principal),
) -> JadeWhatIfOut:
    snapshot = compute_portfolio(session, principal.user_id, body.base_currency)
    validate_versions(body, snapshot.holdings)
    remaining = check_jade_what_if_cooldown(str(principal.user_id))
    if remaining is not None:
        raise HTTPException(429, detail="what_if_cooldown", headers={"Retry-After": str(remaining)})
    columns = []
    inputs: list[tuple[Literal["current", "a", "b"], RebalanceRule, list[HoldingValue]]] = [
        ("current", "none", snapshot.holdings)
    ]
    version_keys: tuple[Literal["a", "b"], ...] = ("a", "b")
    for version_key, version in zip(version_keys, body.versions, strict=False):
        inputs.append(
            (
                version_key,
                version.rule,
                what_if_holdings(snapshot.holdings, version.rows, body.base_currency),
            )
        )
    for key, rule, holdings in inputs:
        columns.append(
            WhatIfColumn(
                key=key,
                rule=rule,
                total=str(
                    sum((h.market_value_base or Decimal(0) for h in holdings), Decimal(0)).quantize(
                        Decimal(".01")
                    )
                ),
                replay=build_replay(
                    session,
                    principal.user_id,
                    body.base_currency,
                    "sp500",
                    body.range,
                    holdings=holdings,
                    rule=rule,
                ).out,
                tail_risk=compute_tail_risk(
                    session,
                    principal.user_id,
                    body.base_currency,
                    "sp500",
                    holdings=holdings,
                    rule=rule,
                ),
                stress=compute_stress(
                    session,
                    principal.user_id,
                    body.base_currency,
                    "sp500",
                    holdings=holdings,
                    rule=rule,
                ),
            )
        )
    return JadeWhatIfOut(base_currency=body.base_currency, range=body.range, columns=columns)
