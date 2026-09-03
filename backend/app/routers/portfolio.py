from __future__ import annotations

import logging
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.database import get_session
from app.core.deps import Principal, current_principal
from app.core.rate_limit import (
    check_portfolio_overview_cooldown,
    release_portfolio_overview_cooldown,
)
from app.schemas.portfolio import (
    ConcentrationOut,
    HoldingValueOut,
    PortfolioSummaryResponse,
    SendOverviewResponse,
)
from app.services.portfolio_calculator import compute_portfolio
from app.tasks.notification_tasks import send_portfolio_overview_email_task

router = APIRouter()
logger = logging.getLogger(__name__)

# Mirrors app/schemas/holdings.py's VALID_CURRENCIES (15 entries) — kept as an
# explicit Literal, not built dynamically from the frozenset, so mypy --strict
# can still verify it; a drift test in test_portfolio_router.py pins the two
# together (issue #320: widened from a 3-value Literal that predated
# issue #204's FX-pair coverage of the full VALID_CURRENCIES set).
BaseCurrency = Literal[
    "USD",
    "CNY",
    "CNH",
    "HKD",
    "GBP",
    "EUR",
    "JPY",
    "SGD",
    "AUD",
    "CAD",
    "CHF",
    "KRW",
    "TWD",
    "MOP",
    "NZD",
]


@router.get("/summary", response_model=PortfolioSummaryResponse)
def get_portfolio_summary(
    base_currency: Annotated[BaseCurrency, Query()] = "USD",
    session: Session = Depends(get_session),
    principal: Principal = Depends(current_principal),
) -> PortfolioSummaryResponse:
    snap = compute_portfolio(session, user_id=principal.user_id, base_currency=base_currency)

    holdings_out = [
        HoldingValueOut(
            holding_id=hv.holding_id,
            name=hv.name,
            ticker=hv.ticker,
            fund_code=hv.fund_code,
            currency=hv.currency,
            asset_type=hv.asset_type,
            asset_class=hv.asset_class,
            sector=hv.sector,
            market=hv.market,
            market_value=hv.market_value,
            market_value_base=hv.market_value_base,
            price_as_of=hv.price_as_of,
            pricing_mode=hv.pricing_mode,
            capture_supported=hv.capture_supported,
            broker=hv.broker,
            account=hv.account,
            portfolio=hv.portfolio,
            avg_cost=hv.avg_cost,
            shares=hv.shares,
            notes=hv.notes,
            cost_basis_base=hv.cost_basis_base,
            unrealized_pnl_base=hv.unrealized_pnl_base,
            unrealized_pnl_pct=hv.unrealized_pnl_pct,
        )
        for hv in snap.holdings
    ]

    return PortfolioSummaryResponse(
        base_currency=snap.base_currency,
        fx_date=snap.fx_date,
        total_base=snap.total_base,
        by_market=snap.by_market,
        by_currency=snap.by_currency,
        by_asset_type=snap.by_asset_type,
        by_sector=snap.by_sector,
        by_asset_class=snap.by_asset_class,
        by_group=snap.by_group,
        by_broker=snap.by_broker,
        by_account=snap.by_account,
        total_cost_basis_base=snap.total_cost_basis_base,
        total_unrealized_pnl_base=snap.total_unrealized_pnl_base,
        total_unrealized_pnl_pct=snap.total_unrealized_pnl_pct,
        price_as_of_date=snap.price_as_of_date,
        concentration=ConcentrationOut.model_validate(snap.concentration),
        stale_tickers=snap.stale_tickers,
        holdings=holdings_out,
    )


@router.post("/send-overview", response_model=SendOverviewResponse)
def send_portfolio_overview(
    base_currency: Annotated[BaseCurrency, Query()] = "USD",
    principal: Principal = Depends(current_principal),
) -> SendOverviewResponse:
    """Explicit, user-clicked "Send holdings overview" (issue #202) — NOT a
    formal report: no `reports` row, no LLM call. `base_currency` mirrors
    whatever the /portfolio page has selected when the button is clicked, so
    the emailed total matches what's on screen.

    The 15-minute cooldown is claimed synchronously here (so the response is
    immediate either way) before the actual send is dispatched fire-and-
    forget — a broker blip after the claim must not turn a successful click
    into a 500, so `.delay()` failures are logged, not raised, same pattern
    as `_enqueue_confirm_capture` in routers/holdings.py. If the enqueue
    itself fails, the claim is released (review 5100733033 leftover): the
    send never happened, so the user must not be locked out of retrying for
    the full 15 minutes over a message that was never even queued.
    """
    user_id = str(principal.user_id)
    remaining = check_portfolio_overview_cooldown(user_id)
    if remaining is not None:
        return SendOverviewResponse(sent=False, retry_after_seconds=remaining)
    try:
        send_portfolio_overview_email_task.delay(user_id, base_currency)
    except Exception:
        logger.exception("send_portfolio_overview: user_id=%s failed to enqueue send", user_id)
        release_portfolio_overview_cooldown(user_id)
        return SendOverviewResponse(sent=False, retry_after_seconds=None)
    return SendOverviewResponse(sent=True)
