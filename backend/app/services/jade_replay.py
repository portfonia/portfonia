"""Read-only replay of today's unchanged holdings (issue #714)."""

from bisect import bisect_right
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from itertools import pairwise
from math import sqrt
from statistics import covariance, stdev, variance
from typing import cast
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.timezones import today_et
from app.models.benchmark_price import BenchmarkPrice
from app.models.jade_price import JadePricePoint, JadePriceSeries
from app.schemas.jade import (
    Coverage,
    JadeReplayOut,
    Metrics,
    ReplayHolding,
    ReplayMetrics,
    ReplayPoint,
)
from app.services.benchmark_valuation import load_fx_series
from app.services.fx_conversion import conversion_pairs, to_base
from app.services.instrument_symbols import normalize_legacy_ticker
from app.services.jade_replay_config import (
    BENCHMARK_ETF,
    BETA_MIN_SAMPLES,
    CARRY_DAYS,
    CLASS_PROXY,
    DATA_QUALITY_SHARE,
    MIN_RETURNS,
    STOCK_PROXY_BY_MARKET,
    EtfSpec,
    ReplayRange,
    months_before,
    years_before,
)
from app.services.portfolio_calculator import HoldingValue, compute_portfolio


def ratio(value: float | Decimal) -> str:
    return f"{value:.6f}"


def price_on(prices: dict[date, Decimal], day: date) -> Decimal | None:
    dates = sorted(prices)
    i = bisect_right(dates, day) - 1
    return prices[dates[i]] if i >= 0 and (day - dates[i]).days <= CARRY_DAYS else None


def valid_returns(values: list[tuple[date, float]]) -> list[tuple[date, float]]:
    return [(d, v / prev - 1) for (_, prev), (d, v) in pairwise(values)]


def metrics(values: list[tuple[date, float]], hide_worst_month: bool) -> Metrics:
    start, first = values[0]
    end, last = values[-1]
    returns = valid_returns(values)
    peak = first
    peak_date = start
    drawdown = 0.0
    dd_peak = start
    dd_trough = start
    months: dict[str, float] = {}
    for day, value in values:
        if value > peak:
            peak = value
            peak_date = day
        dd = value / peak - 1
        if dd < drawdown:
            drawdown = dd
            dd_peak = peak_date
            dd_trough = day
        months[day.strftime("%Y-%m")] = value
    month_returns = []
    prev = first
    for month, value in months.items():
        month_returns.append((month, value / prev - 1))
        prev = value
    worst_month, month_return = min(month_returns, key=lambda x: x[1])
    worst_date, worst = min(returns, key=lambda x: x[1])
    return Metrics(
        cumulative_return=ratio(last / first - 1),
        annualized_return=ratio((last / first) ** (365.25 / (end - start).days) - 1),
        annualized_vol=ratio(stdev([r for _, r in returns]) * sqrt(252)),
        max_drawdown=ratio(drawdown),
        max_drawdown_peak=dd_peak,
        max_drawdown_trough=dd_trough,
        worst_day=ratio(worst),
        worst_day_date=worst_date,
        worst_month=None if hide_worst_month else ratio(month_return),
        worst_month_label=None if hide_worst_month else worst_month,
    )


def estimate_beta(pairs: list[tuple[float, float]]) -> float:
    if len(pairs) < BETA_MIN_SAMPLES:
        return 1.0
    y, p = zip(*pairs, strict=True)
    var = variance(p)
    return covariance(y, p) / var if var > 0 else 1.0


def backward_value(value: float, current_proxy: float, previous_proxy: float, beta: float) -> float:
    return value / (1 + beta * (current_proxy / previous_proxy - 1))


def anchored_values(
    amount: Decimal, values: dict[date, Decimal], end: date
) -> list[tuple[date, Decimal]]:
    return [(d, amount * x / values[end]) for d, x in values.items()]


def own_key(h: HoldingValue) -> str:
    return "yf:" + normalize_legacy_ticker(h.ticker) if h.ticker else "nav:" + str(h.fund_code)


def proxy_for(h: HoldingValue) -> EtfSpec:
    return (
        STOCK_PROXY_BY_MARKET[h.market]
        if h.asset_class == "STOCK"
        else CLASS_PROXY[str(h.asset_class)]
    )


def window_start(session: Session, end: date, range_key: ReplayRange) -> date:
    if range_key == "YTD":
        return cast(
            date,
            session.scalar(
                select(func.max(BenchmarkPrice.price_date)).where(
                    BenchmarkPrice.index_code == "sp500",
                    BenchmarkPrice.price_date < date(end.year, 1, 1),
                )
            ),
        )
    if range_key.endswith("M"):
        return months_before(end, int(range_key[:-1]))
    return years_before(end, int(range_key[:-1]))


@dataclass(frozen=True)
class ReplayBuild:
    out: JadeReplayOut
    portfolio: list[tuple[date, float]]
    benchmark: list[tuple[date, float]]
    total: Decimal


def compute_replay(
    session: Session, user_id: UUID, base_currency: str, benchmark: str, range_key: ReplayRange
) -> JadeReplayOut:
    return build_replay(session, user_id, base_currency, benchmark, range_key).out


def build_replay(
    session: Session, user_id: UUID, base_currency: str, benchmark: str, range_key: ReplayRange
) -> ReplayBuild:
    end = cast(
        date,
        session.scalar(
            select(func.max(BenchmarkPrice.price_date)).where(
                BenchmarkPrice.index_code == "sp500", BenchmarkPrice.price_date <= today_et()
            )
        ),
    )
    start = window_start(session, end, range_key)
    days = list(
        session.scalars(
            select(BenchmarkPrice.price_date)
            .where(
                BenchmarkPrice.index_code == "sp500", BenchmarkPrice.price_date.between(start, end)
            )
            .order_by(BenchmarkPrice.price_date)
        )
    )
    snap = compute_portfolio(session, user_id, base_currency)
    spec = BENCHMARK_ETF[benchmark]
    keys = {"yf:" + spec.symbol}
    currencies = {base_currency, spec.currency}
    for h in snap.holdings:
        if h.pricing_mode == "auto" and (h.ticker or h.fund_code):
            keys.add(own_key(h))
        currencies.add(h.currency)
        if h.asset_class != "CASH_EQUIV":
            proxy_spec = proxy_for(h)
            keys.add("yf:" + proxy_spec.symbol)
            currencies.add(proxy_spec.currency)
    cached = {
        s.series_key: s
        for s in session.scalars(
            select(JadePriceSeries).where(JadePriceSeries.series_key.in_(keys))
        )
    }
    prices: dict[str, dict[date, Decimal]] = {k: {} for k in keys}
    rows = session.execute(
        select(JadePricePoint.series_key, JadePricePoint.trade_date, JadePricePoint.close)
        .where(
            JadePricePoint.series_key.in_(keys),
            JadePricePoint.trade_date.between(start - timedelta(days=CARRY_DAYS), end),
        )
        .order_by(JadePricePoint.trade_date)
    ).all()
    for key, day, close in rows:
        prices[key][day] = close
    first_dates: dict[str, date] = {
        key: day
        for key, day in session.execute(
            select(JadePricePoint.series_key, func.min(JadePricePoint.trade_date))
            .where(JadePricePoint.series_key.in_(keys))
            .group_by(JadePricePoint.series_key)
        ).all()
    }
    pairs = {
        pair
        for c in currencies
        for target in currencies
        for pair in conversion_pairs(c, target) or []
    }
    fx_rows = load_fx_series(session, sorted(pairs), start - timedelta(days=CARRY_DAYS), end)
    fx_prices = {p: dict(rows) for p, rows in fx_rows.items()}

    def fx(currency: str, target: str, day: date) -> Decimal | None:
        rates = {
            p: r for p, series in fx_prices.items() if (r := price_on(series, day)) is not None
        }
        return to_base(Decimal(1), currency, target, rates)

    def converted(key: str, currency: str, target: str, day: date) -> Decimal | None:
        price = price_on(prices[key], day)
        rate = fx(currency, target, day)
        return price * rate if price is not None and rate is not None else None

    def pending(key: str) -> bool:
        return key not in cached or cached[key].last_attempt_on is None

    def usable(key: str) -> bool:
        return (
            key in cached
            and cached[key].unusable_reason is None
            and price_on(prices[key], end) is not None
        )

    holding_rows = []
    included: list[tuple[HoldingValue, ReplayHolding, dict[date, Decimal]]] = []
    for h in snap.holdings:
        row = ReplayHolding(holding_id=h.holding_id, name=h.name, method="excluded")
        own = own_key(h)
        proxy: EtfSpec | None = None
        if h.market_value_base is None or h.market_value_base <= 0:
            row.excluded_reason = "unvalued"
        elif h.asset_type == "cash" or h.asset_class == "CASH_EQUIV":
            row.method = "cash"
        elif h.asset_type == "wmf":
            row.method = "cash_assumed"
        else:
            if h.pricing_mode == "auto":
                if pending(own):
                    row.excluded_reason = "pending"
                elif usable(own):
                    row.own_first_date = first_dates[own]
                    row.method = (
                        ("own" if h.ticker else "fund_nav")
                        if first_dates[own] <= start + timedelta(days=CARRY_DAYS)
                        else "head_proxy"
                    )
                else:
                    row.own_history_unavailable = True
            if row.method == "excluded" and row.excluded_reason is None:
                row.method = (
                    "cash_assumed"
                    if h.asset_class == "BOND_FUND" and h.currency in ("CNY", "CNH")
                    else "proxy"
                )
            if row.method in ("proxy", "head_proxy"):
                proxy = proxy_for(h)
                row.proxy_symbol = proxy.symbol
                row.proxy_name = proxy.name
                key = "yf:" + proxy.symbol
                if pending(key):
                    row.method = "excluded"
                    row.excluded_reason = "pending"
                elif not usable(key):
                    row.method = "excluded"
                    row.excluded_reason = "data_unavailable"
        values: dict[date, Decimal] = {}
        if row.method in ("cash", "cash_assumed"):
            values = {d: x for d in days if (x := fx(h.currency, base_currency, d)) is not None}
        elif row.method in ("own", "fund_nav"):
            values = {
                d: x
                for d in days
                if (x := converted(own, h.currency, base_currency, d)) is not None
            }
        elif row.method == "proxy":
            assert proxy is not None
            values = {
                d: x
                for d in days
                if (x := converted("yf:" + proxy.symbol, proxy.currency, base_currency, d))
                is not None
            }
        elif row.method == "head_proxy":
            assert proxy is not None and row.own_first_date is not None
            local = {
                d: float(x)
                for d in days
                if d >= row.own_first_date
                and (x := converted(own, h.currency, proxy.currency, d)) is not None
            }
            p = {
                d: float(x)
                for d in days
                if (x := price_on(prices["yf:" + proxy.symbol], d)) is not None
            }
            beta_pairs = [
                (local[b] / local[a] - 1, p[b] / p[a] - 1)
                for a, b in pairwise(days)
                if a in local and b in local and a in p and b in p
            ]
            beta = estimate_beta(beta_pairs)
            row.beta = f"{beta:.4f}"
            row.beta_samples = len(beta_pairs)
            row.own_vol = (
                ratio(stdev([y for y, _ in beta_pairs]) * sqrt(252))
                if len(beta_pairs) >= MIN_RETURNS
                else None
            )
            before = [
                p[b] / p[a] - 1
                for a, b in pairwise(days)
                if b < row.own_first_date and a in p and b in p
            ]
            row.proxy_segment_vol = (
                ratio(abs(beta) * stdev(before) * sqrt(252)) if len(before) >= 2 else None
            )
            anchor = next(d for d in days if d >= row.own_first_date)
            last = local.get(anchor)
            for a, b in reversed(list(pairwise(days))):
                if a >= row.own_first_date:
                    continue
                if last is not None and a in p and b in p:
                    last = backward_value(last, p[b], p[a], beta)
                    local[a] = last
            values = {
                d: Decimal(str(x)) * rate
                for d, x in local.items()
                if (rate := fx(proxy.currency, base_currency, d)) is not None
            }
        if row.method != "excluded":
            if end not in values:
                row.method = "excluded"
                row.excluded_reason = "data_unavailable"
            else:
                included.append((h, row, values))
        holding_rows.append(row)
    total = sum((h.market_value_base or Decimal(0) for h, _, _ in included), Decimal(0))
    shares = {m: Decimal(0) for m in ("own", "head_proxy", "proxy", "cash", "cash_assumed")}
    for h, row, _ in included:
        assert h.market_value_base is not None
        row.weight = ratio(h.market_value_base / total)
        shares["own" if row.method == "fund_nav" else row.method] += h.market_value_base / total
    approx = shares["head_proxy"] + shares["proxy"] + shares["cash_assumed"]
    pending_value = sum(
        (
            h.market_value_base or Decimal(0)
            for h, row in zip(snap.holdings, holding_rows, strict=True)
            if row.excluded_reason == "pending"
        ),
        Decimal(0),
    )
    coverage = Coverage(
        **{k + "_share": ratio(v) for k, v in shares.items()},
        approx_share_at_start=ratio(approx),
        data_quality=approx >= DATA_QUALITY_SHARE,
        pending_share=ratio(pending_value / (total + pending_value)) if pending_value else None,
    )
    rolled = [
        dict(anchored_values(cast(Decimal, h.market_value_base), values, end))
        for h, _, values in included
    ]
    portfolio = [
        (d, float(sum((values[d] for values in rolled), Decimal(0))))
        for d in days
        if rolled and all(d in values for values in rolled)
    ]
    count = max(len(portfolio) - 1, 0)
    status = (
        "no_holdings"
        if not any(
            h.market_value_base is not None and h.market_value_base > 0 for h in snap.holdings
        )
        else "pending"
        if not included
        else "insufficient"
        if count < MIN_RETURNS
        else "ok"
    )
    first = portfolio[0][0] if portfolio else None
    benchmark_key = "yf:" + spec.symbol
    benchmark_values = [
        (d, float(x))
        for d in days
        if first is not None
        and d >= first
        and (x := converted(benchmark_key, spec.currency, base_currency, d)) is not None
    ]
    benchmark_status = (
        "pending"
        if pending(benchmark_key)
        else "unavailable"
        if not usable(benchmark_key) or len(benchmark_values) - 1 < MIN_RETURNS
        else "ok"
    )
    pm = metrics(portfolio, hide_worst_month=range_key == "1M") if status == "ok" else None
    bm = (
        metrics(benchmark_values, hide_worst_month=range_key == "1M")
        if benchmark_status == "ok"
        else None
    )
    pv = dict(portfolio)
    bv = dict(benchmark_values) if bm else {}
    points = (
        [
            ReplayPoint(
                date=d,
                portfolio=ratio(pv[d] / portfolio[0][1] - 1) if d in pv else None,
                benchmark=ratio(bv[d] / benchmark_values[0][1] - 1) if d in bv else None,
            )
            for d in days
            if first is not None and d >= first
        ]
        if status == "ok"
        else []
    )
    out = JadeReplayOut(
        range=range_key,
        status=status,
        base_currency=base_currency,
        benchmark=benchmark,
        benchmark_symbol=spec.symbol,
        benchmark_name=spec.name,
        benchmark_status=benchmark_status,
        window_start=start,
        window_end=end,
        first_valid_date=first,
        sample_count=count,
        skipped_days=len(days) - len(portfolio),
        points=points,
        metrics=ReplayMetrics(portfolio=pm, benchmark=bm),
        coverage=coverage,
        holdings=holding_rows,
    )

    return ReplayBuild(out, portfolio, benchmark_values, total)
