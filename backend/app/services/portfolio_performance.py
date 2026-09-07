"""`GET /portfolio/performance` computation core (issue #360 Phase 1).

Mostly reads what the daily snapshot writer (`portfolio_history.py`)
already computed and stored, at two levels of aggregate re-conversion: the
portfolio's stored canonical base currency (`users.base_currency` at
capture time) differing from the request's `base_currency` re-converts the
already-aggregated per-day totals once per day (`_convert_amount`), never
per holding.

The one place this module DOES re-price an individual position is the TWR
mark for a holding that has no snapshot row at all on the day being marked
— a full exit, or a holding row deleted/replaced entirely
(`_reprice_from_source`, reusing `portfolio_history.historical_price`/
`historical_fx_rates_asof`). This was added after review 5124107298 found
that simply excluding such a holding from V_t_minus turned a solo full exit
into an approximately -100% "return" instead of the cash-flow-neutral
price move the D3 amendment requires — see `_contribution`'s docstring for
the full reasoning and when the fast path (reusing an existing day-t row)
applies instead.

Approximate EOD TWR (D3 amendment): day t's return marks yesterday's
*filtered* holdings at today's price/FX for the SAME `holding_id` —
quantity changes, new lots, exits, and a holding dropping out of the
current filter are all treated identically as an end-of-day cash flow, not
a return.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.portfolio_snapshot_batch import PortfolioSnapshotBatch
from app.models.portfolio_value_snapshot import PortfolioValueSnapshot
from app.services.benchmark_prices import INDEX_YF_TICKERS
from app.services.fx_conversion import to_base
from app.services.instrument_symbols import normalize_legacy_ticker
from app.services.portfolio_history import historical_fx_rates_asof, historical_price
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
        # is_backfilled rows are legacy/known-bad history (issue #366 —
        # the retired composition-replay backfill) and must never re-enter
        # any portfolio aggregate, TWR link, or "any match" empty check
        # regardless of dimension filters, even before a production purge
        # removes them outright.
        if row.is_backfilled:
            return False
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
    # User-level (unfiltered), first real non-backfilled complete-batch
    # snapshot day — the "since tracking" anchor (issue #366 / vault §6).
    tracking_start: date | None = None
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
    # False when this benchmark has no point inside the common compare
    # window (issue #366 D7) — `points` is then cleared to [] rather than
    # left self-normalized over its own, unrelated window.
    comparable: bool = True


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


def _tracking_start(session: Session, user_id: uuid.UUID) -> date | None:
    """Earliest `snapshot_date` with a `complete` batch and at least one
    non-`is_backfilled` row for this user — unfiltered by market/group/
    broker/account and unbounded by the requested range (issue #366 /
    vault §6, superseding the retired `_earliest_usable_start` composition-
    replay signal that conflated "ticker price history available" with
    "this position was actually being tracked"). `None` when the user has
    never produced a real snapshot."""
    return session.execute(
        select(func.min(PortfolioValueSnapshot.snapshot_date))
        .select_from(PortfolioValueSnapshot)
        .join(
            PortfolioSnapshotBatch,
            (PortfolioSnapshotBatch.user_id == PortfolioValueSnapshot.user_id)
            & (PortfolioSnapshotBatch.snapshot_date == PortfolioValueSnapshot.snapshot_date),
        )
        .where(
            PortfolioValueSnapshot.user_id == user_id,
            PortfolioValueSnapshot.is_backfilled.is_(False),
            PortfolioSnapshotBatch.status == "complete",
        )
    ).scalar_one_or_none()


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


def _day_currency(rows: list[PortfolioValueSnapshot]) -> str | None:
    """The currency `market_value_base` is actually denominated in for one
    (user, day)'s rows. All of a day's rows share exactly one value — one
    `write_user_snapshot` call resolves `report_currency_for` ONCE per day
    (issue #367 review finding A, blacktomb42): a user's `users.
    base_currency` preference can change BETWEEN two capture days
    (`PATCH /me/report-currency`), so a single global "canonical currency"
    read once per REQUEST — the pre-fix behavior — silently treated two
    different days' `market_value_base` numbers as the same unit whenever
    the preference changed in between, corrupting both raw MV and TWR
    percentages. `None` only when the day has no rows at all (a true
    zero-holdings day, D5) — the caller's $0 value converts to $0 in any
    currency, so no currency is needed for it."""
    return rows[0].base_currency if rows else None


def _is_approximate(rows: list[PortfolioValueSnapshot]) -> bool:
    return any(r.is_backfilled or r.is_fx_fallback or r.data_quality != "ok" for r in rows)


def _reprice_from_source(
    session: Session,
    prev_row: PortfolioValueSnapshot,
    day_t: date,
    canonical_currency: str,
) -> Decimal | None:
    """Reprice `prev_row`'s position at `day_t` directly from
    `price_snapshots`/`fx_rates`, for a holding with NO stored snapshot row
    on `day_t` at all (a full exit, or a holding deleted/replaced entirely —
    review 5124107298 finding 1 / PR #363).

    This is the fallback path only — the fast path in `_contribution`
    below reuses an existing day-t row (whether or not it currently passes
    the active filter) without ever reaching here. Returns None when the
    position genuinely can't be priced (matches D5's "insufficient"
    contribution, excluded from V_t_minus) — that is now the ONLY reason a
    holding drops out of V_t_minus; a bare structural absence on day_t no
    longer does.
    """
    if prev_row.shares is not None:
        key = prev_row.ticker or prev_row.fund_code
        if not key:
            return None
        priced = historical_price(session, normalize_legacy_ticker(key), day_t)
        if priced is None:
            return None
        price, _trade_date = priced
        local_value = prev_row.shares * price
    else:
        if prev_row.current_value is None:
            return None
        local_value = prev_row.current_value

    if prev_row.currency == canonical_currency:
        return local_value
    rates = {pair: rate for pair, (rate, _d) in historical_fx_rates_asof(session, day_t).items()}
    return to_base(local_value, prev_row.currency, canonical_currency, rates)


def _contribution(
    session: Session,
    prev_row: PortfolioValueSnapshot,
    curr_row: PortfolioValueSnapshot | None,
    day_t: date,
    canonical_currency: str,
) -> Decimal | None:
    """Value of `prev_row`'s position marked at `curr_row`'s day (D3
    amendment): "yesterday's quantity/local-value at today's price/FX".

    `curr_row` is the day-t row for the SAME `holding_id`, regardless of
    whether it currently passes the active filter (a holding relabeled out
    of the current sub-portfolio view is D8's "shows up as an outflow", not
    a price move — its own stored day-t row is still the right mark). This
    falls back to repricing directly from `price_snapshots`/`fx_rates`
    (`_reprice_from_source`) in two cases: NO day-t row exists at all (full
    exit, or the holding row itself was deleted/replaced — review
    5124107298 finding 1), or a day-t row exists but carries `shares == 0`
    (re-review leftover: a degenerate zero-share row has no usable
    per-share price to derive a mark from, the same unpriceable situation
    as no row at all — never simply excluded, which is what let finding 1
    happen in the first place).
    """
    if curr_row is None:
        return _reprice_from_source(session, prev_row, day_t, canonical_currency)
    if curr_row.market_value_base is None:
        return None
    if curr_row.shares == Decimal("0"):
        return _reprice_from_source(session, prev_row, day_t, canonical_currency)
    if prev_row.shares is not None and curr_row.shares is not None:
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
    session: Session,
    prev_rows_by_id: dict[uuid.UUID, PortfolioValueSnapshot],
    curr_rows_by_id_all: dict[uuid.UUID, PortfolioValueSnapshot],
    v_prev: Decimal,
    day_t: date,
    canonical_currency: str,
) -> Decimal | None:
    if v_prev <= 0:
        return None
    contributions = [
        _contribution(session, prev_row, curr_rows_by_id_all.get(hid), day_t, canonical_currency)
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
    requested_currency: str,
    tracking_start: date | None,
) -> tuple[PortfolioSeries, Decimal, Decimal]:
    dates = _complete_batch_dates(session, user_id, start_date, end_date)
    rows_by_date = _rows_for_dates(session, user_id, dates)

    # Drop days whose ONLY rows are `is_backfilled` (issue #366 legacy-safety
    # path — production purge should remove these outright, but the read
    # path must not depend on that). Distinct from a real zero-holdings day
    # (no rows at all, a legitimate $0 per `_day_value`'s docstring) and from
    # a dimension filter excluding every row (also a legitimate $0, D8's
    # "sold lot" case) — a day with rows that are ALL backfilled has no real
    # tracked data at all and must not appear as a fabricated $0 point.
    dates = [
        d for d in dates if not rows_by_date[d] or any(not r.is_backfilled for r in rows_by_date[d])
    ]

    # Review 5563537095/blacktomb42 finding 1 (issue #367): a date BEFORE
    # the filtered dimension's own first real appearance must be excluded
    # from the series entirely, not read as a legitimate $0 — that reading
    # is reserved for a date AT OR AFTER the filter's first real match
    # where that day's rows happen not to match (D8's "sold lot"/renamed
    # case, `test_filter_on_historical_account_keeps_sold_lot`). Without
    # this, a newly added sub-account (or any dimension value that starts
    # existing partway through `tracking_start`..`range_end`) inherited an
    # unrelated OTHER account's earlier start date via `_day_value([])`'s
    # "empty filtered set = $0" rule, which in turn gave the common-window
    # benchmark rebase (issue #366 D7) the wrong anchor.
    matched_dates = [d for d in dates if any(filters.matches(r) for r in rows_by_date.get(d, []))]
    any_match_in_range = bool(matched_dates)
    if any_match_in_range:
        dates = [d for d in dates if d >= matched_dates[0]]

    filtered_by_date: dict[date, list[PortfolioValueSnapshot]] = {}
    all_by_id_by_date: dict[date, dict[uuid.UUID, PortfolioValueSnapshot]] = {}
    for d in dates:
        filtered_by_date[d] = [r for r in rows_by_date[d] if filters.matches(r)]
        # ALL of that day's rows keyed by holding_id, regardless of dimension
        # filter — the TWR mark for a holding relabeled out of the current
        # filter still uses its own stored day-t row (D8: a relabel is an
        # outflow from this view, not a price move); only a holding with no
        # day-t row at all falls back to repricing from source (see
        # `_contribution`/`_reprice_from_source`). `is_backfilled` rows are
        # excluded here too (issue #366) — known-bad history must never
        # supply a TWR mark, even as a fallback.
        all_by_id_by_date[d] = {
            r.holding_id: r
            for r in rows_by_date[d]
            if r.holding_id is not None and not r.is_backfilled
        }

    included: list[tuple[date, Decimal, dict[uuid.UUID, PortfolioValueSnapshot], bool, str]] = []
    for d in dates:
        rows = filtered_by_date[d]
        value = _day_value(rows)
        if value is None:
            continue
        by_id = {r.holding_id: r for r in rows if r.holding_id is not None}
        # The day's OWN recorded currency (issue #367 finding A) — derived
        # from the UNFILTERED day rows, since one write call resolves one
        # currency for every holding that day regardless of which pass the
        # active dimension filter; falls back to `requested_currency` only
        # for a true zero-holdings day (no rows at all — $0 either way).
        day_currency = _day_currency(rows_by_date[d]) or requested_currency
        included.append((d, value, by_id, _is_approximate(rows), day_currency))

    if not any_match_in_range:
        empty_series = PortfolioSeries(
            empty=True, start_date=None, end_date=None, tracking_start=tracking_start
        )
        return empty_series, Decimal("0"), Decimal("0")

    quality_flags: set[str] = set()
    points: list[PerformancePoint] = []
    ratio = Decimal("1")
    prev_by_id: dict[uuid.UUID, PortfolioValueSnapshot] | None = None
    prev_value: Decimal | None = None
    prev_currency: str | None = None

    for idx, (d, value, by_id, approx, day_currency) in enumerate(included):
        converted_value = _convert_amount(session, value, day_currency, requested_currency, d)
        if converted_value is None:
            continue
        converted_value = converted_value.quantize(_CENT, rounding=ROUND_HALF_UP)

        if idx == 0:
            cumulative = Decimal("0")
        elif twr:
            # v_prev was aggregated under YESTERDAY's own currency
            # (`prev_currency`) — issue #367 finding A: `_contribution`'s
            # fast path derives today's per-share value from `curr_row.
            # market_value_base`, which is in TODAY's currency
            # (`day_currency`). Mixing the two units together (the pre-fix
            # behavior, both silently assumed to be one global "canonical"
            # currency) turned a mere `PATCH /me/report-currency` change
            # into a fictional TWR swing with no real market move behind
            # it. Re-expressing v_prev in today's currency first keeps the
            # ratio r_t = v_minus/v_prev unit-consistent regardless of
            # whether the user's preference changed between the two days.
            v_prev_today = _convert_amount(
                session, prev_value or Decimal("0"), prev_currency or day_currency, day_currency, d
            )
            r_t = (
                _twr_day_return(
                    session,
                    prev_by_id or {},
                    all_by_id_by_date[d],
                    v_prev_today,
                    d,
                    day_currency,
                )
                if v_prev_today is not None
                else None
            )
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
        prev_currency = day_currency

    series = PortfolioSeries(
        empty=False,
        start_date=points[0].point_date if points else None,
        end_date=points[-1].point_date if points else None,
        tracking_start=tracking_start,
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


def _rebase_benchmark_to_window(
    series: BenchmarkSeries, compare_start: date, compare_end: date
) -> BenchmarkSeries:
    """Re-anchor an independently-normalized benchmark series (0% at its own
    first point over the full requested range) to the portfolio's common
    compare window (issue #366 D7): `compare_start = max(range_start,
    tracking_start, first_portfolio_point_in_filtered_series)`, which
    `_build_portfolio_series` already guarantees equals the portfolio
    series' own `start_date` whenever it is non-empty (real data can never
    predate `tracking_start`, and the DB query already bounds it to
    `range_start`).

    `compare_end` is the caller-computed `min(range_end,
    portfolio_series.end_date)` — NOT the raw requested range end. Review
    5563537095/blacktomb42 finding 2 (issue #367): capping only at the raw
    range end let a benchmark point that exists AFTER the portfolio's own
    real last day (e.g. the portfolio's tracked history ends before the
    request's `range_end`) leak into the window with `comparable=True`,
    comparing the portfolio against a period it has no data for at all.

    Rebasing from the already-computed cumulative % (rather than
    re-fetching raw prices) is exact: old_pct(t) = price(t)/price(base) - 1,
    so (1+old_pct(t)) / (1+old_pct(new_base)) = price(t)/price(new_base) =
    1 + new_pct(t).

    `series.start_date` (the benchmark's own true earliest data point in
    the full requested range) is preserved for disclosure even though
    `points` is clipped — the contract requires each series to still
    report where its own data begins, without presenting that as a
    head-to-head comparison window.
    """
    windowed = [p for p in series.points if compare_start <= p.point_date <= compare_end]
    if not windowed:
        return BenchmarkSeries(
            index_code=series.index_code,
            name=series.name,
            start_date=series.start_date,
            points=[],
            comparable=False,
        )
    base_multiplier = Decimal("1") + windowed[0].return_pct_cumulative
    if base_multiplier == 0:
        return BenchmarkSeries(
            index_code=series.index_code,
            name=series.name,
            start_date=series.start_date,
            points=[],
            comparable=False,
        )
    rebased = [
        BenchmarkPoint(
            point_date=p.point_date,
            return_pct_cumulative=(
                (Decimal("1") + p.return_pct_cumulative) / base_multiplier - Decimal("1")
            ).quantize(_PCT, rounding=ROUND_HALF_UP),
        )
        for p in windowed
    ]
    return BenchmarkSeries(
        index_code=series.index_code,
        name=series.name,
        start_date=series.start_date,
        points=rebased,
        comparable=True,
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

    tracking_start = _tracking_start(session, user_id)

    portfolio_series, value_start, value_end = _build_portfolio_series(
        session,
        user_id,
        start_date,
        end_date,
        filters,
        twr,
        requested_currency,
        tracking_start,
    )

    benchmarks = [
        _build_benchmark_series(session, code, start_date, end_date, requested_currency)
        for code in benchmark_codes
        if code in INDEX_YF_TICKERS
    ]

    # Common compare window (issue #366 D7): once the portfolio series is
    # non-empty, its own `start_date` already equals
    # max(range_start, tracking_start, first_filtered_point) — real rows
    # can't predate tracking_start, and the DB query already bounds
    # everything to range_start. Re-anchor each benchmark's independently-
    # normalized-over-the-full-range series to that same start so cumulative
    # % is never read as head-to-head outperformance across unequal windows.
    # The end is capped at the portfolio's own last real day, NOT the raw
    # requested range end (review 5563537095/blacktomb42 finding 2, issue
    # #367) — a benchmark point after the portfolio's real history ends
    # must not silently compare against a period the portfolio has no data
    # for at all. When the portfolio has no matching history, benchmarks
    # stay self-normalized over the full requested range (no comparison
    # target exists to co-normalize against — §1 requirement 5).
    if (
        not portfolio_series.empty
        and portfolio_series.start_date is not None
        and portfolio_series.end_date is not None
    ):
        compare_end = min(end_date, portfolio_series.end_date)
        benchmarks = [
            _rebase_benchmark_to_window(b, portfolio_series.start_date, compare_end)
            for b in benchmarks
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
