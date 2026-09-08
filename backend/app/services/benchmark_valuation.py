"""As-of index valuation for Portfolio Performance (issue #377).

Bulk-loads index closes and FX, then evaluates one calendar day at a time
with a disclosed 10-calendar-day lookback. GET never fetches market data
or writes rows. Missing days stay null; there is no interpolation, current
FX fallback, future price, or unbounded carry-forward.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.benchmark_price import BenchmarkPrice
from app.models.fx_rate import FxRate
from app.services.fx_conversion import conversion_pairs, to_base

LOOKBACK_DAYS = 10

UnavailableReason = Literal[
    "missing_price",
    "stale_price",
    "invalid_price",
    "missing_fx",
    "stale_fx",
    "invalid_fx",
]
Normalization = Literal["portfolio_start", "own_start", "unavailable"]
ComparisonStatus = Literal[
    "available",
    "baseline_only",
    "no_portfolio",
    "anchor_unavailable",
    "incomplete_window",
]


@dataclass(frozen=True)
class RawClose:
    source_date: date
    close: Decimal
    currency: str


@dataclass(frozen=True)
class DailyValuation:
    evaluation_date: date
    value: Decimal | None
    price_as_of: date | None
    fx_as_of: dict[str, date]
    carried: bool
    unavailable_reason: UnavailableReason | None


def _latest_on_or_before(keys: Sequence[date], as_of: date) -> int | None:
    index = bisect_right(keys, as_of) - 1
    return index if index >= 0 else None


def _daterange(start: date, end: date) -> list[date]:
    if start > end:
        return []
    days: list[date] = []
    cursor = start
    while cursor <= end:
        days.append(cursor)
        cursor += timedelta(days=1)
    return days


def load_index_closes(
    session: Session, index_codes: Sequence[str], range_start: date, range_end: date
) -> dict[str, list[RawClose]]:
    """One in-range query plus one predecessor query, not one query per day."""
    codes = list(dict.fromkeys(index_codes))
    out: dict[str, list[RawClose]] = {code: [] for code in codes}
    if not codes:
        return out
    load_start = range_start - timedelta(days=LOOKBACK_DAYS)
    rows = session.execute(
        select(
            BenchmarkPrice.index_code,
            BenchmarkPrice.price_date,
            BenchmarkPrice.close_price,
            BenchmarkPrice.currency,
        )
        .where(
            BenchmarkPrice.index_code.in_(codes),
            BenchmarkPrice.price_date >= load_start,
            BenchmarkPrice.price_date <= range_end,
        )
        .order_by(BenchmarkPrice.index_code.asc(), BenchmarkPrice.price_date.asc())
    ).all()
    for index_code, price_date, close_price, currency in rows:
        out[index_code].append(RawClose(price_date, close_price, currency))

    latest_before = (
        select(
            BenchmarkPrice.index_code,
            func.max(BenchmarkPrice.price_date).label("price_date"),
        )
        .where(
            BenchmarkPrice.index_code.in_(codes),
            BenchmarkPrice.price_date < load_start,
        )
        .group_by(BenchmarkPrice.index_code)
        .subquery()
    )
    predecessors = session.execute(
        select(
            BenchmarkPrice.index_code,
            BenchmarkPrice.price_date,
            BenchmarkPrice.close_price,
            BenchmarkPrice.currency,
        ).join(
            latest_before,
            (BenchmarkPrice.index_code == latest_before.c.index_code)
            & (BenchmarkPrice.price_date == latest_before.c.price_date),
        )
    ).all()
    for index_code, price_date, close_price, currency in predecessors:
        series = out[index_code]
        if not series or series[0].source_date != price_date:
            series.insert(0, RawClose(price_date, close_price, currency))
    return out


def load_fx_series(
    session: Session, pairs: Sequence[str], range_start: date, range_end: date
) -> dict[str, list[tuple[date, Decimal]]]:
    unique_pairs = list(dict.fromkeys(pairs))
    out: dict[str, list[tuple[date, Decimal]]] = {pair: [] for pair in unique_pairs}
    if not unique_pairs:
        return out
    load_start = range_start - timedelta(days=LOOKBACK_DAYS)
    rows = session.execute(
        select(FxRate.pair, FxRate.rate_date, FxRate.rate)
        .where(
            FxRate.pair.in_(unique_pairs),
            FxRate.rate_date >= load_start,
            FxRate.rate_date <= range_end,
        )
        .order_by(FxRate.pair.asc(), FxRate.rate_date.asc())
    ).all()
    for pair, rate_date, rate in rows:
        out[pair].append((rate_date, rate))

    latest_before = (
        select(FxRate.pair, func.max(FxRate.rate_date).label("rate_date"))
        .where(FxRate.pair.in_(unique_pairs), FxRate.rate_date < load_start)
        .group_by(FxRate.pair)
        .subquery()
    )
    predecessors = session.execute(
        select(FxRate.pair, FxRate.rate_date, FxRate.rate).join(
            latest_before,
            (FxRate.pair == latest_before.c.pair) & (FxRate.rate_date == latest_before.c.rate_date),
        )
    ).all()
    for pair, rate_date, rate in predecessors:
        series = out[pair]
        if not series or series[0][0] != rate_date:
            series.insert(0, (rate_date, rate))
    return out


def _price_reason(close_row: RawClose | None, evaluation_date: date) -> UnavailableReason | None:
    if close_row is None:
        return "missing_price"
    age = (evaluation_date - close_row.source_date).days
    if age > LOOKBACK_DAYS:
        return "stale_price"
    if close_row.close <= 0:
        return "invalid_price"
    return None


def _fx_reason(
    lookups: list[tuple[str, date | None, Decimal | None]], evaluation_date: date
) -> UnavailableReason | None:
    if any(source is None for _pair, source, _rate in lookups):
        return "missing_fx"
    if any(
        source is not None and (evaluation_date - source).days > LOOKBACK_DAYS
        for _pair, source, _rate in lookups
    ):
        return "stale_fx"
    if any(rate is not None and rate <= 0 for _pair, _source, rate in lookups):
        return "invalid_fx"
    return None


def evaluate_index_day(
    evaluation_date: date,
    closes: Sequence[RawClose],
    fx_by_pair: dict[str, list[tuple[date, Decimal]]],
    requested_currency: str,
) -> DailyValuation:
    close_dates = [row.source_date for row in closes]
    close_index = _latest_on_or_before(close_dates, evaluation_date)
    close_row = closes[close_index] if close_index is not None else None
    price_reason = _price_reason(close_row, evaluation_date)
    price_as_of = close_row.source_date if close_row is not None else None
    if price_reason is not None or close_row is None:
        carried = bool(price_as_of is not None and price_as_of < evaluation_date)
        return DailyValuation(
            evaluation_date,
            None,
            price_as_of,
            {},
            carried,
            price_reason or "missing_price",
        )

    pairs = conversion_pairs(close_row.currency, requested_currency)
    if pairs is None:
        carried = close_row.source_date < evaluation_date
        return DailyValuation(
            evaluation_date,
            None,
            close_row.source_date,
            {},
            carried,
            "missing_fx",
        )

    lookups: list[tuple[str, date | None, Decimal | None]] = []
    for pair in pairs:
        series = fx_by_pair.get(pair, [])
        fx_dates = [item[0] for item in series]
        fx_index = _latest_on_or_before(fx_dates, evaluation_date)
        if fx_index is None:
            lookups.append((pair, None, None))
        else:
            source, rate = series[fx_index]
            lookups.append((pair, source, rate))

    fx_as_of = {pair: source for pair, source, _rate in lookups if source is not None}
    fx_reason = _fx_reason(lookups, evaluation_date)
    carried = close_row.source_date < evaluation_date or any(
        source < evaluation_date for source in fx_as_of.values()
    )
    if fx_reason is not None:
        return DailyValuation(
            evaluation_date,
            None,
            close_row.source_date,
            fx_as_of,
            carried,
            fx_reason,
        )

    rates = {pair: rate for pair, _source, rate in lookups if rate is not None}
    value = to_base(close_row.close, close_row.currency, requested_currency, rates)
    if value is None:
        return DailyValuation(
            evaluation_date,
            None,
            close_row.source_date,
            fx_as_of,
            carried,
            "missing_fx",
        )
    return DailyValuation(
        evaluation_date,
        value,
        close_row.source_date,
        fx_as_of,
        carried,
        None,
    )


def evaluate_index_range(
    range_start: date,
    range_end: date,
    closes: Sequence[RawClose],
    fx_by_pair: dict[str, list[tuple[date, Decimal]]],
    requested_currency: str,
) -> dict[date, DailyValuation]:
    if not closes:
        return {}
    earliest_source = min(row.source_date for row in closes)
    scan_start = max(range_start, earliest_source)
    out: dict[date, DailyValuation] = {}
    for day in _daterange(scan_start, range_end):
        out[day] = evaluate_index_day(day, closes, fx_by_pair, requested_currency)
    return out


def required_pairs_for_closes(
    closes_by_index: dict[str, list[RawClose]], requested_currency: str
) -> list[str]:
    pairs: list[str] = []
    seen: set[str] = set()
    for series in closes_by_index.values():
        currencies = {row.currency for row in series}
        for currency in currencies:
            needed = conversion_pairs(currency, requested_currency)
            if not needed:
                continue
            for pair in needed:
                if pair not in seen:
                    seen.add(pair)
                    pairs.append(pair)
    return pairs


def first_source_in_range(
    closes: Sequence[RawClose], range_start: date, range_end: date
) -> date | None:
    for row in closes:
        if range_start <= row.source_date <= range_end:
            return row.source_date
    return None
