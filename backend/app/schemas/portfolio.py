from __future__ import annotations

import datetime as dt
import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel


class HoldingValueOut(BaseModel):
    holding_id: uuid.UUID
    name: str
    ticker: str | None
    fund_code: str | None
    currency: str
    asset_type: str | None
    asset_class: str | None
    sector: str | None
    market: str
    market_value: Decimal | None
    market_value_base: Decimal | None
    price_as_of: datetime | None
    pricing_mode: str
    capture_supported: bool
    broker: str | None
    account: str | None
    portfolio: str | None
    avg_cost: Decimal | None
    shares: Decimal | None
    notes: str | None
    cost_basis_base: Decimal | None
    unrealized_pnl_base: Decimal | None
    unrealized_pnl_pct: Decimal | None

    model_config = {"from_attributes": True}


class ConcentrationOut(BaseModel):
    top_holding_name: str | None
    top_holding_ratio: Decimal | None
    top_holding_asset_class: str | None
    top3_ratio: Decimal | None
    top_asset_class_name: str | None
    top_asset_class_ratio: Decimal | None
    single_holding_watch: bool
    single_holding_high: bool
    top3_watch: bool
    asset_class_watch: bool
    asset_class_high: bool

    model_config = {"from_attributes": True}


class PortfolioSummaryResponse(BaseModel):
    base_currency: str
    fx_rates_as_of: dict[str, date]
    total_base: Decimal
    by_market: dict[str, Decimal]
    by_currency: dict[str, Decimal]
    by_asset_type: dict[str, Decimal]
    by_sector: dict[str, Decimal]
    by_asset_class: dict[str, Decimal]
    by_group: dict[str, Decimal]
    by_broker: dict[str, Decimal]
    by_account: dict[str, Decimal]
    total_cost_basis_base: Decimal
    total_unrealized_pnl_base: Decimal
    total_unrealized_pnl_pct: Decimal | None
    price_as_of_date: date | None
    concentration: ConcentrationOut
    stale_tickers: list[str]
    holdings: list[HoldingValueOut]


class PerformancePointOut(BaseModel):
    date: date
    value_base: Decimal
    return_pct_cumulative: Decimal
    is_approximate: bool


class PortfolioSeriesOut(BaseModel):
    empty: bool
    start_date: date | None
    end_date: date | None
    # First real (non-backfilled) complete-batch snapshot day for this user,
    # unfiltered by market/group/broker/account (issue #366 / vault §6) —
    # the "since tracking" evidence anchor, never "earliest ticker price
    # available" (the retired composition-replay start signal). May be
    # earlier than `start_date` when dimension filters shorten the series.
    tracking_start: date | None
    points: list[PerformancePointOut]
    quality_flags: list[str]


class BenchmarkPointOut(BaseModel):
    date: dt.date
    return_pct_cumulative: Decimal | None
    price_as_of: dt.date | None
    fx_as_of: dict[str, dt.date]
    carried: bool
    unavailable_reason: str | None


class BenchmarkSeriesOut(BaseModel):
    index_code: str
    name: str
    start_date: date | None
    points: list[BenchmarkPointOut]
    # Comparison eligibility (issue #377). History may still be displayable
    # when this is false; do not treat the series as a head-to-head return.
    comparable: bool
    displayable: bool
    normalization: str
    anchor_date: date | None
    display_start_date: date | None
    display_end_date: date | None
    comparison_start: date | None
    comparison_end: date | None
    comparison_status: str
    comparison_return_pct: Decimal | None


class PerformanceHeaderOut(BaseModel):
    value_base: Decimal
    value_change_base: Decimal
    value_change_pct: Decimal
    label: str


class PerformanceMetaOut(BaseModel):
    range: str
    twr: bool
    base_currency: str
    filters: dict[str, list[str]]


class PortfolioPerformanceResponse(BaseModel):
    """GET /portfolio/performance (issues #360 / #366 / #377)."""

    portfolio: PortfolioSeriesOut
    benchmarks: list[BenchmarkSeriesOut]
    header: PerformanceHeaderOut
    meta: PerformanceMetaOut


class SendOverviewResponse(BaseModel):
    """POST /portfolio/send-overview (issue #202).

    `sent=False` with `retry_after_seconds` set means the 15-minute cooldown
    is still running — the frontend renders "still X minutes left" and never
    shows this as an error. `sent=True` means the send was dispatched
    (fire-and-forget); a downstream delivery failure surfaces only via the
    ops alerts `send_portfolio_overview_email` fires, not in this response.
    """

    sent: bool
    retry_after_seconds: int | None = None
