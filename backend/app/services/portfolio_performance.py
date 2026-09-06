"""`GET /portfolio/performance` computation core (issue #360 Phase 1).

Reads only what the daily snapshot writer (`portfolio_history.py`) already
computed and stored — this module never re-prices a holding or re-derives
FX for an individual row. The one exception is the portfolio's stored
canonical base currency (`users.base_currency` at capture time) differing
from the request's `base_currency`: that is a single aggregate-level
re-conversion per day (see `_convert_amount`), not a re-valuation.

Approximate EOD TWR (D3 amendment): day t's return marks yesterday's
*filtered* holdings at today's own stored values for the SAME `holding_id`
— quantity changes, new lots, exits, and a holding dropping out of the
current filter are all treated identically as an end-of-day cash flow, not
a return (see `_contribution`).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.portfolio_snapshot_batch import PortfolioSnapshotBatch
from app.models.portfolio_value_snapshot import PortfolioValueSnapshot
from app.services.benchmark_prices import INDEX_YF_TICKERS
from app.services.fx_conversion import to_base
from app.services.portfolio_history import historical_fx_rates_asof
from app.services.user_scope import report_currency_for

_PCT = Decimal("0.0001")
_CENT = Decimal("0.01")

VALID_RANGES = ("1M", "6M", "YTD", "1Y", "5Y", "ALL")

BENCHMARK_NAMES: dict[str, str] = {
    "sp500": "S&P 500",
    "dow30": "Dow 30",
    "nasdaq": "Nasdaq Composite",
}

_ALL_RANGE_SENTINEL = date(2000, 1, 1)


@dataclass
class Filters:
    markets: frozenset[str] | None = None
    groups: frozenset[str] | None = None
    brokers: frozenset[str] | None = None
    accounts: frozenset[str] | None = None

    def matches(self, row: PortfolioValueSnapshot) -> bool:
        if self.markets is not None and row.market not in self.markets:
            return False
        if self.groups is not None and row.portfolio not in self.groups:
            return False
        if self.brokers is not None and row.broker not in self.brokers:
            return False
        return not (self.accounts is not None and row.account not in self.accounts)


@dataclass
class PerformancePoint:
    point_date: date
    value_base: Decimal
    return_pct_cumulative: Decimal
    is_approximate: bool


@dataclass
class PortfolioSeries:
    empty: bool
    start_date: date | None
    end_date: date | None
    points: list[PerformancePoint] = field(default_factory=list)
    quality_flags: list[str] = field(default_factory=list)


@dataclass
class BenchmarkPoint:
    point_date: date
    return_pct_cumulative: Decimal


@dataclass
class BenchmarkSeries:
    index_code: str
    name: str
    start_date: date | None
    points: list[BenchmarkPoint] = field(default_factory=list)


@dataclass
class PerformanceHeader:
    value_base: Decimal
    value_change_base: Decimal
    value_change_pct: Decimal
    label: str = "market_value_change"


@dataclass
class PerformanceResult:
    portfolio: PortfolioSeries
    benchmarks: list[BenchmarkSeries]
    header: PerformanceHeader
    meta: dict[str, object]


def resolve_range(range_key: str, today: date) -> tuple[date, date]:
    if range_key == "1M":
        return today - timedelta(days=30), today
    if range_key == "6M":
        return today - timedelta(days=182), today
    if range_key == "YTD":
        return date(today.year, 1, 1), today
    if range_key == "1Y":
        return today - timedelta(days=365), today
    if range_key == "5Y":
        return today - timedelta(days=365 * 5), today
    if range_key == "ALL":
        return _ALL_RANGE_SENTINEL, today
    raise ValueError(f"unknown range: {range_key!r}")


def _complete_batch_dates(
    session: Session, user_id: uuid.UUID, start_date: date, end_date: date
) -> list[date]:
    rows = session.execute(
        select(PortfolioSnapshotBatch.snapshot_date)
        .where(
            PortfolioSnapshotBatch.user_id == user_id,
            PortfolioSnapshotBatch.status == "complete",
            PortfolioSnapshotBatch.snapshot_date >= start_date,
            PortfolioSnapshotBatch.snapshot_date <= end_date,
        )
        .order_by(PortfolioSnapshotBatch.snapshot_date.asc())
    ).scalars()
    return list(rows)


def _rows_for_dates(
    session: Session, user_id: uuid.UUID, dates: list[date]
) -> dict[date, list[PortfolioValueSnapshot]]:
    if not dates:
        return {}
    rows = session.execute(
        select(PortfolioValueSnapshot).where(
            PortfolioValueSnapshot.user_id == user_id,
            PortfolioValueSnapshot.snapshot_date.in_(dates),
        )
    ).scalars()
    by_date: dict[date, list[PortfolioValueSnapshot]] = {d: [] for d in dates}
    for row in rows:
        by_date[row.snapshot_date].append(row)
    return by_date


def _day_value(rows: list[PortfolioValueSnapshot]) -> Decimal | None:
    """Sum of priced rows' market_value_base. None (insufficient) only when
    the filtered set is non-empty but NOT ONE row has a usable value — an
    empty filtered set is a legitimate zero, not "insufficient"."""
    if not rows:
        return Decimal("0")
    priced = [r.market_value_base for r in rows if r.market_value_base is not None]
    if not priced:
        return None
    return sum(priced, Decimal("0"))


def _is_approximate(rows: list[PortfolioValueSnapshot]) -> bool:
    return any(r.is_backfilled or r.is_fx_fallback or r.data_quality != "ok" for r in rows)


def _contribution(
    prev_row: PortfolioValueSnapshot, curr_row: PortfolioValueSnapshot | None
) -> Decimal | None:
    """Value of `prev_row`'s position marked at `curr_row`'s day (D3
    amendment): "yesterday's quantity/local-value at today's price/FX".
    None when `curr_row` doesn't exist (position exited/relabeled out of
    the current filter) or lacks the fields needed — both cases are
    excluded from V_t_minus and so become an implicit cash flow rather
    than a return, per the TWR spec.
    """
    if curr_row is None or curr_row.market_value_base is None:
        return None
    if (
        prev_row.shares is not None
        and curr_row.shares is not None
        and curr_row.shares != 0
        and curr_row.market_value_base is not None
    ):
        unit_value_base = curr_row.market_value_base / curr_row.shares
        return prev_row.shares * unit_value_base
    if (
        prev_row.shares is None
        and curr_row.shares is None
        and prev_row.current_value is not None
        and curr_row.current_value is not None
        and curr_row.current_value != 0
    ):
        fx_multiplier = curr_row.market_value_base / curr_row.current_value
        return prev_row.current_value * fx_multiplier
    return None


def _twr_day_return(
    prev_rows_by_id: dict[uuid.UUID, PortfolioValueSnapshot],
    curr_rows_by_id: dict[uuid.UUID, PortfolioValueSnapshot],
    v_prev: Decimal,
) -> Decimal | None:
    if v_prev <= 0:
        return None
    contributions = [
        _contribution(prev_row, curr_rows_by_id.get(hid))
        for hid, prev_row in prev_rows_by_id.items()
        if hid is not None
    ]
    v_minus = sum((c for c in contributions if c is not None), Decimal("0"))
    return (v_minus / v_prev) - Decimal("1")


def _convert_amount(
    session: Session, amount: Decimal, from_currency: str, to_currency: str, as_of_date: date
) -> Decimal | None:
    if from_currency == to_currency:
        return amount
    rates = {
        pair: rate for pair, (rate, _d) in historical_fx_rates_asof(session, as_of_date).items()
    }
    return to_base(amount, from_currency, to_currency, rates)


def _build_portfolio_series(
    session: Session,
    user_id: uuid.UUID,
    start_date: date,
    end_date: date,
    filters: Filters,
    twr: bool,
    canonical_currency: str,
    requested_currency: str,
) -> tuple[PortfolioSeries, Decimal, Decimal]:
    dates = _complete_batch_dates(session, user_id, start_date, end_date)
    rows_by_date = _rows_for_dates(session, user_id, dates)

    any_match_in_range = any(
        any(filters.matches(r) for r in rows_by_date.get(d, [])) for d in dates
    )

    filtered_by_date: dict[date, list[PortfolioValueSnapshot]] = {}
    for d in dates:
        filtered_by_date[d] = [r for r in rows_by_date[d] if filters.matches(r)]

    included: list[tuple[date, Decimal, dict[uuid.UUID, PortfolioValueSnapshot], bool]] = []
    for d in dates:
        rows = filtered_by_date[d]
        value = _day_value(rows)
        if value is None:
            continue
        by_id = {r.holding_id: r for r in rows if r.holding_id is not None}
        included.append((d, value, by_id, _is_approximate(rows)))

    if not any_match_in_range:
        empty_series = PortfolioSeries(empty=True, start_date=None, end_date=None)
        return empty_series, Decimal("0"), Decimal("0")

    quality_flags: set[str] = set()
    points: list[PerformancePoint] = []
    ratio = Decimal("1")
    prev_by_id: dict[uuid.UUID, PortfolioValueSnapshot] | None = None
    prev_value: Decimal | None = None

    for idx, (d, value, by_id, approx) in enumerate(included):
        converted_value = _convert_amount(session, value, canonical_currency, requested_currency, d)
        if converted_value is None:
            continue
        converted_value = converted_value.quantize(_CENT, rounding=ROUND_HALF_UP)

        if idx == 0:
            cumulative = Decimal("0")
        elif twr:
            r_t = _twr_day_return(prev_by_id or {}, by_id, prev_value or Decimal("0"))
            if r_t is not None:
                ratio = ratio * (Decimal("1") + r_t)
            cumulative = (ratio - Decimal("1")).quantize(_PCT, rounding=ROUND_HALF_UP)
        else:
            first_value = points[0].value_base if points else converted_value
            cumulative = (
                (converted_value / first_value - Decimal("1")).quantize(
                    _PCT, rounding=ROUND_HALF_UP
                )
                if first_value > 0
                else Decimal("0")
            )

        points.append(
            PerformancePoint(
                point_date=d,
                value_base=converted_value,
                return_pct_cumulative=cumulative,
                is_approximate=approx,
            )
        )
        if approx:
            quality_flags.add(
                "approx_backfill" if any(r.is_backfilled for r in by_id.values()) else "approx_fx"
            )
        prev_by_id = by_id
        prev_value = value

    series = PortfolioSeries(
        empty=False,
        start_date=points[0].point_date if points else None,
        end_date=points[-1].point_date if points else None,
        points=points,
        quality_flags=sorted(quality_flags),
    )
    value_start = points[0].value_base if points else Decimal("0")
    value_end = points[-1].value_base if points else Decimal("0")
    return series, value_start, value_end


def _build_benchmark_series(
    session: Session,
    index_code: str,
    start_date: date,
    end_date: date,
    requested_currency: str,
) -> BenchmarkSeries:
    from app.models.benchmark_price import BenchmarkPrice

    rows = session.execute(
        select(BenchmarkPrice.price_date, BenchmarkPrice.close_price, BenchmarkPrice.currency)
        .where(
            BenchmarkPrice.index_code == index_code,
            BenchmarkPrice.price_date >= start_date,
            BenchmarkPrice.price_date <= end_date,
        )
        .order_by(BenchmarkPrice.price_date.asc())
    ).all()

    points: list[BenchmarkPoint] = []
    base_value: Decimal | None = None
    for price_date, close_price, currency in rows:
        converted = _convert_amount(session, close_price, currency, requested_currency, price_date)
        if converted is None:
            continue
        if base_value is None:
            base_value = converted
            pct = Decimal("0")
        elif base_value > 0:
            pct = (converted / base_value - Decimal("1")).quantize(_PCT, rounding=ROUND_HALF_UP)
        else:
            pct = Decimal("0")
        points.append(BenchmarkPoint(point_date=price_date, return_pct_cumulative=pct))

    return BenchmarkSeries(
        index_code=index_code,
        name=BENCHMARK_NAMES.get(index_code, index_code),
        start_date=points[0].point_date if points else None,
        points=points,
    )


def compute_portfolio_performance(
    session: Session,
    user_id: uuid.UUID,
    *,
    range_key: str,
    benchmark_codes: list[str],
    markets: list[str] | None = None,
    groups: list[str] | None = None,
    brokers: list[str] | None = None,
    accounts: list[str] | None = None,
    twr: bool = True,
    base_currency: str | None = None,
    today: date | None = None,
) -> PerformanceResult:
    today = today or date.today()
    start_date, end_date = resolve_range(range_key, today)

    canonical_currency = report_currency_for(session, user_id, "USD")
    requested_currency = base_currency or canonical_currency

    filters = Filters(
        markets=frozenset(markets) if markets else None,
        groups=frozenset(groups) if groups else None,
        brokers=frozenset(brokers) if brokers else None,
        accounts=frozenset(accounts) if accounts else None,
    )

    portfolio_series, value_start, value_end = _build_portfolio_series(
        session,
        user_id,
        start_date,
        end_date,
        filters,
        twr,
        canonical_currency,
        requested_currency,
    )

    benchmarks = [
        _build_benchmark_series(session, code, start_date, end_date, requested_currency)
        for code in benchmark_codes
        if code in INDEX_YF_TICKERS
    ]

    value_change = value_end - value_start
    if twr and portfolio_series.points:
        value_change_pct = portfolio_series.points[-1].return_pct_cumulative
    elif portfolio_series.points and value_start > 0:
        value_change_pct = ((value_end / value_start) - Decimal("1")).quantize(
            _PCT, rounding=ROUND_HALF_UP
        )
    else:
        value_change_pct = Decimal("0")

    header = PerformanceHeader(
        value_base=value_end,
        value_change_base=value_change,
        value_change_pct=value_change_pct,
        label="market_value_change",
    )

    meta = {
        "range": range_key,
        "twr": twr,
        "base_currency": requested_currency,
        "filters": {
            "markets": sorted(markets) if markets else [],
            "groups": sorted(groups) if groups else [],
            "brokers": sorted(brokers) if brokers else [],
            "accounts": sorted(accounts) if accounts else [],
        },
    }

    return PerformanceResult(
        portfolio=portfolio_series, benchmarks=benchmarks, header=header, meta=meta
    )
