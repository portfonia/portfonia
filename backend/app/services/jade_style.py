"""Read-only returns-based style analysis of the fixed three-month replay."""

from datetime import date, timedelta
from decimal import Decimal
from math import sqrt
from statistics import stdev, variance
from uuid import UUID

import numpy as np
from numpy.typing import NDArray
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.jade_price import JadePricePoint, JadePriceSeries
from app.schemas.jade import JadeStyleOut, StyleFit, StylePoint, StyleWeight
from app.services.benchmark_valuation import load_fx_series
from app.services.fx_conversion import conversion_pairs, to_base
from app.services.jade_replay import build_replay, price_on, ratio
from app.services.jade_replay_config import (
    CARRY_DAYS,
    STYLE_BASIS,
    STYLE_HORIZON_DAYS,
    STYLE_KEYS,
    STYLE_LOW_FIT,
    STYLE_MIN_RETURNS,
    STYLE_RANGE,
    STYLE_SOLVER_TOL,
)


def overlapping_returns(values: list[tuple[date, float]]) -> list[float]:
    h = STYLE_HORIZON_DAYS
    return [values[i][1] / values[i - h][1] - 1 for i in range(h, len(values))]


def constrained_weights(x: NDArray[np.float64], y: NDArray[np.float64]) -> NDArray[np.float64]:
    """Primal active set on the simplex; ties follow basis order."""
    q, c = x.T @ x, x.T @ y
    k = x.shape[1]
    first = int(np.argmin(np.sum((y[:, None] - x) ** 2, axis=0)))
    w = np.zeros(k)
    w[first] = 1
    free = {first}
    for _ in range(10 * k):
        ix = sorted(free)
        size = len(ix)
        a = np.block(
            [[q[np.ix_(ix, ix)], np.ones((size, 1))], [np.ones((1, size)), np.zeros((1, 1))]]
        )
        solution = np.linalg.solve(a, np.r_[c[ix], 1])
        z, nu = solution[:-1], solution[-1]
        if np.all(z >= -STYLE_SOLVER_TOL):
            w[:] = 0
            w[ix] = z
            fixed = sorted(set(range(k)) - free)
            multipliers = q @ w - c + nu
            if not fixed or min(multipliers[fixed]) >= -STYLE_SOLVER_TOL:
                return np.maximum(w, 0)
            release = min(fixed, key=lambda i: float(multipliers[i]))
            free.add(release)
        else:
            alpha = min(
                1.0,
                min(
                    float(w[i] / (w[i] - value))
                    for i, value in zip(ix, z, strict=True)
                    if value < 0
                ),
            )
            w[ix] += alpha * (z - w[ix])
            for i in ix:
                if w[i] <= STYLE_SOLVER_TOL:
                    w[i] = 0
                    free.remove(i)
    raise RuntimeError("Style active-set solver exceeded its iteration limit")


def low_fit(r_squared: float | None) -> bool:
    return r_squared is not None and r_squared < STYLE_LOW_FIT


def fit(
    x: NDArray[np.float64], y: NDArray[np.float64], weights: NDArray[np.float64] | None = None
) -> StyleFit:
    w = constrained_weights(x, y) if weights is None else weights
    residuals = (y - x @ w).tolist()
    var_y = variance(y.tolist())
    r_squared = 1 - variance(residuals) / var_y if var_y != 0 else None
    return StyleFit(
        weights=[
            StyleWeight(symbol=e.symbol, weight=ratio(float(v)))
            for e, v in zip(STYLE_BASIS[: len(w)], w, strict=True)
        ],
        r_squared=ratio(r_squared) if r_squared is not None else None,
        residual_vol=ratio(stdev(residuals) * sqrt(252 / STYLE_HORIZON_DAYS)),
        low_fit=low_fit(r_squared),
    )


def compute_style(
    session: Session, user_id: UUID, base_currency: str, benchmark: str
) -> JadeStyleOut:
    b = build_replay(session, user_id, base_currency, benchmark, STYLE_RANGE)
    out = JadeStyleOut(
        status=b.out.status,
        base_currency=b.out.base_currency,
        benchmark=b.out.benchmark,
        benchmark_symbol=b.out.benchmark_symbol,
        benchmark_name=b.out.benchmark_name,
        benchmark_status=b.out.benchmark_status,
        window_start=b.out.window_start,
        window_end=b.out.window_end,
        first_valid_date=None,
        sample_count=0,
        horizon_days=STYLE_HORIZON_DAYS,
        min_samples=STYLE_MIN_RETURNS,
        portfolio=None,
        benchmark_fit=None,
        points=[],
        coverage=b.out.coverage,
        proxy_inflates_fit=Decimal(b.out.coverage.proxy_share)
        + Decimal(b.out.coverage.head_proxy_share)
        > 0,
    )
    if out.status in ("no_holdings", "pending"):
        return out
    cached = {
        s.series_key: s
        for s in session.scalars(
            select(JadePriceSeries).where(JadePriceSeries.series_key.in_(STYLE_KEYS))
        )
    }
    if any(key not in cached or cached[key].last_attempt_on is None for key in STYLE_KEYS):
        out.status = "pending"
        return out
    start = out.window_start - timedelta(days=CARRY_DAYS)
    prices: dict[str, dict[date, Decimal]] = {key: {} for key in STYLE_KEYS}
    for key, day, close in session.execute(
        select(JadePricePoint.series_key, JadePricePoint.trade_date, JadePricePoint.close).where(
            JadePricePoint.series_key.in_(STYLE_KEYS),
            JadePricePoint.trade_date.between(start, out.window_end),
        )
    ):
        prices[key][day] = close
    pairs = {p for e in STYLE_BASIS for p in conversion_pairs(e.currency, base_currency) or []}
    fx = {
        p: dict(rows)
        for p, rows in load_fx_series(session, sorted(pairs), start, out.window_end).items()
    }
    basis: dict[date, list[float]] = {}
    for day in sorted({d for d, _ in b.portfolio} | {d for d, _ in b.benchmark}):
        rates = {p: r for p, rows in fx.items() if (r := price_on(rows, day)) is not None}
        values = []
        for e in STYLE_BASIS:
            price = price_on(prices["yf:" + e.symbol], day)
            rate = to_base(Decimal(1), e.currency, base_currency, rates)
            if price is None or rate is None:
                break
            values.append(float(price * rate))
        if len(values) == len(STYLE_BASIS):
            basis[day] = values

    def samples(
        values: list[tuple[date, float]],
    ) -> tuple[
        list[tuple[date, float]], NDArray[np.float64], NDArray[np.float64], NDArray[np.float64]
    ]:
        common = [(d, v) for d, v in values if d in basis]
        levels = np.array([basis[d] for d, _ in common], dtype=np.float64).reshape(
            (-1, len(STYLE_BASIS))
        )
        h = STYLE_HORIZON_DAYS
        x = levels[h:] / levels[:-h] - 1
        y = np.array(overlapping_returns(common), dtype=np.float64)
        return common, levels, x, y

    common, levels, x, y = samples(b.portfolio)
    out.sample_count = len(y)
    out.first_valid_date = common[0][0] if common else None
    if b.out.status == "insufficient" or len(y) < STYLE_MIN_RETURNS:
        out.status = "insufficient"
        return out
    out.status = "ok"
    w = constrained_weights(x, y)
    out.portfolio = fit(x, y, w)
    out.points = [
        StylePoint(
            date=d,
            portfolio=ratio(v / common[0][1] - 1),
            style_mix=ratio(float((levels[i] / levels[0]) @ w - 1)),
        )
        for i, (d, v) in enumerate(common)
    ]
    if b.out.benchmark_status == "ok":
        _, _, bx, by = samples(b.benchmark)
        out.benchmark_status = "ok" if len(by) >= STYLE_MIN_RETURNS else "unavailable"
        if out.benchmark_status == "ok":
            out.benchmark_fit = fit(bx, by)
    elif b.out.benchmark_status != "pending":
        out.benchmark_status = "unavailable"
    return out
