"""Jade holdings replay response; numeric ratios have explicit precision."""

from datetime import date
from typing import Literal
from uuid import UUID

from pydantic import BaseModel


class Metrics(BaseModel):
    cumulative_return: str
    annualized_return: str
    annualized_vol: str
    max_drawdown: str
    max_drawdown_peak: date
    max_drawdown_trough: date
    worst_day: str
    worst_day_date: date
    worst_month: str
    worst_month_label: str


class ReplayMetrics(BaseModel):
    portfolio: Metrics | None = None
    benchmark: Metrics | None = None


class ReplayPoint(BaseModel):
    date: date
    portfolio: str | None
    benchmark: str | None


class Coverage(BaseModel):
    own_share: str = "0.000000"
    head_proxy_share: str = "0.000000"
    proxy_share: str = "0.000000"
    cash_share: str = "0.000000"
    cash_assumed_share: str = "0.000000"
    approx_share_at_start: str = "0.000000"
    data_quality: bool = False
    pending_share: str | None = None


class ReplayHolding(BaseModel):
    holding_id: UUID
    name: str
    weight: str | None = None
    method: Literal["own", "fund_nav", "head_proxy", "proxy", "cash", "cash_assumed", "excluded"]
    excluded_reason: Literal["unvalued", "pending", "data_unavailable"] | None = None
    own_history_unavailable: bool = False
    proxy_symbol: str | None = None
    proxy_name: str | None = None
    beta: str | None = None
    beta_samples: int | None = None
    own_first_date: date | None = None
    own_vol: str | None = None
    proxy_segment_vol: str | None = None


class JadeReplayOut(BaseModel):
    status: Literal["ok", "pending", "no_holdings", "insufficient"]
    base_currency: str
    benchmark: Literal["sp500", "dow30", "nasdaq", "csi300"]
    benchmark_symbol: str
    benchmark_name: str
    benchmark_status: Literal["ok", "pending", "unavailable"]
    window_start: date
    window_end: date
    first_valid_date: date | None
    sample_count: int
    skipped_days: int
    points: list[ReplayPoint]
    metrics: ReplayMetrics
    coverage: Coverage
    holdings: list[ReplayHolding]
