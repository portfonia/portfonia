"""Jade holdings replay response; numeric ratios have explicit precision."""

from datetime import date
from typing import Literal
from uuid import UUID

from pydantic import BaseModel

from app.services.jade_replay_config import ReplayRange, ScenarioId


class Metrics(BaseModel):
    cumulative_return: str
    annualized_return: str
    annualized_vol: str
    max_drawdown: str
    max_drawdown_peak: date
    max_drawdown_trough: date
    worst_day: str
    worst_day_date: date
    worst_month: str | None
    worst_month_label: str | None


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
    excluded_reason: Literal["unvalued", "watch_only", "pending", "data_unavailable"] | None = None
    own_history_unavailable: bool = False
    proxy_symbol: str | None = None
    proxy_name: str | None = None
    beta: str | None = None
    beta_samples: int | None = None
    own_first_date: date | None = None
    own_vol: str | None = None
    proxy_segment_vol: str | None = None


class JadeReplayOut(BaseModel):
    range: ReplayRange
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


class TailCell(BaseModel):
    var: str
    cvar: str
    var_amount: str | None = None
    cvar_amount: str | None = None


class TailLevel(BaseModel):
    level: Literal[95, 99]
    available: bool
    tail_days: int | None
    tail_windows: int | None
    daily: TailCell | None
    monthly: TailCell | None
    normal_daily: TailCell | None
    normal_monthly: TailCell | None
    benchmark_daily: TailCell | None
    benchmark_monthly: TailCell | None
    reference_daily: str | None
    reference_monthly: str | None


class HistogramBin(BaseModel):
    lower: str
    upper: str
    count: int


class JadeTailRiskOut(BaseModel):
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
    month_windows: int
    month_independent: int
    portfolio_value: str | None
    levels: list[TailLevel]
    histogram: list[HistogramBin]
    tolerance_status: Literal["ok", "no_questionnaire"]
    coverage: Coverage
    proxy_understates: bool


class StressHolding(BaseModel):
    holding_id: UUID
    name: str
    asset_class: str | None
    weight: str | None = None
    method: Literal["own", "fund_nav", "head_proxy", "proxy", "cash", "cash_assumed", "excluded"]
    excluded_reason: Literal["unvalued", "watch_only", "pending", "data_unavailable"] | None = None
    proxy_symbol: str | None = None
    proxy_name: str | None = None
    beta: str | None = None
    beta_samples: int | None = None
    own_first_date: date | None = None
    proxy_for: str | None = None
    price_only: bool = False


class StressContribution(BaseModel):
    asset_class: str
    contribution: str


class StressPoint(BaseModel):
    date: date
    portfolio: str
    benchmark: str | None


class StressSubstitution(BaseModel):
    primary: str
    symbol: str
    name: str
    price_only: bool


class StressScenario(BaseModel):
    fx_source: Literal["standard", "fred"]
    benchmark_symbol: str
    benchmark_name: str
    benchmark_price_only: bool
    substitutions: list[StressSubstitution]
    price_index_symbols: list[str]
    id: ScenarioId
    peak_date: date
    trough_date: date
    window_start: date
    window_end: date
    status: Literal["ok", "pending", "no_holdings", "insufficient"]
    first_valid_date: date | None
    sample_count: int  # max(len(valid) - 1, 0)
    portfolio_value: str | None
    shock_return: str | None
    shock_amount: str | None
    max_drawdown: str | None
    max_drawdown_peak: date | None
    max_drawdown_trough: date | None
    benchmark_status: Literal["ok", "pending", "unavailable"]
    benchmark_shock_return: str | None
    benchmark_max_drawdown: str | None
    contributions: list[StressContribution]
    points: list[StressPoint]
    coverage: Coverage
    proxy_understates: bool
    holdings: list[StressHolding]


class JadeStressOut(BaseModel):
    base_currency: str
    benchmark: Literal["sp500", "dow30", "nasdaq", "csi300"]
    benchmark_symbol: str
    benchmark_name: str
    scenarios: list[StressScenario]  # SCENARIOS order


class StyleWeight(BaseModel):
    symbol: str
    weight: str


class StyleFit(BaseModel):
    weights: list[StyleWeight]
    r_squared: str | None
    residual_vol: str
    low_fit: bool


class StylePoint(BaseModel):
    date: date
    portfolio: str
    style_mix: str


class JadeStyleOut(BaseModel):
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
    horizon_days: int
    min_samples: int
    portfolio: StyleFit | None
    benchmark_fit: StyleFit | None
    points: list[StylePoint]
    coverage: Coverage
    proxy_inflates_fit: bool
