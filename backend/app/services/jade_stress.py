"""Read-only historical stress paths at today's value weights (issue #720)."""

from datetime import date, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from itertools import pairwise
from typing import Literal, cast
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.timezones import today_et
from app.models.benchmark_price import BenchmarkPrice
from app.models.jade_price import (
    JadePricePoint,
    JadePriceSeries,
    JadeScenarioPoint,
    JadeScenarioSeries,
)
from app.schemas.jade import (
    Coverage,
    JadeStressOut,
    StressContribution,
    StressHolding,
    StressPoint,
    StressScenario,
    StressSubstitution,
)
from app.services.benchmark_valuation import load_fx_series
from app.services.fx_conversion import conversion_pairs, to_base
from app.services.jade_replay import estimate_beta, metrics, own_key, price_on, proxy_for, ratio
from app.services.jade_replay_config import (
    BENCHMARK_ETF,
    CALENDAR_KEY,
    CARRY_DAYS,
    DATA_QUALITY_SHARE,
    MIN_RETURNS,
    REPLAY_YEARS,
    SCENARIOS,
    EtfSpec,
    Scenario,
)
from app.services.jade_substitutes import SeriesSpec, candidates
from app.services.portfolio_calculator import HoldingValue, compute_portfolio


def holding_beta(session: Session, h: HoldingValue, proxy: EtfSpec) -> tuple[float, int]:
    keys = [own_key(h), "yf:" + proxy.symbol]
    cached = {
        s.series_key: s
        for s in session.scalars(
            select(JadePriceSeries).where(JadePriceSeries.series_key.in_(keys))
        )
    }
    if any(
        k not in cached or cached[k].last_attempt_on is None or cached[k].unusable_reason
        for k in keys
    ):
        return 1.0, 0
    end = cast(
        date,
        session.scalar(
            select(func.max(BenchmarkPrice.price_date)).where(
                BenchmarkPrice.index_code == "sp500", BenchmarkPrice.price_date <= today_et()
            )
        ),
    )
    from app.services.jade_replay_config import years_before

    start = years_before(end, REPLAY_YEARS)
    days = list(
        session.scalars(
            select(BenchmarkPrice.price_date)
            .where(
                BenchmarkPrice.index_code == "sp500", BenchmarkPrice.price_date.between(start, end)
            )
            .order_by(BenchmarkPrice.price_date)
        )
    )
    prices: dict[str, dict[date, Decimal]] = {k: {} for k in keys}
    for point in session.scalars(
        select(JadePricePoint).where(
            JadePricePoint.series_key.in_(keys),
            JadePricePoint.trade_date.between(start - timedelta(days=CARRY_DAYS), end),
        )
    ):
        prices[point.series_key][point.trade_date] = point.close
    fx = {
        k: dict(v)
        for k, v in load_fx_series(
            session,
            conversion_pairs(h.currency, proxy.currency) or [],
            start - timedelta(days=CARRY_DAYS),
            end,
        ).items()
    }
    local: dict[date, Decimal] = {}
    p: dict[date, Decimal] = {}
    for d in days:
        rate = to_base(
            Decimal(1),
            h.currency,
            proxy.currency,
            {k: r for k, v in fx.items() if (r := price_on(v, d)) is not None},
        )
        own = price_on(prices[keys[0]], d)
        if own is not None and rate is not None:
            local[d] = own * rate
        if (value := price_on(prices[keys[1]], d)) is not None:
            p[d] = value
    pairs = [
        (float(local[b] / local[a] - 1), float(p[b] / p[a] - 1))
        for a, b in pairwise(days)
        if a in local and b in local and a in p and b in p
    ]
    return estimate_beta(pairs), len(pairs)


def forward_chain(
    days: list[date],
    proxy: dict[date, float],
    own: dict[date, float],
    first: date | None,
    beta: float,
) -> dict[date, float]:
    values: dict[date, float] = {}
    last: date | None = None
    for day in days:
        if last is None:
            if day in proxy:
                values[day] = 1.0
                last = day
            continue
        if first is not None and last >= first and last in own and day in own:
            values[day] = values[last] * own[day] / own[last]
        elif last in proxy and day in proxy:
            values[day] = values[last] * (1 + beta * (proxy[day] / proxy[last] - 1))
        else:
            continue
        last = day
    return values


def weighted_path(
    days: list[date], curves: list[tuple[dict[date, Decimal], Decimal]]
) -> list[tuple[date, float]]:
    valid = [d for d in days if curves and all(d in values for values, _ in curves)]
    if not valid:
        return []
    first = valid[0]
    return [
        (
            d,
            float(
                sum((weight * values[d] / values[first] for values, weight in curves), Decimal(0))
            ),
        )
        for d in valid
    ]


def compute_stress(
    session: Session, user_id: UUID, base_currency: str, benchmark: str
) -> JadeStressOut:
    snap = compute_portfolio(session, user_id, base_currency)
    spec = BENCHMARK_ETF[benchmark]
    betas: dict[UUID, tuple[float, int]] = {}
    out = JadeStressOut(
        base_currency=base_currency,
        benchmark=benchmark,
        benchmark_symbol=spec.symbol,
        benchmark_name=spec.name,
        scenarios=[],
    )
    for s in SCENARIOS:
        out.scenarios.append(scenario_result(session, s, snap.holdings, base_currency, spec, betas))
    return out


def scenario_result(
    session: Session,
    s: Scenario,
    holdings: list[HoldingValue],
    base: str,
    spec: EtfSpec,
    betas: dict[UUID, tuple[float, int]],
) -> StressScenario:
    keys = {CALENDAR_KEY, *(c.key for c in candidates(spec))}
    currencies = {base, *(c.currency for c in candidates(spec))}
    for h in holdings:
        currencies.add(h.currency)
        if h.pricing_mode == "auto" and (h.ticker or h.fund_code):
            keys.add(own_key(h))
        if h.asset_class != "CASH_EQUIV":
            proxy_spec = proxy_for(h)
            keys.update(c.key for c in candidates(proxy_spec))
            currencies.update(c.currency for c in candidates(proxy_spec))
    cached = {
        row.series_key: row
        for row in session.scalars(
            select(JadeScenarioSeries).where(
                JadeScenarioSeries.scenario_id == s.id, JadeScenarioSeries.series_key.in_(keys)
            )
        )
    }
    prices: dict[str, dict[date, Decimal]] = {k: {} for k in keys}
    for point in session.scalars(
        select(JadeScenarioPoint)
        .where(
            JadeScenarioPoint.scenario_id == s.id,
            JadeScenarioPoint.series_key.in_(keys),
            JadeScenarioPoint.trade_date.between(s.start - timedelta(days=CARRY_DAYS), s.end),
        )
        .order_by(JadeScenarioPoint.trade_date)
    ):
        prices[point.series_key][point.trade_date] = point.close
    days = [d for d in prices[CALENDAR_KEY] if s.start <= d <= s.end]
    pairs = {
        p for c in currencies for target in currencies for p in conversion_pairs(c, target) or []
    }
    fx_prices = {
        k: dict(v)
        for k, v in load_fx_series(
            session, sorted(pairs), s.start - timedelta(days=CARRY_DAYS), s.end
        ).items()
    }

    def fx(currency: str, target: str, d: date) -> Decimal | None:
        return to_base(
            Decimal(1),
            currency,
            target,
            {k: r for k, v in fx_prices.items() if (r := price_on(v, d)) is not None},
        )

    def converted(key: str, currency: str, target: str, d: date) -> Decimal | None:
        p, r = price_on(prices[key], d), fx(currency, target, d)
        return p * r if p is not None and r is not None else None

    def pending(key: str) -> bool:
        return key not in cached or cached[key].last_attempt_on is None

    def resolve(primary: EtfSpec) -> SeriesSpec | Literal["pending", "data_unavailable"]:
        for c in candidates(primary):
            if pending(c.key):
                return "pending"
            if (
                price_on(prices[c.key], s.peak) is not None
                and price_on(prices[c.key], s.trough) is not None
            ):
                return c
        return "data_unavailable"

    rows: list[StressHolding] = []
    included: list[tuple[HoldingValue, StressHolding, dict[date, Decimal]]] = []
    for h in holdings:
        row = StressHolding(
            holding_id=h.holding_id, name=h.name, asset_class=h.asset_class, method="excluded"
        )
        own = own_key(h)
        if h.market_value_base is None or h.market_value_base <= 0:
            row.excluded_reason = (
                "watch_only" if h.watch_tier is not None and h.shares == 0 else "unvalued"
            )
        elif pending(CALENDAR_KEY):
            row.excluded_reason = "pending"
        elif h.asset_type == "cash" or h.asset_class == "CASH_EQUIV":
            row.method = "cash"
        elif h.asset_type == "wmf":
            row.method = "cash_assumed"
        elif h.pricing_mode == "auto" and (h.ticker or h.fund_code):
            if pending(own):
                row.excluded_reason = "pending"
            elif prices[own] and not cached[own].unusable_reason:
                first = min(prices[own])
                row.own_first_date = first
                row.method = (
                    ("own" if h.ticker else "fund_nav")
                    if first <= s.start + timedelta(days=CARRY_DAYS)
                    else "head_proxy"
                )
            else:
                row.method = (
                    "cash_assumed"
                    if h.asset_class == "BOND_FUND" and h.currency in ("CNY", "CNH")
                    else "head_proxy"
                )
        else:
            row.method = (
                "cash_assumed"
                if h.asset_class == "BOND_FUND" and h.currency in ("CNY", "CNH")
                else "proxy"
            )
        proxy: SeriesSpec | None = None
        if row.method in ("proxy", "head_proxy"):
            primary = proxy_for(h)
            row.proxy_symbol = primary.symbol
            row.proxy_name = primary.name
            if row.method == "head_proxy":
                if h.holding_id not in betas:
                    betas[h.holding_id] = holding_beta(session, h, primary)
                beta, samples = betas[h.holding_id]
                row.beta = f"{beta:.4f}"
                row.beta_samples = samples
            chosen = resolve(primary)
            if isinstance(chosen, str):
                row.method = "excluded"
                row.excluded_reason = chosen
            else:
                proxy = chosen
                row.proxy_symbol = chosen.symbol
                row.proxy_name = chosen.name
                row.price_only = chosen.price_only
                row.proxy_for = primary.symbol if chosen.symbol != primary.symbol else None
        values: dict[date, Decimal] = {}
        if row.method in ("cash", "cash_assumed"):
            values = {d: r for d in days if (r := fx(h.currency, base, d)) is not None}
        elif row.method in ("own", "fund_nav"):
            values = {d: v for d in days if (v := converted(own, h.currency, base, d)) is not None}
        elif row.method == "proxy":
            assert proxy is not None
            values = {
                d: v
                for d in days
                if (v := converted(proxy.key, proxy.currency, base, d)) is not None
            }
        elif row.method == "head_proxy":
            assert proxy is not None
            local = {
                d: float(v)
                for d in days
                if row.own_first_date is not None
                and d >= row.own_first_date
                and (v := converted(own, h.currency, proxy.currency, d)) is not None
            }
            p = {d: float(v) for d in days if (v := price_on(prices[proxy.key], d)) is not None}
            chain = forward_chain(days, p, local, row.own_first_date, betas[h.holding_id][0])
            values = {
                d: Decimal(str(v)) * r
                for d, v in chain.items()
                if (r := fx(proxy.currency, base, d)) is not None
            }
        if row.method != "excluded":
            if s.peak not in values or s.trough not in values:
                row.method = "excluded"
                row.excluded_reason = "data_unavailable"
            else:
                included.append((h, row, values))
        rows.append(row)
    total = sum((h.market_value_base or Decimal(0) for h, _, _ in included), Decimal(0))
    shares = {m: Decimal(0) for m in ("own", "head_proxy", "proxy", "cash", "cash_assumed")}
    curves = []
    for h, row, values in included:
        weight = cast(Decimal, h.market_value_base) / total
        row.weight = ratio(weight)
        shares["own" if row.method == "fund_nav" else row.method] += weight
        curves.append((values, weight))
    approx = shares["head_proxy"] + shares["proxy"] + shares["cash_assumed"]
    pending_value = sum(
        (
            h.market_value_base or Decimal(0)
            for h, row in zip(holdings, rows, strict=True)
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
    portfolio = weighted_path(days, curves)
    count = max(len(portfolio) - 1, 0)
    first_valid = portfolio[0][0] if portfolio else None
    status = (
        "no_holdings"
        if not any(h.market_value_base is not None and h.market_value_base > 0 for h in holdings)
        else "pending"
        if not included
        else "insufficient"
        if count < MIN_RETURNS
        else "ok"
    )
    chosen_benchmark = resolve(spec)
    benchmark_spec = chosen_benchmark if isinstance(chosen_benchmark, SeriesSpec) else None
    bench = [
        (d, float(v))
        for d in days
        if benchmark_spec is not None
        and first_valid is not None
        and d >= first_valid
        and (v := converted(benchmark_spec.key, benchmark_spec.currency, base, d)) is not None
    ]
    bv = dict(bench)
    bench_status = (
        "pending"
        if chosen_benchmark == "pending"
        else "unavailable"
        if s.peak not in bv or s.trough not in bv or len(bench) - 1 < MIN_RETURNS
        else "ok"
    )
    substitutions: list[StressSubstitution] = []
    seen: set[tuple[str, str]] = set()
    price_indexes: set[str] = set()
    for _, row, _ in included:
        if row.proxy_for is not None and row.proxy_symbol is not None:
            pair = (row.proxy_for, row.proxy_symbol)
            if pair not in seen:
                substitutions.append(
                    StressSubstitution(
                        primary=pair[0],
                        symbol=pair[1],
                        name=cast(str, row.proxy_name),
                        price_only=row.price_only,
                    )
                )
                seen.add(pair)
        if row.price_only and row.proxy_symbol is not None:
            price_indexes.add(row.proxy_symbol)
    if bench_status == "ok" and benchmark_spec is not None:
        pair = (spec.symbol, benchmark_spec.symbol)
        if spec.symbol != benchmark_spec.symbol and pair not in seen:
            substitutions.append(
                StressSubstitution(
                    primary=pair[0],
                    symbol=pair[1],
                    name=benchmark_spec.name,
                    price_only=benchmark_spec.price_only,
                )
            )
        if benchmark_spec.price_only:
            price_indexes.add(benchmark_spec.symbol)
    out = StressScenario(
        fx_source=s.fx_source,
        benchmark_symbol=benchmark_spec.symbol if benchmark_spec else spec.symbol,
        benchmark_name=benchmark_spec.name if benchmark_spec else spec.name,
        benchmark_price_only=benchmark_spec.price_only if benchmark_spec else False,
        substitutions=substitutions,
        price_index_symbols=sorted(price_indexes),
        id=s.id,
        peak_date=s.peak,
        trough_date=s.trough,
        window_start=s.start,
        window_end=s.end,
        status=status,
        first_valid_date=first_valid,
        sample_count=count,
        portfolio_value=None,
        shock_return=None,
        shock_amount=None,
        max_drawdown=None,
        max_drawdown_peak=None,
        max_drawdown_trough=None,
        benchmark_status=bench_status,
        benchmark_shock_return=None,
        benchmark_max_drawdown=None,
        contributions=[],
        points=[],
        coverage=coverage,
        proxy_understates=shares["head_proxy"] + shares["proxy"] > 0,
        holdings=rows,
    )
    if status != "ok":
        return out
    pv = dict(portfolio)
    shock = pv[s.trough] / pv[s.peak] - 1
    out.portfolio_value = str(total.quantize(Decimal(".01"), rounding=ROUND_HALF_EVEN))
    out.shock_return = ratio(shock)
    out.shock_amount = str(
        (Decimal(out.shock_return) * Decimal(out.portfolio_value)).quantize(
            Decimal(".01"), rounding=ROUND_HALF_EVEN
        )
    )
    pm = metrics(portfolio, hide_worst_month=True)
    out.max_drawdown = pm.max_drawdown
    out.max_drawdown_peak = pm.max_drawdown_peak
    out.max_drawdown_trough = pm.max_drawdown_trough
    contributions: dict[str, float] = {}
    for (h, _, values), (_, weight) in zip(included, curves, strict=True):
        key = cast(str, h.asset_class)
        contributions[key] = (
            contributions.get(key, 0)
            + float(weight * (values[s.trough] - values[s.peak]) / values[cast(date, first_valid)])
            / pv[s.peak]
        )
    out.contributions = [
        StressContribution(asset_class=k, contribution=ratio(v))
        for k, v in sorted(contributions.items(), key=lambda kv: (kv[1], kv[0]))
    ]
    if bench_status == "ok":
        out.benchmark_shock_return = ratio(bv[s.trough] / bv[s.peak] - 1)
        out.benchmark_max_drawdown = metrics(bench, hide_worst_month=True).max_drawdown
    out.points = [
        StressPoint(
            date=d,
            portfolio=ratio(v / pv[s.peak] - 1),
            benchmark=ratio(bv[d] / bv[s.peak] - 1) if bench_status == "ok" and d in bv else None,
        )
        for d, v in portfolio
    ]
    return out
