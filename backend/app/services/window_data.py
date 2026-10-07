"""Incremental-report window data (ADR-002 report layer).

The report covers `[period_start, period_end]` where period_start is the user's
watermark (previous report's period_end capped at the seven-calendar-day floor) and
period_end is the run cutoff. News and price moves over that window are read from
the capture-layer stores, never re-fetched live.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.timezones import ET
from app.models.holding import Holding
from app.models.intel import NewsInstrument
from app.models.news import News
from app.models.news_surfaced import NewsSurfaced
from app.models.price_snapshot import PriceSnapshot
from app.models.report import Report
from app.models.ticker_theme import TickerTheme
from app.services.asset_class_config import load_asset_class_config
from app.services.instrument_symbols import InstrumentKey, intelligence_identifier
from app.services.intel_deepen_config import load_intel_deepen_config
from app.services.intel_records import headline_from_row, macro_labels_for_items
from app.services.market_sessions import baseline_session, previous_sessions, sessions_closing_in
from app.services.news_fetcher import LATE_INGEST_WINDOW, NewsItem
from app.services.price_anomaly_detector import ConstituentMove, PriceAnomaly
from app.services.ticker_leverage import load_leverage_map
from app.services.user_scope import global_identifier_universe, user_holdings

logger = logging.getLogger(__name__)

# Retired from the production watermark path (Ring 1-B §6.6). Kept as a
# fixed fixture timestamp for tests that need a historical baseline — a new
# user with no DONE reports now uses `cold_start_watermark(now)` instead.
BOOTSTRAP_WATERMARK = datetime(2026, 6, 1, 16, 0, tzinfo=ET)
WINDOW_MAX_DAYS = 7

_RATIO = Decimal("0.0001")  # 4 dp for pct_change

# Per-asset-class (per_day_trigger, cumulative_window_cap) — admin-editable,
# see config/asset_class_thresholds.yml (#35). Loaded fresh on every call (no
# cache) so an admin's edit takes effect on the next report without a
# process restart.
#
# The window threshold = per_day * trading_days, capped at the class cap.
# Broad funds have a high cap (40%) so a normal weekly drift never fires;
# individual stocks cap at 10% (any week-long run above that is noteworthy).
# Per-day trigger is the same (5%) for most equity classes so a single
# violent session is still caught regardless of cumulative behaviour.


def _window_threshold(per_day: Decimal, cap: Decimal, trading_days: int) -> Decimal:
    """per_day * trading_days, capped at the per-class cumulative cap."""
    return min(per_day * max(trading_days, 1), cap)


# Completed statuses whose period_end counts toward the watermark.
_DONE_STATUSES = ("success", "skipped", "needs_review")


def cold_start_watermark(now: datetime) -> datetime:
    """ET midnight of the calendar date WINDOW_MAX_DAYS days before ``now``.

    The earliest start of any report window: a new user's first report, and
    the cap after a gap. Pure function of ``now``.
    """
    day = now.astimezone(ET).date() - timedelta(days=WINDOW_MAX_DAYS)
    return datetime(day.year, day.month, day.day, tzinfo=ET)


def user_watermark(
    session: Session,
    user_id: object,
    report_type: str,
    exclude_report_id: object | None = None,
    *,
    now: datetime,
) -> datetime:
    """period_start for the next report = max(period_end) over the user's completed
    reports of this type, capped at ET midnight seven calendar days before ``now``.
    With no completed history, use that same floor.

    ``exclude_report_id`` drops the report currently being (re)generated from the
    watermark. Without it, regenerating an existing failed/needs_review/skipped row
    would read that row's OWN period_end back as its period_start — collapsing the
    window to a few minutes (the session uses autoflush=False, so the in-flight
    status reset is not yet visible to this query). Always pass the row's id when
    regenerating in place.

    ``now`` is required for every window and must be the same timestamp
    ``generate_report`` already computed for the batch — do not omit it and
    do not let this function read the wall clock.
    """
    stmt = select(func.max(Report.period_end)).where(
        Report.user_id == user_id,
        Report.report_type == report_type,
        Report.status.in_(_DONE_STATUSES),
    )
    if exclude_report_id is not None:
        stmt = stmt.where(Report.id != exclude_report_id)
    latest = session.execute(stmt).scalar_one_or_none()
    floor = cold_start_watermark(now)
    return max(latest, floor) if latest is not None else floor


def backfill_news_surfaced_before(session: Session, user_id: uuid.UUID, cutoff: datetime) -> int:
    """Mark news published strictly before ``cutoff`` as already surfaced.

    Used at signup and whenever a newly computed window starts at the floor
    so ``load_news_window`` (no lower bound) does not swallow older history. ``report_id``
    on these rows is the user's own id — not a real Report — because
    ``news_surfaced.report_id`` has no FK and this backfill is not attached
    to a generated report. ``ON CONFLICT DO NOTHING`` makes a later
    generate_report backfill of the same cutoff a no-op.
    """
    news_ids = list(
        session.execute(select(News.id).where(News.published_at < cutoff)).scalars().all()
    )
    if not news_ids:
        return 0
    stmt = (
        pg_insert(NewsSurfaced)
        .values([{"user_id": user_id, "news_id": nid, "report_id": user_id} for nid in news_ids])
        .on_conflict_do_nothing(constraint="uq_news_surfaced_user_news")
    )
    session.execute(stmt)
    return len(news_ids)


def load_news_window(
    session: Session, start: datetime, end: datetime, user_id: uuid.UUID
) -> list[NewsItem]:
    """News published at/before the window cutoff that hasn't yet been surfaced
    in any of THIS USER's DONE-status reports, newest first, from the `news`
    store.

    Issue #30 late ingestion is supported within the shared 48-hour allowance:
    published_at > start - LATE_INGEST_WINDOW and published_at <= end.
    The per-user surfaced ledger excludes previously reported items.

    Scoped per `user_id` (PR #139 review): `news` is a global capture-layer
    store, but each user's report stream has its own watermark/window, so the
    same news item can legitimately need to surface once for each user —
    marking it surfaced for one user must not hide it from another.
    """
    surfaced = select(NewsSurfaced.news_id).where(NewsSurfaced.user_id == user_id)
    rows = (
        session.execute(
            select(News)
            .where(
                News.origin == "pool",
                News.published_at > start - LATE_INGEST_WINDOW,
                News.published_at <= end,
                News.id.not_in(surfaced),
            )
            .order_by(News.published_at.desc())
        )
        .scalars()
        .all()
    )
    return [headline_from_row(r) for r in rows]


def macro_news_items(session: Session, items: list[NewsItem]) -> list[NewsItem]:
    """Filter only macro detection input; preserve the complete report/holding pool."""
    labels = macro_labels_for_items(session, items)
    return [
        item
        for item in items
        if item.url_hash not in labels or labels[item.url_hash]["type"] == "development"
    ]


def load_instrument_news_by_identifier(
    session: Session, start: datetime, end: datetime, user_id: uuid.UUID, identifiers: list[str]
) -> dict[str, list[NewsItem]]:
    """Load linked headlines grouped by their owning identifier."""
    if not identifiers:
        return {}
    surfaced = select(NewsSurfaced.news_id).where(NewsSurfaced.user_id == user_id)
    result: dict[str, list[NewsItem]] = {identifier: [] for identifier in identifiers}
    rows = session.execute(
        select(NewsInstrument.identifier, News)
        .join(News, News.id == NewsInstrument.news_id)
        .where(
            NewsInstrument.identifier.in_(identifiers),
            NewsInstrument.relation.is_(None),
            News.published_at > start - LATE_INGEST_WINDOW,
            News.published_at <= end,
            News.id.not_in(surfaced),
        )
        .order_by(News.published_at.desc())
    )
    for identifier, row in rows:
        result[identifier].append(headline_from_row(row))
    return result


def load_related_news_by_identifier(
    session: Session, start: datetime, end: datetime, user_id: uuid.UUID, identifiers: list[str]
) -> dict[str, list[tuple[NewsItem, str, str]]]:
    """Related-company headlines (#681) as (item, related entity, relation), newest first."""
    if not identifiers:
        return {}
    surfaced = select(NewsSurfaced.news_id).where(NewsSurfaced.user_id == user_id)
    result: dict[str, list[tuple[NewsItem, str, str]]] = {}
    rows = session.execute(
        select(NewsInstrument.identifier, NewsInstrument.related_to, NewsInstrument.relation, News)
        .join(News, News.id == NewsInstrument.news_id)
        .where(
            NewsInstrument.identifier.in_(identifiers),
            NewsInstrument.relation.is_not(None),
            News.published_at > start - LATE_INGEST_WINDOW,
            News.published_at <= end,
            News.id.not_in(surfaced),
        )
        .order_by(News.published_at.desc())
    )
    for identifier, related_to, relation, row in rows:
        result.setdefault(identifier, []).append(
            (headline_from_row(row), str(related_to), str(relation))
        )
    return result


def mark_news_surfaced(
    session: Session, user_id: uuid.UUID, report_id: uuid.UUID, url_hashes: Sequence[str]
) -> None:
    """Record that these news items appeared in a report of this user's that
    reached a DONE status (success/needs_review/skipped) — the dedup ledger
    `load_news_window` reads to never select them again for this user.

    Idempotent against Celery redelivery (`task_acks_late`): `(user_id,
    news_id)` is unique on `news_surfaced`, so re-marking an already-surfaced
    item is a no-op via ON CONFLICT DO NOTHING rather than an IntegrityError.
    """
    if not url_hashes:
        return
    news_ids = session.execute(select(News.id).where(News.url_hash.in_(url_hashes))).scalars().all()
    if not news_ids:
        return
    stmt = (
        pg_insert(NewsSurfaced)
        .values([{"user_id": user_id, "news_id": nid, "report_id": report_id} for nid in news_ids])
        .on_conflict_do_nothing(constraint="uq_news_surfaced_user_news")
    )
    session.execute(stmt)


def unmark_news_surfaced(session: Session, report_id: uuid.UUID) -> None:
    """Undo `mark_news_surfaced` for a specific report — used when a
    DONE-status report is reopened and reprocessed against its own frozen
    window (PR #139 review).

    `generate_report` reopens an existing `needs_review` row for retry,
    clearing `report_inputs` but reusing the original `period_start`/
    `period_end` (frozen once set). Without this, the retry's
    `load_news_window` call would see this report's own prior marks and
    silently select a DIFFERENT (smaller) news set than the first attempt did
    for the identical window — call this before re-running `load_news_window`
    on a reopened row so the original candidate set is fully selectable
    again.
    """
    session.execute(delete(NewsSurfaced).where(NewsSurfaced.report_id == report_id))


def _close_snapshot_before_window(
    session: Session, ticker: str, market: str | None, start: datetime
) -> PriceSnapshot | None:
    day = baseline_session(market, start)
    return _stored_closes(session, ticker, market, [day]).get(day) if day else None


def _stored_closes(
    session: Session, ticker: str, market: str | None, days: list[date]
) -> dict[date, PriceSnapshot]:
    if not days:
        return {}
    rows = session.scalars(
        select(PriceSnapshot).where(
            PriceSnapshot.ticker == ticker,
            PriceSnapshot.market == market,
            PriceSnapshot.session_node == "close",
            PriceSnapshot.close.is_not(None),
            PriceSnapshot.trade_date.in_(days),
        )
    )
    return {row.trade_date: row for row in rows}


def _window_closes(
    session: Session, ticker: str, market: str | None, start: datetime, end: datetime
) -> list[PriceSnapshot]:
    days = sessions_closing_in(market, start, end)
    rows = _stored_closes(session, ticker, market, days)
    return [rows[day] for day in days if day in rows]


def _universe_keys(session: Session) -> set[tuple[str, str | None]]:
    return {
        (identifier, h.market)
        for identifier, holdings in global_identifier_universe(session).items()
        for h in holdings
    }


def latest_window_close_date(session: Session, start: datetime, end: datetime) -> date | None:
    """Latest calendar window session with a stored in-scope close."""
    days = [
        row.trade_date
        for identifier, market in _universe_keys(session)
        for row in _window_closes(session, identifier, market, start, end)
    ]
    return max(days) if days else None


def _after_hours_last(
    session: Session, ticker: str, market: str | None, on: date
) -> Decimal | None:
    snap = session.scalar(
        select(PriceSnapshot).where(
            PriceSnapshot.ticker == ticker,
            PriceSnapshot.market == market,
            PriceSnapshot.session_node == "after_close",
            PriceSnapshot.trade_date == on,
        )
    )
    return snap.last if snap else None


@dataclass
class HoldingMove:
    """Raw window price-move facts for one identifier, computed exactly once
    regardless of how many holdings (across however many users) carry it.

    Deliberately carries NO threshold judgment and no per-holding fields
    (name, asset_type) — see `select_user_anomalies` for why the
    anomaly/non-anomaly decision has to stay per-user: two different users'
    Holding rows can classify the very same identifier under different
    `asset_class` values, which changes the threshold (design doc §3.3,
    Ring 1-A design.md issue #128).
    """

    identifier: str
    market: str
    current_price: Decimal
    prev_price: Decimal
    net_pct: Decimal
    max_day_pct: Decimal | None
    max_day_date: date | None
    baseline_date: date
    latest_date: date
    prev_close: Decimal | None
    day_open: Decimal | None
    day_high: Decimal | None
    day_low: Decimal | None
    day_close: Decimal | None
    after_hours: Decimal | None
    d3_pct: Decimal | None = None
    d5_pct: Decimal | None = None


# Keyed by the exact (start, end) window a batch fan-out is generating over —
# see detect_window_anomalies' `moves_cache` parameter.
MovesCache = dict[
    tuple[datetime, datetime], tuple[dict[tuple[str, str | None], "HoldingMove"], int]
]


def _compute_identifier_move(
    session: Session, identifier: str, market: str | None, start: datetime, end: datetime
) -> HoldingMove | None:
    baseline = _close_snapshot_before_window(session, identifier, market, start)
    series = _window_closes(session, identifier, market, start, end)
    if baseline is None or baseline.close is None or baseline.close == 0 or not series:
        return None
    latest = series[-1]
    if latest.close is None:
        return None
    latest_close = latest.close
    net_pct = (latest_close / baseline.close - 1).quantize(_RATIO)
    path = {row.trade_date: row for row in [baseline, *series]}
    max_day_pct: Decimal | None = None
    max_day_date: date | None = None
    for cur in series:
        previous = previous_sessions(market, cur.trade_date, 1)
        prev = path.get(previous[0]) if previous else None
        if prev is None or prev.close is None or prev.close == 0 or cur.close is None:
            continue
        day_pct = (cur.close / prev.close - 1).quantize(_RATIO)
        if max_day_pct is None or abs(day_pct) > abs(max_day_pct):
            max_day_pct, max_day_date = day_pct, cur.trade_date
    preceding = previous_sessions(market, latest.trade_date, 5)
    if not preceding:
        preceding = previous_sessions(market, latest.trade_date, 3)
    if not preceding:
        preceding = previous_sessions(market, latest.trade_date, 1)
    history = _stored_closes(session, identifier, market, preceding)

    def rolling(n: int) -> Decimal | None:
        days = previous_sessions(market, latest.trade_date, n)
        if not days or any(day not in history for day in days):
            return None
        close = history[days[0]].close
        return (latest_close / close - 1).quantize(_RATIO) if close else None

    previous = previous_sessions(market, latest.trade_date, 1)
    prev_row = history.get(previous[0]) if previous else None
    return HoldingMove(
        identifier=identifier,
        market=latest.market,
        current_price=latest.close,
        prev_price=baseline.close,
        net_pct=net_pct,
        max_day_pct=max_day_pct,
        max_day_date=max_day_date,
        baseline_date=baseline.trade_date,
        latest_date=latest.trade_date,
        prev_close=prev_row.close if prev_row else None,
        day_open=latest.open,
        day_high=latest.high,
        day_low=latest.low,
        day_close=latest.close,
        after_hours=_after_hours_last(session, identifier, market, latest.trade_date),
        d3_pct=rolling(3),
        d5_pct=rolling(5),
    )


def _merge_theme_anomalies(
    flagged: list[tuple[Holding, PriceAnomaly]],
    theme_row: TickerTheme,
) -> PriceAnomaly:
    """Merge multiple per-holding anomalies sharing a theme into one entry.

    Headline pct_change = value-weighted average of each holding's window net
    pct.  The session arc (open/high/low/close) comes from the value-dominant
    holding so the numbers remain coherent (mixing two currencies' OHLC is
    meaningless).  The threshold, trigger, and date range are taken from the
    dominant holding as well.
    """

    # Sort by real value descending; fall back to 0 when value is unknown.
    #
    # `Holding.current_value` is not a valuation the codebase maintains for
    # pricing_mode="auto" holdings (build_snapshot_row always freezes it as
    # None); the live column can hold anything depending on which creation
    # path wrote the row (issue #492). For an auto holding the real value is
    # shares x its own already-computed current price; for everything else
    # (manual/cash/capture_supported=False), current_value is the real,
    # user-declared value and is unchanged.
    def _val(pair: tuple[Holding, PriceAnomaly]) -> Decimal:
        h, a = pair
        if h.pricing_mode == "auto":
            if h.shares is None:
                return Decimal("0")
            return h.shares * a.current_price
        return h.current_value or Decimal("0")

    flagged_sorted = sorted(flagged, key=_val, reverse=True)
    _dominant_h, dominant_a = flagged_sorted[0]

    total_value = sum(_val(pair) for pair in flagged)
    if total_value == 0:
        # Equal-weight fallback when no values are available.
        weighted_pct = (
            sum((a.pct_change for _, a in flagged), Decimal("0")) / len(flagged)
        ).quantize(_RATIO)
    else:
        weighted_pct = (
            sum(_val(pair) * pair[1].pct_change for pair in flagged) / total_value
        ).quantize(_RATIO)

    def weighted_rolling(values: list[tuple[Decimal | None, Decimal]]) -> Decimal | None:
        if any(value is None for value, _ in values):
            return None
        present = [(value, weight) for value, weight in values if value is not None]
        if total_value == 0:
            return (sum((value for value, _ in present), Decimal(0)) / len(present)).quantize(
                _RATIO
            )
        return (
            sum((value * weight for value, weight in present), Decimal(0)) / total_value
        ).quantize(_RATIO)

    constituents = [
        ConstituentMove(
            name=h.name,
            identifier=a.identifier,
            pct_change=a.pct_change,
            current_value=_val((h, a)),
        )
        for h, a in flagged_sorted
    ]

    return PriceAnomaly(
        name=theme_row.theme_label_zh,
        identifier=theme_row.theme,
        asset_type=theme_row.asset_class,
        current_price=dominant_a.current_price,
        prev_price=dominant_a.prev_price,
        pct_change=weighted_pct,
        threshold=dominant_a.threshold,
        trigger=dominant_a.trigger,
        market=dominant_a.market,
        baseline_date=dominant_a.baseline_date,
        latest_date=dominant_a.latest_date,
        window_net_pct=weighted_pct,
        d3_pct=weighted_rolling([(a.d3_pct, _val((h, a))) for h, a in flagged]),
        d5_pct=weighted_rolling([(a.d5_pct, _val((h, a))) for h, a in flagged]),
        max_day_pct=dominant_a.max_day_pct,
        max_day_date=dominant_a.max_day_date,
        prev_close=dominant_a.prev_close,
        day_open=dominant_a.day_open,
        day_high=dominant_a.day_high,
        day_low=dominant_a.day_low,
        day_close=dominant_a.day_close,
        after_hours=dominant_a.after_hours,
        theme=theme_row.theme,
        theme_label_zh=theme_row.theme_label_zh,
        theme_label_en=theme_row.theme_label_en,
        constituents=constituents,
    )


def _count_trading_days(keys: set[tuple[str, str | None]], start: datetime, end: datetime) -> int:
    return len({day for _, market in keys for day in sessions_closing_in(market, start, end)})


def _load_theme_map(session: Session) -> dict[str, TickerTheme]:
    """ticker (upper) -> TickerTheme row."""
    return {row.ticker.upper(): row for row in session.execute(select(TickerTheme)).scalars().all()}


def compute_global_moves(
    session: Session, start: datetime, end: datetime
) -> tuple[dict[tuple[str, str | None], HoldingMove], int]:
    """Every identifier across ALL users' auto-priced holdings (design doc
    §1.3/§3.3, issue #128 A1), window price move computed exactly once each.

    price_capture's identifier universe is already global (no user_id
    filter — see design doc §1.3); this makes that explicit and shares the
    snapshot query + move computation across every user who happens to hold
    the same identifier, instead of recomputing it once per Holding row (the
    pre-A1 behavior, which meant N users each holding the same ticker paid
    for the same query N times).

    No threshold judgment happens here — see `select_user_anomalies`.
    Returns ((identifier, declared market) -> HoldingMove, calendar trading days).
    """
    keys = _universe_keys(session)
    trading_days = _count_trading_days(keys, start, end)
    moves: dict[tuple[str, str | None], HoldingMove] = {}
    for identifier, market in keys:
        move = _compute_identifier_move(session, identifier, market, start, end)
        if move is not None:
            moves[(identifier, market)] = move
    return moves, trading_days


def resolve_global_moves(
    session: Session,
    start: datetime,
    end: datetime,
    moves_cache: MovesCache | None = None,
) -> tuple[dict[tuple[str, str | None], HoldingMove], int]:
    """`compute_global_moves` behind the batch-shared `moves_cache` — the one
    place the cache-or-compute decision lives.

    Public because the global move set has a SECOND consumer besides anomaly
    detection: `generate_report` reads the large-holding window moves from it,
    and must not pay for a second full computation to do so.
    """
    cache_key = (start, end)
    cached = moves_cache.get(cache_key) if moves_cache is not None else None
    if cached is None:
        cached = compute_global_moves(session, start, end)
        if moves_cache is not None:
            moves_cache[cache_key] = cached
    return cached


def preferred_identifier_holdings(holdings: Sequence[Holding]) -> dict[str, Holding]:
    """Lowest-position row per identifier; NULL last, iteration order breaks ties."""
    result: dict[str, Holding] = {}
    for h in holdings:
        raw = h.ticker or h.fund_code
        if not raw:
            continue
        identifier = intelligence_identifier(InstrumentKey("ticker", raw))
        existing = result.get(identifier)
        if existing is None or (h.position is None, h.position or 0) < (
            existing.position is None,
            existing.position or 0,
        ):
            result[identifier] = h
    return result


def select_user_anomalies(
    moves: dict[tuple[str, str | None], HoldingMove],
    holdings: Sequence[Holding],
    trading_days: int,
    theme_map: dict[str, TickerTheme],
    leverage_map: dict[str, Decimal],
) -> list[PriceAnomaly]:
    """Per-user threshold judgment + theme merge over globally-computed moves.

    Threshold judgment stays per-user rather than folding into
    compute_global_moves: two users can hold the very same identifier under
    different `Holding.asset_class` values (design doc §3.3 — the same
    ticker classified differently by two users' upload parses is a real,
    already-possible case), so the exact same raw move can clear the
    threshold for one user and not the other. Pure in-memory — no DB access,
    no LLM, no I/O — so it's cheap to call once per user in a fan-out loop
    even though compute_global_moves ran only once for the whole batch.

    Holdings that share a theme in ``ticker_themes`` are merged into a single
    anomaly entry if ANY constituent flags, exactly as the pre-split
    ``detect_window_anomalies`` did — merging only ever considers the
    holdings passed in here (this one user's), so it cannot pull another
    user's holdings into a merged entry.

    ``leverage_map`` (ticker_leverage_overrides, issue #87): a leveraged
    product's routine daily/cumulative move is the underlying's move times
    its leverage_multiple, so both per_day and cumulative_cap are widened
    (multiplied up) for a ticker with an override — the opposite direction
    from the §4.1 concentration adjustment in portfolio_calculator.py.
    Keeps this function pure/in-memory like theme_map: the caller loads the
    table once via ``load_leverage_map`` and passes the dict in.
    """
    config = load_asset_class_config()
    rolling = load_intel_deepen_config().thresholds
    theme_buckets: dict[str, list[tuple[Holding, PriceAnomaly]]] = {}
    preferred = preferred_identifier_holdings(holdings)
    standalone_by_id: dict[str, tuple[Holding, PriceAnomaly]] = {}

    for h in holdings:
        if h.pricing_mode != "auto":
            continue
        raw = h.ticker or h.fund_code
        # .upper() here must match global_identifier_universe's key casing
        # (PR #151 review round 2): POST /holdings/confirm accepts
        # ParsedRow.ticker as-is, bypassing the upload parser's case
        # normalization, so a mixed-case ticker's move gets computed
        # correctly under moves' uppercase key but would silently miss here
        # without the same normalization on this side of the lookup.
        identifier = intelligence_identifier(InstrumentKey("ticker", raw)) if raw else None
        if not identifier:
            continue
        move = moves.get((identifier, preferred[identifier].market))
        if move is None:
            continue
        thresholds = config.by_class.get(h.asset_class)
        if thresholds is None:
            continue
        per_day, cumulative_cap = thresholds.anomaly_per_day, thresholds.anomaly_cumulative_cap
        # `identifier` is already intelligence_identifier(...) (see above),
        # the same form load_leverage_map's keys use — no re-normalization
        # needed here.
        leverage = leverage_map.get(identifier)
        if leverage is not None:
            per_day = per_day * leverage
            cumulative_cap = cumulative_cap * leverage
        window_threshold = _window_threshold(per_day, cumulative_cap, trading_days)
        single_day_hit = move.max_day_pct is not None and abs(move.max_day_pct) >= per_day
        cumulative_hit = abs(move.net_pct) >= window_threshold
        multiplier = leverage if leverage is not None else Decimal(1)
        d3_hit = (
            move.d3_pct is not None and abs(move.d3_pct) >= Decimal(str(rolling.d3)) * multiplier
        )
        d5_hit = (
            move.d5_pct is not None and abs(move.d5_pct) >= Decimal(str(rolling.d5)) * multiplier
        )
        if not (single_day_hit or cumulative_hit or d3_hit or d5_hit):
            continue

        anomaly = PriceAnomaly(
            name=h.name,
            identifier=identifier,
            asset_type=h.asset_class,
            current_price=move.current_price,
            prev_price=move.prev_price,
            pct_change=move.net_pct,
            threshold=window_threshold,
            trigger="single_day"
            if single_day_hit
            else "cumulative"
            if cumulative_hit
            else "d5"
            if d5_hit
            else "d3",
            market=move.market,
            baseline_date=move.baseline_date,
            latest_date=move.latest_date,
            window_net_pct=move.net_pct,
            d3_pct=move.d3_pct,
            d5_pct=move.d5_pct,
            max_day_pct=move.max_day_pct,
            max_day_date=move.max_day_date,
            prev_close=move.prev_close,
            day_open=move.day_open,
            day_high=move.day_high,
            day_low=move.day_low,
            day_close=move.day_close,
            after_hours=move.after_hours,
        )
        # Theme lookup key is the RAW ticker/fund_code uppercased (not the
        # HK-normalized `identifier` above) — matches the pre-split behavior
        # exactly; ticker_themes is keyed by the raw form (issue #128 A1
        # single-user-identical requirement).
        theme_key = (h.ticker or h.fund_code or "").upper()
        theme_row = theme_map.get(theme_key)
        if theme_row is not None:
            theme_buckets.setdefault(theme_row.theme, []).append((h, anomaly))
        else:
            existing = standalone_by_id.get(identifier)
            if existing is None or (h.position is None, h.position or 0) < (
                existing[0].position is None,
                existing[0].position or 0,
            ):
                standalone_by_id[identifier] = (h, anomaly)

    theme_anomalies: list[PriceAnomaly] = []
    for members in theme_buckets.values():
        first_h = members[0][0]
        theme_key = (first_h.ticker or first_h.fund_code or "").upper()
        theme_row = theme_map[theme_key]
        theme_anomalies.append(_merge_theme_anomalies(members, theme_row))

    anomalies = theme_anomalies + [anomaly for _, anomaly in standalone_by_id.values()]
    anomalies.sort(
        key=lambda a: max(
            abs(a.window_net_pct or a.pct_change),
            abs(a.max_day_pct or _RATIO),
            abs(a.d3_pct or Decimal(0)),
            abs(a.d5_pct or Decimal(0)),
        ),
        reverse=True,
    )
    return anomalies


def detect_window_anomalies(
    session: Session,
    start: datetime,
    end: datetime,
    user_id: uuid.UUID,
    moves_cache: MovesCache | None = None,
) -> tuple[list[PriceAnomaly], int]:
    """Single-user anomaly list — composes compute_global_moves() +
    select_user_anomalies() for one user (design doc §3.3, issue #128 A1).

    Pre-A1 this function queried ALL holdings with no user filter at all —
    a real cross-user data leak once more than one user exists (design doc
    §1.3). `user_id` is now required; there is no "give me every user's
    anomalies" call site left, by design.

    `moves_cache`, keyed by the exact (start, end) window, is how a
    multi-user batch (generate_incremental_report's fan-out) shares ONE
    compute_global_moves() call across every user whose window happens to
    match, instead of this wrapper recomputing the global move set from
    scratch on every call — passing a dict that's shared across calls in the
    same batch is what actually enforces "the same identifier's move is
    computed once per window, not once per user" (design doc §3.8), not just
    the existence of compute_global_moves as a separate function. Callers
    that only ever generate one report (manual trigger, most existing tests
    and call sites) omit it and get the pre-A1 per-call behavior, just now
    scoped to one user instead of leaking every user's holdings.
    """
    moves, trading_days = resolve_global_moves(session, start, end, moves_cache)
    holdings = user_holdings(session, user_id)
    theme_map = _load_theme_map(session)
    leverage_map = load_leverage_map(session)
    anomalies = select_user_anomalies(moves, holdings, trading_days, theme_map, leverage_map)
    return anomalies, trading_days
