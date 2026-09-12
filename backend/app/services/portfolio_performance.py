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
current filter (market/broker snapshot-time D8) are all treated identically
as an end-of-day cash flow, not a return. Group/account regroup is current
attribution (issue #371): the holding stays in the filtered set for its
whole tracking history, so it is not an outflow.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.models.holding import Holding
from app.models.portfolio_snapshot_batch import PortfolioSnapshotBatch
from app.models.portfolio_value_snapshot import PortfolioValueSnapshot
from app.services.asset_class_config import VALID_ASSET_CLASSES
from app.services.benchmark_prices import INDEX_YF_TICKERS
from app.services.benchmark_valuation import (
    LOOKBACK_DAYS,
    ComparisonStatus,
    DailyValuation,
    Normalization,
    RawClose,
    evaluate_index_day,
    evaluate_index_range,
    first_source_in_range,
    load_fx_series,
    load_index_closes,
    required_pairs_for_closes,
)
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
    "csi300": "CSI 300",
}

_ALL_RANGE_SENTINEL = date(2000, 1, 1)

# Issue #433: fixed display order for the closed asset_class taxonomy, used
# only to keep the allocation chart's color-per-class mapping stable across
# dates/ranges (never rank/value-sorted). Must stay a permutation of
# `VALID_ASSET_CLASSES` — checked once at import time so a taxonomy change
# in asset_class_config.py fails loudly here too, instead of silently
# dropping a new class from every allocation response.
ASSET_CLASS_ORDER: tuple[str, ...] = (
    "STOCK",
    "EQUITY_US_BROAD",
    "EQUITY_US_TECH",
    "EQUITY_DM",
    "EQUITY_CN",
    "EQUITY_EM",
    "EQUITY_BROAD",
    "REIT",
    "PRECIOUS_METALS",
    "ENERGY",
    "COMMODITY",
    "BOND_FUND",
    "CASH_EQUIV",
)
assert set(ASSET_CLASS_ORDER) == VALID_ASSET_CLASSES, (
    "ASSET_CLASS_ORDER drifted from VALID_ASSET_CLASSES"
)

MonthlyPartialReason = Literal["range_start", "tracking_start", "month_to_date"]


@dataclass(frozen=True)
class _OrgLabels:
    portfolio: str | None
    account: str | None


@dataclass
class Filters:
    markets: frozenset[str] | None = None
    groups: frozenset[str] | None = None
    brokers: frozenset[str] | None = None
    accounts: frozenset[str] | None = None
    # Issue #371 / vault D8: group/account match live holdings (or the
    # last-snapshot fallback for a cleared holding_id), not each row's
    # denorm. Populated by `_attach_org_attribution` when either dim is
    # filtered; unused when both are None.
    live_labels: dict[uuid.UUID, _OrgLabels] = field(default_factory=dict)
    last_snapshot_labels: dict[uuid.UUID, _OrgLabels] = field(default_factory=dict)

    def _org_labels(self, row: PortfolioValueSnapshot) -> _OrgLabels:
        hid = row.holding_id
        if hid is not None:
            live = self.live_labels.get(hid)
            if live is not None:
                return live
            last = self.last_snapshot_labels.get(hid)
            if last is not None:
                return last
        return _OrgLabels(portfolio=row.portfolio, account=row.account)

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
        if self.brokers is not None and row.broker not in self.brokers:
            return False
        org = self._org_labels(row)
        if self.groups is not None and org.portfolio not in self.groups:
            return False
        return not (self.accounts is not None and org.account not in self.accounts)


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
    return_pct_cumulative: Decimal | None
    price_as_of: date | None = None
    fx_as_of: dict[str, date] = field(default_factory=dict)
    carried: bool = False
    unavailable_reason: str | None = None


@dataclass
class BenchmarkSeries:
    index_code: str
    name: str
    start_date: date | None
    points: list[BenchmarkPoint] = field(default_factory=list)
    # Comparison eligibility (issue #377) is independent of whether the
    # series is displayable. A non-comparable series may still carry full
    # selected-range history; it must not be treated as a head-to-head
    # return against the portfolio.
    comparable: bool = True
    displayable: bool = False
    normalization: Normalization = "unavailable"
    anchor_date: date | None = None
    display_start_date: date | None = None
    display_end_date: date | None = None
    comparison_start: date | None = None
    comparison_end: date | None = None
    comparison_status: ComparisonStatus = "no_portfolio"
    comparison_return_pct: Decimal | None = None


@dataclass
class PerformanceHeader:
    value_base: Decimal
    value_change_base: Decimal
    value_change_pct: Decimal
    label: str = "market_value_change"


@dataclass
class AllocationPoint:
    point_date: date
    # Only closed-taxonomy keys with a usable, classified value that day —
    # never "Other", never zero-filled to 100 (issue #433 requirement 3).
    weights: dict[str, Decimal] = field(default_factory=dict)
    is_incomplete: bool = False
    excluded_holding_count: int = 0


@dataclass
class Allocation:
    # Closed-taxonomy keys that occur anywhere in `points`, in the fixed
    # `ASSET_CLASS_ORDER` — the frontend colors by this order, never by
    # per-date rank.
    asset_classes: list[str] = field(default_factory=list)
    points: list[AllocationPoint] = field(default_factory=list)


@dataclass
class MonthlyPerformancePoint:
    month: str  # "YYYY-MM"
    start_date: date
    end_date: date
    portfolio_return_pct: Decimal | None
    benchmark_return_pct: Decimal | None
    partial_reason: MonthlyPartialReason | None
    is_approximate: bool
    benchmark_unavailable_reason: str | None


@dataclass
class MonthlyPerformance:
    benchmark_code: str
    method: str = "approx_eod_twr"
    points: list[MonthlyPerformancePoint] = field(default_factory=list)


@dataclass
class PerformanceResult:
    portfolio: PortfolioSeries
    benchmarks: list[BenchmarkSeries]
    header: PerformanceHeader
    allocation: Allocation
    monthly_performance: MonthlyPerformance
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


def _prior_complete_date(session: Session, user_id: uuid.UUID, before: date) -> date | None:
    """Latest complete-batch `snapshot_date` strictly before `before`, for
    this user (unfiltered) — one bounded lookup, not a scan. Used only by
    the monthly-performance opening-boundary lookback (issue #433 design
    §4): "load at most the bounded preceding complete observation needed
    for the first displayed month". Never used to extend the displayed
    portfolio/cumulative series itself."""
    return session.execute(
        select(func.max(PortfolioSnapshotBatch.snapshot_date)).where(
            PortfolioSnapshotBatch.user_id == user_id,
            PortfolioSnapshotBatch.status == "complete",
            PortfolioSnapshotBatch.snapshot_date < before,
        )
    ).scalar_one_or_none()


def _live_org_labels(session: Session, user_id: uuid.UUID) -> dict[uuid.UUID, _OrgLabels]:
    holdings = session.execute(select(Holding).where(Holding.user_id == user_id)).scalars()
    return {h.id: _OrgLabels(portfolio=h.portfolio, account=h.account) for h in holdings}


def _last_snapshot_org_labels(
    session: Session, user_id: uuid.UUID, holding_ids: set[uuid.UUID]
) -> dict[uuid.UUID, _OrgLabels]:
    if not holding_ids:
        return {}
    latest = (
        select(
            PortfolioValueSnapshot.holding_id.label("holding_id"),
            func.max(PortfolioValueSnapshot.snapshot_date).label("max_date"),
        )
        .where(
            PortfolioValueSnapshot.user_id == user_id,
            PortfolioValueSnapshot.holding_id.in_(holding_ids),
            PortfolioValueSnapshot.is_backfilled.is_(False),
        )
        .group_by(PortfolioValueSnapshot.holding_id)
        .subquery()
    )
    rows = session.execute(
        select(PortfolioValueSnapshot)
        .join(
            latest,
            and_(
                PortfolioValueSnapshot.holding_id == latest.c.holding_id,
                PortfolioValueSnapshot.snapshot_date == latest.c.max_date,
                PortfolioValueSnapshot.user_id == user_id,
            ),
        )
        .where(PortfolioValueSnapshot.is_backfilled.is_(False))
    ).scalars()
    return {
        row.holding_id: _OrgLabels(portfolio=row.portfolio, account=row.account)
        for row in rows
        if row.holding_id is not None
    }


def _attach_org_attribution(
    session: Session,
    user_id: uuid.UUID,
    filters: Filters,
    rows_by_date: dict[date, list[PortfolioValueSnapshot]],
) -> None:
    """Load current group/account labels when those dims are filtered.

    Live `holdings` win; missing live row (cleared/deleted) uses that
    holding_id's latest non-backfilled snapshot tags; null holding_id
    stays on the row denorm inside `Filters._org_labels`.
    """
    if filters.groups is None and filters.accounts is None:
        return
    filters.live_labels = _live_org_labels(session, user_id)
    missing: set[uuid.UUID] = set()
    for rows in rows_by_date.values():
        for row in rows:
            hid = row.holding_id
            if hid is not None and hid not in filters.live_labels:
                missing.add(hid)
    filters.last_snapshot_labels = _last_snapshot_org_labels(session, user_id, missing)


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
    `stage_user_snapshot` call resolves `report_currency_for` ONCE per day
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
    whether it currently passes the active filter. A market/broker change
    that drops the row from the current view is D8 snapshot-time outflow
    (not a price move — its stored day-t row is still the right mark). A
    group/account regroup does not drop the holding: current attribution
    (issue #371) follows the live or last-snapshot label for the whole
    tracking history. This falls back to repricing directly from
    `price_snapshots`/`fx_rates` (`_reprice_from_source`) in two cases: NO
    day-t row exists at all (full exit, or the holding row itself was
    deleted/replaced — review 5124107298 finding 1), or a day-t row exists
    but carries `shares == 0` (re-review leftover: a degenerate zero-share
    row has no usable per-share price to derive a mark from, the same
    unpriceable situation as no row at all — never simply excluded, which
    is what let finding 1 happen in the first place).
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


@dataclass
class _SeriesBuild:
    series: PortfolioSeries
    value_start: Decimal
    value_end: Decimal
    # Unrounded daily approximate-TWR link (r_t), keyed by date, for every
    # displayed day except the series' own first point (which has no prior
    # day to link against). Computed regardless of the `twr` request
    # parameter (issue #433 D11/invariant 3: monthly performance never
    # switches to raw market-value change because the cumulative chart's
    # toggle is off).
    daily_links: dict[date, Decimal | None] = field(default_factory=dict)
    # Dates after the filtered scope's own first real match, is_backfilled-
    # only days already dropped — the same set the allocation chart draws
    # from (issue #433 design §3), reused rather than re-queried.
    dates: list[date] = field(default_factory=list)
    filtered_by_date: dict[date, list[PortfolioValueSnapshot]] = field(default_factory=dict)
    all_by_id_by_date: dict[date, dict[uuid.UUID, PortfolioValueSnapshot]] = field(
        default_factory=dict
    )
    day_currency_by_date: dict[date, str] = field(default_factory=dict)


def _build_portfolio_series(
    session: Session,
    user_id: uuid.UUID,
    start_date: date,
    end_date: date,
    filters: Filters,
    twr: bool,
    requested_currency: str,
    tracking_start: date | None,
) -> _SeriesBuild:
    dates = _complete_batch_dates(session, user_id, start_date, end_date)
    rows_by_date = _rows_for_dates(session, user_id, dates)
    _attach_org_attribution(session, user_id, filters, rows_by_date)

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
    # where that day's rows happen not to match (sold lot, or market/broker
    # snapshot-time drop). Group/account regroup uses the current-attribution
    # predicate (issue #371), so a holding newly tagged into a group still
    # contributes its earlier tracking days; this gate is the first day
    # that predicate matches, not the first day the denorm label appears.
    # Without this, a newly added sub-account (a new holding_id with no
    # prior row) inherited an unrelated OTHER account's earlier start date
    # via `_day_value([])`'s "empty filtered set = $0" rule, which in turn
    # gave the common-window benchmark rebase (issue #366 D7) the wrong
    # anchor.
    matched_dates = [d for d in dates if any(filters.matches(r) for r in rows_by_date.get(d, []))]
    any_match_in_range = bool(matched_dates)
    if any_match_in_range:
        dates = [d for d in dates if d >= matched_dates[0]]

    filtered_by_date: dict[date, list[PortfolioValueSnapshot]] = {}
    all_by_id_by_date: dict[date, dict[uuid.UUID, PortfolioValueSnapshot]] = {}
    for d in dates:
        filtered_by_date[d] = [r for r in rows_by_date[d] if filters.matches(r)]
        # ALL of that day's rows keyed by holding_id, regardless of dimension
        # filter — the TWR mark for a holding that drops out of the current
        # view (market/broker snapshot-time D8) still uses its own stored
        # day-t row (outflow from this view, not a price move). Group/account
        # regroup does not drop the holding (issue #371 current attribution).
        # Only a holding with no day-t row at all falls back to repricing
        # from source (see `_contribution`/`_reprice_from_source`).
        # `is_backfilled` rows are excluded here too (issue #366) — known-bad
        # history must never supply a TWR mark, even as a fallback.
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
        return _SeriesBuild(series=empty_series, value_start=Decimal("0"), value_end=Decimal("0"))

    quality_flags: set[str] = set()
    points: list[PerformancePoint] = []
    daily_links: dict[date, Decimal | None] = {}
    day_currency_by_date: dict[date, str] = {}
    ratio = Decimal("1")
    prev_by_id: dict[uuid.UUID, PortfolioValueSnapshot] | None = None
    prev_value: Decimal | None = None
    prev_currency: str | None = None

    for idx, (d, value, by_id, approx, day_currency) in enumerate(included):
        converted_value = _convert_amount(session, value, day_currency, requested_currency, d)
        if converted_value is None:
            continue
        converted_value = converted_value.quantize(_CENT, rounding=ROUND_HALF_UP)
        day_currency_by_date[d] = day_currency

        r_t: Decimal | None = None
        if idx == 0:
            cumulative = Decimal("0")
        else:
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
            #
            # Computed unconditionally (issue #433 D11): monthly
            # performance always needs this approximate-TWR daily link even
            # when `twr=False` only turns off the CUMULATIVE display below.
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
            if twr:
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
        daily_links[d] = r_t

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
    return _SeriesBuild(
        series=series,
        value_start=value_start,
        value_end=value_end,
        daily_links=daily_links,
        dates=dates,
        filtered_by_date=filtered_by_date,
        all_by_id_by_date=all_by_id_by_date,
        day_currency_by_date=day_currency_by_date,
    )


def _build_allocation(
    dates: list[date], filtered_by_date: dict[date, list[PortfolioValueSnapshot]]
) -> Allocation:
    """Asset-class allocation history (issue #433 design §3).

    Reuses the exact `dates`/`filtered_by_date` the cumulative series
    already computed — no extra query per date. A row missing a usable
    value OR a classification is excluded from both numerator and
    denominator and bumps `excluded_holding_count`; a zero denominator (no
    classified, valued row that day) is an explicit incomplete/empty point,
    never a fabricated 100% stack. Because every bucket on a given day
    shares that day's own currency, weights are currency-invariant — no
    conversion is needed here.
    """
    points: list[AllocationPoint] = []
    seen_classes: set[str] = set()
    for d in dates:
        rows = filtered_by_date.get(d, [])
        class_totals: dict[str, Decimal] = {}
        excluded_count = 0
        for row in rows:
            if row.market_value_base is None or row.asset_class is None:
                excluded_count += 1
                continue
            class_totals[row.asset_class] = (
                class_totals.get(row.asset_class, Decimal("0")) + row.market_value_base
            )
        denominator = sum(class_totals.values(), Decimal("0"))
        if denominator <= 0:
            points.append(
                AllocationPoint(
                    point_date=d,
                    weights={},
                    is_incomplete=True,
                    excluded_holding_count=excluded_count,
                )
            )
            continue
        weights = {
            cls: (val / denominator).quantize(_PCT, rounding=ROUND_HALF_UP)
            for cls, val in class_totals.items()
        }
        seen_classes.update(weights.keys())
        points.append(
            AllocationPoint(
                point_date=d,
                weights=weights,
                is_incomplete=excluded_count > 0,
                excluded_holding_count=excluded_count,
            )
        )
    asset_classes = [cls for cls in ASSET_CLASS_ORDER if cls in seen_classes]
    return Allocation(asset_classes=asset_classes, points=points)


def _month_key(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def _build_monthly_performance(
    session: Session,
    user_id: uuid.UUID,
    filters: Filters,
    build: _SeriesBuild,
    tracking_start: date | None,
    requested_currency: str,
    monthly_benchmark: str,
    closes: list[RawClose],
    fx_by_pair: dict[str, list[tuple[date, Decimal]]],
    today: date,
) -> MonthlyPerformance:
    """Monthly portfolio-vs-benchmark bars (issue #433 D11/D12).

    Always approximate EOD TWR, aggregated from the SAME unrounded daily
    links `_build_portfolio_series` already computed
    (`prod(1+r_t for links in month) - 1`), never by subtracting rounded
    cumulative percentages, and never affected by the request's `twr` flag.
    The benchmark leg reuses the already bulk-loaded closes/FX and the
    existing bounded as-of evaluator at the portfolio's own exact monthly
    start/end dates.
    """
    points = build.series.points
    if not points:
        return MonthlyPerformance(benchmark_code=monthly_benchmark, points=[])

    all_dates = [p.point_date for p in points]
    date_to_idx = {d: i for i, d in enumerate(all_dates)}
    approx_by_date = {p.point_date: p.is_approximate for p in points}
    first_overall = all_dates[0]

    months_order: list[str] = []
    dates_by_month: dict[str, list[date]] = {}
    for d in all_dates:
        key = _month_key(d)
        if key not in dates_by_month:
            dates_by_month[key] = []
            months_order.append(key)
        dates_by_month[key].append(d)

    # Bounded one-day lookback (issue #433 design §4 "period boundaries"):
    # only attempted when the first displayed day is itself the first
    # calendar day of its month — the one case where real tracked history
    # may exist just outside the requested/filtered window and this month
    # can be disclosed as a full month instead of a partial one. At most one
    # extra date's rows are loaded; this never changes the displayed
    # cumulative series or its own first point.
    #
    # The prior date itself is also bounded to `LOOKBACK_DAYS` (the same
    # 10-calendar-day staleness bound issue #377's benchmark valuation uses)
    # — "bounded" means a normal weekend/holiday-sized gap, not "whatever the
    # last complete batch happens to be" (review 5124107298-successor
    # feedback on PR #434): a stale, months-old prior day would otherwise
    # get silently disclosed as a real, unremarkable "full month" open
    # instead of the honest partial/tracking_start reason.
    extra_open_link: Decimal | None = None
    extra_open_date: date | None = None
    if first_overall.day == 1:
        prior_date = _prior_complete_date(session, user_id, first_overall)
        if prior_date is not None and (first_overall - prior_date).days <= LOOKBACK_DAYS:
            prior_rows_all = _rows_for_dates(session, user_id, [prior_date]).get(prior_date, [])
            prior_filtered = [r for r in prior_rows_all if filters.matches(r)]
            prior_value = _day_value(prior_filtered)
            if prior_value is not None and prior_value > 0:
                first_currency = build.day_currency_by_date[first_overall]
                prior_currency = _day_currency(prior_rows_all) or first_currency
                v_prev_conv = _convert_amount(
                    session, prior_value, prior_currency, first_currency, first_overall
                )
                if v_prev_conv is not None:
                    prior_by_id = {
                        r.holding_id: r for r in prior_filtered if r.holding_id is not None
                    }
                    r0 = _twr_day_return(
                        session,
                        prior_by_id,
                        build.all_by_id_by_date[first_overall],
                        v_prev_conv,
                        first_overall,
                        first_currency,
                    )
                    if r0 is not None:
                        extra_open_link = r0
                        extra_open_date = prior_date

    current_month_key = _month_key(today)
    monthly_points: list[MonthlyPerformancePoint] = []

    for idx, month_key in enumerate(months_order):
        month_dates = dates_by_month[month_key]
        is_first_month = idx == 0
        is_current_month = month_key == current_month_key

        ratio = Decimal("1")
        any_valid_link = False
        for d in month_dates:
            if d == first_overall and not (is_first_month and extra_open_date is not None):
                continue  # the series' own anchor day has no link
            r_t = build.daily_links.get(d)
            if r_t is not None:
                ratio *= Decimal("1") + r_t
                any_valid_link = True
        if is_first_month and extra_open_date is not None and extra_open_link is not None:
            ratio *= Decimal("1") + extra_open_link
            any_valid_link = True

        baseline_only_single_point = (
            is_first_month and len(month_dates) == 1 and extra_open_date is None
        )
        portfolio_return: Decimal | None
        if any_valid_link or baseline_only_single_point:
            portfolio_return = (ratio - Decimal("1")).quantize(_PCT, rounding=ROUND_HALF_UP)
        else:
            portfolio_return = None

        is_approx = any(approx_by_date.get(d, False) for d in month_dates)

        partial_reason: MonthlyPartialReason | None
        if is_current_month:
            partial_reason = "month_to_date"
        elif is_first_month:
            if extra_open_date is not None:
                partial_reason = None
            elif tracking_start is not None and first_overall <= tracking_start:
                partial_reason = "tracking_start"
            else:
                partial_reason = "range_start"
        else:
            partial_reason = None

        if is_first_month and extra_open_date is not None:
            disclosed_start = extra_open_date
        elif is_first_month:
            disclosed_start = first_overall
        else:
            disclosed_start = all_dates[date_to_idx[month_dates[0]] - 1]
        disclosed_end = month_dates[-1]

        start_val = evaluate_index_day(disclosed_start, closes, fx_by_pair, requested_currency)
        end_val = evaluate_index_day(disclosed_end, closes, fx_by_pair, requested_currency)
        benchmark_return: Decimal | None = None
        unavailable_reason: str | None = None
        if start_val.value is None:
            unavailable_reason = start_val.unavailable_reason
        elif end_val.value is None:
            unavailable_reason = end_val.unavailable_reason
        elif start_val.value <= 0:
            unavailable_reason = "invalid_price"
        else:
            benchmark_return = (end_val.value / start_val.value - Decimal("1")).quantize(
                _PCT, rounding=ROUND_HALF_UP
            )

        monthly_points.append(
            MonthlyPerformancePoint(
                month=month_key,
                start_date=disclosed_start,
                end_date=disclosed_end,
                portfolio_return_pct=portfolio_return,
                benchmark_return_pct=benchmark_return,
                partial_reason=partial_reason,
                is_approximate=is_approx,
                benchmark_unavailable_reason=unavailable_reason,
            )
        )

    return MonthlyPerformance(benchmark_code=monthly_benchmark, points=monthly_points)


def _point_from_valuation(valuation: DailyValuation, return_pct: Decimal | None) -> BenchmarkPoint:
    return BenchmarkPoint(
        point_date=valuation.evaluation_date,
        return_pct_cumulative=return_pct,
        price_as_of=valuation.price_as_of,
        fx_as_of=dict(valuation.fx_as_of),
        carried=valuation.carried,
        unavailable_reason=valuation.unavailable_reason,
    )


def _classify_comparison(
    portfolio: PortfolioSeries,
    by_day: dict[date, DailyValuation],
) -> tuple[ComparisonStatus, bool, Decimal | None, date | None, date | None]:
    if (
        portfolio.empty
        or portfolio.start_date is None
        or portfolio.end_date is None
        or not portfolio.points
    ):
        return "no_portfolio", False, None, None, None

    p0 = portfolio.start_date
    p1 = portfolio.end_date
    anchor = by_day.get(p0)
    if anchor is None or anchor.value is None or anchor.value <= 0:
        return "anchor_unavailable", False, None, p0, p1

    for point in portfolio.points:
        day_value = by_day.get(point.point_date)
        if day_value is None or day_value.value is None:
            return "incomplete_window", False, None, p0, p1

    if p0 == p1:
        return "baseline_only", True, Decimal("0.0000"), p0, p1

    end_value = by_day[p1].value
    if end_value is None or end_value <= 0:
        return "incomplete_window", False, None, p0, p1
    comparison_return = (end_value / anchor.value - Decimal("1")).quantize(
        _PCT, rounding=ROUND_HALF_UP
    )
    return "available", True, comparison_return, p0, p1


def _unavailable_series(
    index_code: str,
    source_start: date | None,
    portfolio: PortfolioSeries,
    by_day: dict[date, DailyValuation],
) -> BenchmarkSeries:
    status, comparable, comparison_return, comparison_start, comparison_end = _classify_comparison(
        portfolio, by_day
    )
    return BenchmarkSeries(
        index_code=index_code,
        name=BENCHMARK_NAMES.get(index_code, index_code),
        start_date=source_start,
        points=[],
        comparable=comparable,
        displayable=False,
        normalization="unavailable",
        comparison_start=comparison_start,
        comparison_end=comparison_end,
        comparison_status=status,
        comparison_return_pct=comparison_return,
    )


def _serialize_benchmark_series(
    index_code: str,
    range_start: date,
    range_end: date,
    closes: list[RawClose],
    by_day: dict[date, DailyValuation],
    portfolio: PortfolioSeries,
) -> BenchmarkSeries:
    source_start = first_source_in_range(closes, range_start, range_end)
    if not by_day:
        return _unavailable_series(index_code, source_start, portfolio, {})

    p0 = portfolio.start_date if not portfolio.empty else None
    p0_value = by_day[p0].value if p0 is not None and p0 in by_day else None
    if p0 is not None and p0_value is not None and p0_value > 0:
        anchor_date = p0
        anchor_value = p0_value
        normalization: Normalization = "portfolio_start"
    else:
        first_valid = next(
            (day for day, valuation in sorted(by_day.items()) if valuation.value is not None),
            None,
        )
        if first_valid is None:
            return _unavailable_series(index_code, source_start, portfolio, by_day)
        first_value = by_day[first_valid].value
        if first_value is None or first_value <= 0:
            return _unavailable_series(index_code, source_start, portfolio, by_day)
        anchor_date = first_valid
        anchor_value = first_value
        normalization = "own_start"

    first_valid_day = next(
        day for day, valuation in sorted(by_day.items()) if valuation.value is not None
    )
    points: list[BenchmarkPoint] = []
    for day in sorted(by_day):
        if day < first_valid_day:
            continue
        valuation = by_day[day]
        if valuation.value is None:
            points.append(_point_from_valuation(valuation, None))
            continue
        pct = (valuation.value / anchor_value - Decimal("1")).quantize(_PCT, rounding=ROUND_HALF_UP)
        points.append(_point_from_valuation(valuation, pct))

    non_null = [point for point in points if point.return_pct_cumulative is not None]
    status, comparable, comparison_return, comparison_start, comparison_end = _classify_comparison(
        portfolio, by_day
    )
    return BenchmarkSeries(
        index_code=index_code,
        name=BENCHMARK_NAMES.get(index_code, index_code),
        start_date=source_start,
        points=points,
        comparable=comparable,
        displayable=bool(non_null),
        normalization=normalization,
        anchor_date=anchor_date,
        display_start_date=non_null[0].point_date if non_null else None,
        display_end_date=non_null[-1].point_date if non_null else None,
        comparison_start=comparison_start,
        comparison_end=comparison_end,
        comparison_status=status,
        comparison_return_pct=comparison_return,
    )


@dataclass
class _BenchmarkBuild:
    series: list[BenchmarkSeries] = field(default_factory=list)
    # Raw closes/FX for every loaded code (cumulative selections plus the
    # monthly benchmark, issue #433) — reused by the monthly builder so it
    # never issues its own `load_index_closes`/`load_fx_series` call.
    closes_by_index: dict[str, list[RawClose]] = field(default_factory=dict)
    fx_by_pair: dict[str, list[tuple[date, Decimal]]] = field(default_factory=dict)


def _build_selected_benchmarks(
    session: Session,
    benchmark_codes: list[str],
    range_start: date,
    range_end: date,
    requested_currency: str,
    portfolio: PortfolioSeries,
    extra_codes: tuple[str, ...] = (),
) -> _BenchmarkBuild:
    """Builds the cumulative-chart benchmark series for `benchmark_codes`.

    `extra_codes` (issue #433: the independently single-selected
    `monthly_benchmark`) are bulk-loaded in the SAME `load_index_closes`/
    `load_fx_series` calls so a code used only for the monthly card never
    triggers its own extra query, but do not produce a `BenchmarkSeries` of
    their own — the monthly card is not part of the cumulative multi-select.
    """
    codes = [code for code in benchmark_codes if code in INDEX_YF_TICKERS]
    load_codes = list(dict.fromkeys([*codes, *(c for c in extra_codes if c in INDEX_YF_TICKERS)]))
    if not load_codes:
        return _BenchmarkBuild()
    closes_by_index = load_index_closes(session, load_codes, range_start, range_end)
    fx_by_pair = load_fx_series(
        session,
        required_pairs_for_closes(closes_by_index, requested_currency),
        range_start,
        range_end,
    )
    series: list[BenchmarkSeries] = []
    for code in codes:
        closes = closes_by_index.get(code, [])
        by_day = evaluate_index_range(
            range_start, range_end, closes, fx_by_pair, requested_currency
        )
        for point in portfolio.points:
            if point.point_date not in by_day:
                by_day[point.point_date] = evaluate_index_day(
                    point.point_date, closes, fx_by_pair, requested_currency
                )
        series.append(
            _serialize_benchmark_series(code, range_start, range_end, closes, by_day, portfolio)
        )
    return _BenchmarkBuild(series=series, closes_by_index=closes_by_index, fx_by_pair=fx_by_pair)


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
    monthly_benchmark: str = "sp500",
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

    build = _build_portfolio_series(
        session,
        user_id,
        start_date,
        end_date,
        filters,
        twr,
        requested_currency,
        tracking_start,
    )
    portfolio_series = build.series
    value_start = build.value_start
    value_end = build.value_end

    # Issue #377: evaluate each selected index across the requested range
    # with bounded as-of prices/FX, then normalize to P0 when that day is
    # valuable. Display history is not clipped to the portfolio window;
    # comparison_status/comparable say whether the shared-anchor return is
    # honest. Do not rebase already-rounded percentages.
    # Issue #433: the monthly card's single benchmark is loaded in the same
    # bulk call even when it is not among the cumulative multi-select.
    benchmark_build = _build_selected_benchmarks(
        session,
        benchmark_codes,
        start_date,
        end_date,
        requested_currency,
        portfolio_series,
        extra_codes=(monthly_benchmark,),
    )
    benchmarks = benchmark_build.series

    allocation = _build_allocation(build.dates, build.filtered_by_date)

    monthly_performance = _build_monthly_performance(
        session,
        user_id,
        filters,
        build,
        tracking_start,
        requested_currency,
        monthly_benchmark,
        benchmark_build.closes_by_index.get(monthly_benchmark, []),
        benchmark_build.fx_by_pair,
        today,
    )

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
        portfolio=portfolio_series,
        benchmarks=benchmarks,
        header=header,
        allocation=allocation,
        monthly_performance=monthly_performance,
        meta=meta,
    )
