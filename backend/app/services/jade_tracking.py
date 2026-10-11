"""Read-only comparison against a user's daily-rebalanced reference."""

from datetime import date, timedelta
from decimal import Decimal
from itertools import pairwise
from math import sqrt
from statistics import stdev
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.jade_price import JadePricePoint, JadePriceSeries
from app.models.user import User
from app.schemas.jade import (
    JadeBenchmarkOut,
    JadeTrackingOut,
    TrackingComponent,
    TrackingMetrics,
    TrackingPoint,
)
from app.services.benchmark_valuation import load_fx_series
from app.services.fx_conversion import conversion_pairs, to_base
from app.services.jade_replay import build_replay, price_on, ratio
from app.services.jade_replay_config import (
    CARRY_DAYS,
    CASH_COMPONENT,
    DEFAULT_TRACKING_REFERENCE,
    TRACKING_COMPONENTS,
    TRACKING_HORIZON_DAYS,
    TRACKING_MIN_SAMPLES,
    ReplayRange,
)


def reference_index(values: list[list[float]], weights: list[float]) -> list[float]:
    """Daily constant-weight returns on the ordered common dates."""
    index = [1.0]
    for previous, current in pairwise(values):
        daily = sum(
            w * (now / before - 1)
            for w, now, before in zip(weights, current, previous, strict=True)
        )
        index.append(index[-1] * (1 + daily))
    return index


def compute_tracking(
    session: Session, user_id: UUID, base_currency: str, range_key: ReplayRange
) -> JadeTrackingOut:
    user = session.get(User, user_id)
    assert user is not None
    components = (
        JadeBenchmarkOut.model_validate({**user.jade_benchmark, "saved": True}).components
        if user.jade_benchmark is not None
        else [TrackingComponent(symbol=s, weight=w) for s, w in DEFAULT_TRACKING_REFERENCE]
    )
    b = build_replay(session, user_id, base_currency, "sp500", range_key)
    out = JadeTrackingOut(
        status=b.out.status,
        range=b.out.range,
        base_currency=b.out.base_currency,
        window_start=b.out.window_start,
        window_end=b.out.window_end,
        first_valid_date=b.out.first_valid_date,
        components=components,
        benchmark_saved=user.jade_benchmark is not None,
        benchmark_status=None,
        sample_count=0,
        horizon_days=TRACKING_HORIZON_DAYS,
        min_samples=TRACKING_MIN_SAMPLES,
        metrics=None,
        points=[],
        coverage=b.out.coverage,
        proxy_note=Decimal(b.out.coverage.proxy_share) + Decimal(b.out.coverage.head_proxy_share)
        > 0,
    )
    if out.status != "ok":
        return out
    specs = {e.symbol: e for e in TRACKING_COMPONENTS}
    etfs = [specs[c.symbol] for c in components if c.symbol != CASH_COMPONENT]
    keys = {"yf:" + e.symbol for e in etfs}
    cached = {
        s.series_key: s
        for s in session.scalars(
            select(JadePriceSeries).where(JadePriceSeries.series_key.in_(keys))
        )
    }
    if any(k not in cached or cached[k].last_attempt_on is None for k in keys):
        out.benchmark_status = "pending"
        return out
    start = out.window_start - timedelta(days=CARRY_DAYS)
    prices: dict[str, dict[date, Decimal]] = {key: {} for key in keys}
    for key, day, close in session.execute(
        select(JadePricePoint.series_key, JadePricePoint.trade_date, JadePricePoint.close).where(
            JadePricePoint.series_key.in_(keys),
            JadePricePoint.trade_date.between(start, out.window_end),
        )
    ):
        prices[key][day] = close
    pairs = {p for e in etfs for p in conversion_pairs(e.currency, base_currency) or []}
    fx = {
        p: dict(rows)
        for p, rows in load_fx_series(session, sorted(pairs), start, out.window_end).items()
    }
    common = []
    values = []
    for day, portfolio in b.portfolio:
        rates = {p: r for p, rows in fx.items() if (r := price_on(rows, day)) is not None}
        row = []
        for component in components:
            if component.symbol == CASH_COMPONENT:
                row.append(1.0)
                continue
            e = specs[component.symbol]
            price = price_on(prices["yf:" + e.symbol], day)
            rate = to_base(Decimal(1), e.currency, base_currency, rates)
            if price is None or rate is None:
                break
            row.append(float(price * rate))
        if len(row) == len(components):
            common.append((day, portfolio))
            values.append(row)
    if not common or common[0][0] != b.portfolio[0][0] or common[-1][0] != b.portfolio[-1][0]:
        out.benchmark_status = "unavailable"
        return out
    out.benchmark_status = "ok"
    index = reference_index(values, [c.weight / 100 for c in components])
    p = [v for _, v in common]
    pr = p[-1] / p[0] - 1
    br = index[-1] - 1
    days = (common[-1][0] - common[0][0]).days
    h = TRACKING_HORIZON_DAYS
    active = [p[i] / p[i - h] - index[i] / index[i - h] for i in range(h, len(common))]
    out.sample_count = max(len(common) - h, 0)
    te = None if out.sample_count < TRACKING_MIN_SAMPLES else ratio(stdev(active) * sqrt(252 / h))
    out.metrics = TrackingMetrics(
        portfolio_return=ratio(pr),
        benchmark_return=ratio(br),
        excess_return=ratio(pr - br),
        annualized_excess=ratio((1 + pr) ** (365.25 / days) - (1 + br) ** (365.25 / days)),
        tracking_error=te,
    )
    out.points = [
        TrackingPoint(date=day, excess=ratio(v / p[0] - index[i]))
        for i, (day, v) in enumerate(common)
    ]
    return out
