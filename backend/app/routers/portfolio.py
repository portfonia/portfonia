from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.database import get_session
from app.schemas.portfolio import (
    ConcentrationOut,
    HoldingValueOut,
    PortfolioSummaryResponse,
)
from app.services import fx_fetcher, price_fetcher
from app.services.fund_nav_fetcher import update_fund_navs
from app.services.portfolio_calculator import compute_portfolio

router = APIRouter()

BaseCurrency = Literal["USD", "HKD", "CNY"]


class RefreshResult(BaseModel):
    prices_updated: int
    prices_failed: list[str]
    sectors_backfilled: int
    funds_updated: int
    funds_failed: list[str]
    fx_upserted: int
    fx_failed: list[str]


@router.post("/refresh", response_model=RefreshResult)
def refresh_market_data(session: Session = Depends(get_session)) -> RefreshResult:
    """Manually trigger price / sector / NAV / FX refresh (Ring 0 entry point).

    Until Celery (stage H) schedules this, it is the only way to pull fresh
    market data into the holdings + fx_rates tables.
    """
    prices = price_fetcher.update_holding_prices(session)
    sectors = price_fetcher.backfill_sectors(session)
    funds = update_fund_navs(session)
    fx = fx_fetcher.update_fx_rates(session)
    session.commit()
    return RefreshResult(
        prices_updated=prices.updated,
        prices_failed=prices.failed,
        sectors_backfilled=sectors,
        funds_updated=funds.updated,
        funds_failed=funds.failed,
        fx_upserted=fx.upserted,
        fx_failed=fx.failed,
    )


@router.get("/summary", response_model=PortfolioSummaryResponse)
def get_portfolio_summary(
    base_currency: Annotated[BaseCurrency, Query()] = "USD",
    session: Session = Depends(get_session),
) -> PortfolioSummaryResponse:
    snap = compute_portfolio(session, base_currency=base_currency)

    holdings_out = [
        HoldingValueOut(
            holding_id=hv.holding_id,
            name=hv.name,
            ticker=hv.ticker,
            fund_code=hv.fund_code,
            currency=hv.currency,
            asset_type=hv.asset_type,
            sector=hv.sector,
            market=hv.market,
            market_value=hv.market_value,
            market_value_base=hv.market_value_base,
            price_as_of=hv.price_as_of,
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
        concentration=ConcentrationOut.model_validate(snap.concentration),
        stale_tickers=snap.stale_tickers,
        holdings=holdings_out,
    )
