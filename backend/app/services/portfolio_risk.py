"""Read-only historical volatility and questionnaire-relative risk for issue #551."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.timezones import today_et
from app.models.benchmark_price import BenchmarkPrice
from app.models.user_investment_context import UserInvestmentContext
from app.services.benchmark_valuation import (
    evaluate_index_range,
    load_fx_series,
    load_index_closes,
    required_pairs_for_closes,
)
from app.services.portfolio_calculator import HoldingValue, compute_portfolio
from app.services.portfolio_performance import Filters, _build_portfolio_series, _tracking_start

logger = logging.getLogger(__name__)

WINDOW = 60
MIN_SAMPLES = 10
ANNUALIZE = Decimal(252).sqrt()
VOL_TIERS = (Decimal("0.10"), Decimal("0.20"))
RISK_BASE = {
    "CONSERVATIVE": (Decimal("0.10"), Decimal("0.20")),
    "BALANCED": (Decimal("0.20"), Decimal("0.30")),
    "AGGRESSIVE": (Decimal("0.30"), Decimal("0.40")),
}
DEFENSIVE_SHIFT = Decimal("0.05")
RISK_FLOOR = (Decimal("0.10"), Decimal("0.20"))
MANUAL_SHARE_LIMIT = Decimal("0.66")
RISK_ASSET_TIERS = (Decimal("0.40"), Decimal("0.75"))
INDEX_STOCK_LIMIT = Decimal("0.30")
OFF_MARKET_LIMIT = Decimal("0.30")
_FOUR_DP = Decimal("0.0001")

VolStatus = Literal["ok", "insufficient_sample"]
RiskStatus = Literal["ok", "no_questionnaire", "insufficient_sample", "data_quality"]
DeviationStatus = Literal["ok", "no_questionnaire", "no_valued_holdings"]
BenchmarkCode = Literal["sp500", "dow30", "nasdaq", "csi300"]


@dataclass
class VolPoint:
    date: date
    vol: Decimal


@dataclass
class VolSeries:
    status: VolStatus = "insufficient_sample"
    current: Decimal | None = None
    tier: Literal["low", "medium", "high"] | None = None
    window_start: date | None = None
    window_end: date | None = None
    sample_count: int = 0
    points: list[VolPoint] = field(default_factory=list)


@dataclass
class BetaResult:
    status: VolStatus = "insufficient_sample"
    value: Decimal | None = None
    sample_count: int = 0


@dataclass
class RiskResult:
    status: RiskStatus
    label: Literal["within", "caution", "exceeds"] | None = None


@dataclass
class DeviationResult:
    status: DeviationStatus
    delta: int | None = None


@dataclass
class PortfolioRiskResult:
    base_currency: str
    portfolio_vol: VolSeries
    benchmark_vols: list[tuple[BenchmarkCode, VolSeries]]
    beta: BetaResult
    risk: RiskResult
    deviation: DeviationResult
    manual_valuation_share: Decimal | None


def _tier(vol: Decimal) -> Literal["low", "medium", "high"]:
    if vol < VOL_TIERS[0]:
        return "low"
    if vol < VOL_TIERS[1]:
        return "medium"
    return "high"


def _sample_variance(values: list[Decimal]) -> Decimal:
    mean = sum(values, Decimal(0)) / len(values)
    return sum(((value - mean) ** 2 for value in values), Decimal(0)) / (len(values) - 1)


def _rolling_vol(
    days: list[date], returns: dict[date, Decimal], visible_days: list[date]
) -> VolSeries:
    if not days:
        return VolSeries()
    positions = {day: i for i, day in enumerate(days)}
    result = VolSeries(window_start=days[max(0, len(days) - WINDOW)], window_end=days[-1])
    for day in visible_days:
        i = positions[day]
        samples = [returns[d] for d in days[max(0, i - WINDOW + 1) : i + 1] if d in returns]
        if day == days[-1]:
            result.sample_count = len(samples)
        if len(samples) < MIN_SAMPLES:
            continue
        vol = _sample_variance(samples).sqrt() * ANNUALIZE
        result.points.append(VolPoint(day, vol))
        if day == days[-1]:
            result.status = "ok"
            result.current = vol
            result.tier = _tier(vol)
    return result


def _beta(
    days: list[date], portfolio: dict[date, Decimal], sp500: dict[date, Decimal]
) -> BetaResult:
    pairs = [(portfolio[d], sp500[d]) for d in days[-WINDOW:] if d in portfolio and d in sp500]
    result = BetaResult(sample_count=len(pairs))
    if len(pairs) < MIN_SAMPLES:
        return result
    xs = [pair[0] for pair in pairs]
    ys = [pair[1] for pair in pairs]
    variance = _sample_variance(ys)
    if variance == 0:
        return result
    xmean = sum(xs, Decimal(0)) / len(xs)
    ymean = sum(ys, Decimal(0)) / len(ys)
    covariance = sum(((x - xmean) * (y - ymean) for x, y in pairs), Decimal(0)) / (len(pairs) - 1)
    return BetaResult("ok", covariance / variance, len(pairs))


def _risk_label(
    vol: Decimal, appetite: str, horizon: str | None, objective: str | None
) -> Literal["within", "caution", "exceeds"]:
    caution, exceeds = RISK_BASE[appetite]
    if horizon == "SHORT" or objective == "PRESERVATION":
        caution = max(caution - DEFENSIVE_SHIFT, RISK_FLOOR[0])
        exceeds = max(exceeds - DEFENSIVE_SHIFT, RISK_FLOOR[1])
    if vol < caution:
        return "within"
    if vol < exceeds:
        return "caution"
    return "exceeds"


def _valued(holdings: list[HoldingValue]) -> list[HoldingValue]:
    return [holding for holding in holdings if holding.market_value_base is not None]


def _manual_share(holdings: list[HoldingValue]) -> Decimal | None:
    valued = _valued(holdings)
    total = sum((holding.market_value_base or Decimal(0) for holding in valued), Decimal(0))
    if total == 0:
        return None
    manual = sum(
        (
            holding.market_value_base or Decimal(0)
            for holding in valued
            if holding.asset_type == "wmf"
            or (holding.pricing_mode == "manual" and holding.asset_type != "cash")
        ),
        Decimal(0),
    )
    return manual / total


def _deviation(holdings: list[HoldingValue], answers: dict[str, object]) -> DeviationResult:
    valued = _valued(holdings)
    total = sum((holding.market_value_base or Decimal(0) for holding in valued), Decimal(0))
    if total == 0:
        return DeviationResult("no_valued_holdings")
    appetite = answers["risk_appetite"]
    assert isinstance(appetite, str)
    target = {"CONSERVATIVE": 1, "BALANCED": 2, "AGGRESSIVE": 3}[appetite]
    risk_assets = sum(
        (
            holding.market_value_base or Decimal(0)
            for holding in valued
            if holding.asset_class not in {"BOND_FUND", "CASH_EQUIV"}
        ),
        Decimal(0),
    )
    share = risk_assets / total
    observed = 1 if share < RISK_ASSET_TIERS[0] else 2 if share < RISK_ASSET_TIERS[1] else 3
    stock = sum(
        (
            holding.market_value_base or Decimal(0)
            for holding in valued
            if holding.asset_class == "STOCK"
        ),
        Decimal(0),
    )
    if answers.get("style") == "INDEX" and stock / total >= INDEX_STOCK_LIMIT:
        observed += 1
    markets = answers.get("markets")
    if isinstance(markets, list) and markets:
        selected = set(markets)
        non_cash = [holding for holding in valued if holding.asset_type != "cash"]
        non_cash_total = sum(
            (holding.market_value_base or Decimal(0) for holding in non_cash), Decimal(0)
        )
        outside = sum(
            (
                holding.market_value_base or Decimal(0)
                for holding in non_cash
                if (holding.market if holding.market in {"US", "HK", "A-Share"} else "Other")
                not in selected
            ),
            Decimal(0),
        )
        if non_cash_total > 0 and outside / non_cash_total >= OFF_MARKET_LIMIT:
            observed += 1
    return DeviationResult("ok", min(3, observed) - target)


def _index_days(session: Session, code: str, today: date) -> list[date]:
    rows = session.execute(
        select(BenchmarkPrice.price_date)
        .where(BenchmarkPrice.index_code == code, BenchmarkPrice.price_date <= today)
        .order_by(BenchmarkPrice.price_date.desc())
        .limit(2 * WINDOW)
    ).scalars()
    return sorted(rows)


def _index_returns(
    session: Session, code: str, days: list[date], currency: str
) -> dict[date, Decimal]:
    if not days:
        return {}
    closes = load_index_closes(session, [code], days[0], days[-1])[code]
    pairs = required_pairs_for_closes({code: closes}, currency)
    fx = load_fx_series(session, pairs, closes[0].source_date, days[-1])
    values = evaluate_index_range(days[0], days[-1], closes, fx, currency)
    result: dict[date, Decimal] = {}
    previous: Decimal | None = None
    predecessor = [row.source_date for row in closes if row.source_date < days[0]]
    if predecessor:
        preceding = evaluate_index_range(predecessor[-1], predecessor[-1], closes, fx, currency)
        valuation = preceding.get(predecessor[-1])
        if valuation is not None and valuation.price_as_of == predecessor[-1]:
            previous = valuation.value
    for day in days:
        valuation = values.get(day)
        current = (
            valuation.value if valuation is not None and valuation.price_as_of == day else None
        )
        if current is not None:
            if previous is not None and previous > 0:
                result[day] = current / previous - 1
            previous = current
    return result


def compute_portfolio_risk(
    session: Session,
    user_id: uuid.UUID,
    benchmarks: list[BenchmarkCode],
    base_currency: str,
    today: date | None = None,
) -> PortfolioRiskResult:
    today = today or today_et()
    nyse_days = _index_days(session, "sp500", today)
    sp_returns = _index_returns(session, "sp500", nyse_days, base_currency)
    portfolio_returns: dict[date, Decimal] = {}
    if nyse_days:
        tracking_start = _tracking_start(session, user_id)
        build = _build_portfolio_series(
            session,
            user_id,
            nyse_days[0],
            nyse_days[-1],
            Filters(),
            True,
            base_currency,
            tracking_start,
        )
        for day in nyse_days:
            link = build.daily_links.get(day)
            if link is not None and tracking_start is not None and day > tracking_start:
                portfolio_returns[day] = link
    portfolio_vol = _rolling_vol(nyse_days, portfolio_returns, nyse_days[-WINDOW:])
    benchmark_vols: list[tuple[BenchmarkCode, VolSeries]] = []
    for code in dict.fromkeys(benchmarks):
        selected_days = nyse_days if code == "sp500" else _index_days(session, code, today)
        returns = (
            sp_returns
            if code == "sp500"
            else _index_returns(session, code, selected_days, base_currency)
        )
        benchmark_vols.append((code, _rolling_vol(selected_days, returns, selected_days[-WINDOW:])))
    beta = _beta(nyse_days, portfolio_returns, sp_returns)
    holdings = compute_portfolio(session, user_id=user_id, base_currency=base_currency).holdings
    manual_share = _manual_share(holdings)
    context = session.get(UserInvestmentContext, user_id)
    answers = context.questionnaire if context is not None else None
    answer_markets = answers.get("markets") if answers is not None else None
    appetite = answers.get("risk_appetite") if answers is not None else None
    horizon = answers.get("horizon") if answers is not None else None
    objective = answers.get("objective") if answers is not None else None
    style = answers.get("style") if answers is not None else None
    risk_answers_valid = answers is not None and not (
        not isinstance(appetite, str)
        or appetite not in RISK_BASE
        or horizon not in ("SHORT", "MEDIUM", "LONG")
        or objective not in ("PRESERVATION", "GROWTH", "INCOME")
    )
    deviation_answers_valid = answers is not None and not (
        not isinstance(appetite, str)
        or appetite not in RISK_BASE
        or style not in ("VALUE", "GROWTH", "INDEX", "MIXED")
        or not isinstance(answer_markets, list)
        or any(
            not isinstance(market, str) or market not in ("US", "HK", "A-Share", "Other")
            for market in answer_markets
        )
    )
    if answers is not None and (not risk_answers_valid or not deviation_answers_valid):
        logger.warning("Invalid stored investment questionnaire for user %s", user_id)
    if not risk_answers_valid or answers is None:
        risk = RiskResult("no_questionnaire")
    else:
        if portfolio_vol.current is None:
            risk = RiskResult("insufficient_sample")
        elif manual_share is not None and manual_share >= MANUAL_SHARE_LIMIT:
            risk = RiskResult("data_quality")
        else:
            risk = RiskResult(
                "ok",
                _risk_label(
                    portfolio_vol.current,
                    str(answers["risk_appetite"]),
                    str(answers["horizon"]),
                    str(answers["objective"]),
                ),
            )
    if not deviation_answers_valid or answers is None:
        deviation = DeviationResult("no_questionnaire")
    else:
        deviation = _deviation(holdings, answers)
    return PortfolioRiskResult(
        base_currency, portfolio_vol, benchmark_vols, beta, risk, deviation, manual_share
    )
