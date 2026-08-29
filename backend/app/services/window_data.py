"""Incremental-report window data (ADR-002 report layer).

The report covers `[period_start, period_end]` where period_start is the user's
watermark (previous report's period_end, derived — not a stored pointer) and
period_end is the run cutoff. News and price moves over that window are read from
the capture-layer stores, never re-fetched live.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from itertools import pairwise

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.timezones import ET
from app.models.holding import Holding
from app.models.news import News
from app.models.news_surfaced import NewsSurfaced
from app.models.price_snapshot import PriceSnapshot
from app.models.report import Report
from app.models.ticker_theme import TickerTheme
from app.services._yfinance import _normalize_ticker
from app.services.asset_class_config import load_asset_class_config
from app.services.news_fetcher import NewsItem
from app.services.price_anomaly_detector import ConstituentMove, PriceAnomaly
from app.services.user_scope import global_identifier_universe, user_holdings

logger = logging.getLogger(__name__)

# Retired from the production watermark path (Ring 1-B §6.6). Kept as a
# fixed fixture timestamp for tests that need a historical baseline — a new
# user with no DONE reports now uses `cold_start_watermark(now)` instead.
BOOTSTRAP_WATERMARK = datetime(2026, 6, 1, 16, 0, tzinfo=ET)
COLD_START_WEEKDAYS = 5

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
    """ET midnight of the date ``COLD_START_WEEKDAYS`` weekdays before ``now``.

    Pure function of the given timestamp — must not call ``datetime.now()``.
    ``generate_report`` already stamps a batch ``now``; using wall-clock here
    would desync the window from that batch (Ring 1-B design.md §2.3 / §6.6).
    """
    cursor = now.astimezone(ET).date()
    remaining = COLD_START_WEEKDAYS
    while remaining > 0:
        cursor -= timedelta(days=1)
        if cursor.weekday() < 5:
            remaining -= 1
    return datetime(cursor.year, cursor.month, cursor.day, tzinfo=ET)


def user_has_done_history(
    session: Session,
    user_id: object,
    report_type: str,
    exclude_report_id: object | None = None,
) -> bool:
    """True if this user has any DONE report of this type (optionally excluding one)."""
    stmt = (
        select(func.count())
        .select_from(Report)
        .where(
            Report.user_id == user_id,
            Report.report_type == report_type,
            Report.status.in_(_DONE_STATUSES),
        )
    )
    if exclude_report_id is not None:
        stmt = stmt.where(Report.id != exclude_report_id)
    return int(session.execute(stmt).scalar_one()) > 0


def user_watermark(
    session: Session,
    user_id: object,
    report_type: str,
    exclude_report_id: object | None = None,
    now: datetime | None = None,
) -> datetime:
    """period_start for the next report = max(period_end) over the user's completed
    reports of this type, or five weekdays before ``now`` when there are none.

    ``exclude_report_id`` drops the report currently being (re)generated from the
    watermark. Without it, regenerating an existing failed/needs_review/skipped row
    would read that row's OWN period_end back as its period_start — collapsing the
    window to a few minutes (the session uses autoflush=False, so the in-flight
    status reset is not yet visible to this query). Always pass the row's id when
    regenerating in place.

    ``now`` is required on the cold-start path and must be the same timestamp
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
    if latest is not None:
        return latest
    if now is None:
        raise ValueError("now is required to compute a cold-start watermark")
    return cold_start_watermark(now)


def backfill_news_surfaced_before(session: Session, user_id: uuid.UUID, cutoff: datetime) -> int:
    """Mark news published strictly before ``cutoff`` as already surfaced.

    Used for a brand-new user so ``load_news_window`` (no lower bound) does
    not swallow the whole capture table on their first report. ``report_id``
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


def _news_item(r: News) -> NewsItem:
    return NewsItem(
        url_hash=r.url_hash,
        title=r.title,
        url=r.url,
        source=r.source,
        published_at=r.published_at,
        summary=r.summary or "",
    )


def day_window_bounds(trade_date: date) -> tuple[datetime, datetime]:
    """The [00:00, 24:00) ET bounds of one ET calendar day — L1's own window
    (design doc §4.8, second addendum), a pure function of `trade_date` alone.

    No `Session`, no `user_id`, no `Report` row read anywhere in this
    function's body — that is deliberate, not an oversight: it is what makes
    it structurally impossible for a per-user report watermark to leak into
    L1's window the way `[period_start, period_end]` did (the round-5 bug).
    Passing these bounds into `resolve_global_moves`/`compute_global_moves`
    yields each identifier's single trading day's move (latest close vs. the
    most recent close before this day), not a multi-day cumulative change —
    see `ticker_intel.build_l1_facts`'s docstring for why that distinction
    matters and how the result is consumed.
    """
    start = datetime.combine(trade_date, time.min, tzinfo=ET)
    end = datetime.combine(trade_date, time.max, tzinfo=ET)
    return start, end


# L1 lookback length (issue #128 quality gate). A weekday list ending on
# trade_date — not a user's report watermark. Keep generate_report and the
# A1 "moves computed once per window" test on this same number.
L1_LOOKBACK_TRADING_DAYS = 5


def lookback_trading_dates(end: date, n: int = L1_LOOKBACK_TRADING_DAYS) -> list[date]:
    """``n`` dates ending on ``end``, oldest first; ``end`` is always
    included, and only the preceding dates are restricted to weekdays.

    Pure function of ``end`` — no Session, no user_id, no report watermark.
    That is the point: L1 may carry multi-day headlines and own-price path,
    but the date list cannot come from a user's ``period_start``. Weekends
    are skipped; exchange holidays are not (this is a weekday calendar, not
    an exchange calendar). ``n < 1`` returns an empty list.

    ``end`` is always the list's last element, even when ``end`` itself
    falls on a weekend (issue #178) — ``report_generator.py``'s only caller
    passes ``eff_date`` here (the real ET calendar date a manual run happens
    to fire on, with no weekday normalization) and then looks up
    ``lookback_moves.get(eff_date, {})``, so silently dropping ``end`` from
    this list — as the naive "skip anything that isn't a weekday, including
    the very first cursor" loop used to — made that lookup always miss,
    which made every L1 candidate's ``day_pct`` come out ``None`` and get
    silently skipped, regardless of whether a real price close existed for
    that date. Only the ``n - 1`` days *before* ``end`` are weekday-filtered.
    """
    if n < 1:
        return []
    out: list[date] = [end]
    cursor = end - timedelta(days=1)
    while len(out) < n:
        if cursor.weekday() < 5:
            out.append(cursor)
        cursor -= timedelta(days=1)
    out.reverse()
    return out


def load_day_news(session: Session, trade_date: date) -> list[NewsItem]:
    """News published on `trade_date` (one ET calendar day) — L1's own
    recall source (design doc §4.8, second addendum).

    Deliberately has NO `user_id` parameter and never touches
    `news_surfaced`: that ledger is a per-user Pass-2 dedup mechanism (a
    user's own report never re-shows them a headline they've already seen),
    and routing L1 through it would make L1's candidate news set depend on
    which user's report happens to run first in a fan-out — the same
    per-user-contamination class `l1_identifiers_for_user`/`build_l1_facts`
    already close off for identifiers and price moves. `load_news_window`
    (per-user, ledger-aware) remains Pass 2's own source and is untouched.
    """
    start, end = day_window_bounds(trade_date)
    rows = (
        session.execute(
            select(News)
            .where(News.published_at >= start, News.published_at <= end)
            .order_by(News.published_at.desc())
        )
        .scalars()
        .all()
    )
    return [_news_item(r) for r in rows]


def load_news_window(
    session: Session, _start: datetime, end: datetime, user_id: uuid.UUID
) -> list[NewsItem]:
    """News published at/before the window cutoff that hasn't yet been surfaced
    in any of THIS USER's DONE-status reports, newest first, from the `news`
    store.

    H-DEBT-3 / issue #30: this used to be a strict ``(start, end]`` range. A
    news item published inside a window but not ingested until after that
    window's period_end fell through BOTH the window it belongs to (not yet
    ingested when that window was selected) and the next window (excluded by
    the `> start` lower bound) — a permanent miss. `_start` is kept in the
    signature (every call site already threads a window) but intentionally
    unused as a lower bound now; dedup is instead delegated entirely to
    `news_surfaced` via `mark_news_surfaced`, which the caller invokes once
    this report reaches a DONE status.

    Scoped per `user_id` (PR #139 review): `news` is a global capture-layer
    store, but each user's report stream has its own watermark/window, so the
    same news item can legitimately need to surface once for each user —
    marking it surfaced for one user must not hide it from another.
    """
    surfaced = select(NewsSurfaced.news_id).where(NewsSurfaced.user_id == user_id)
    rows = (
        session.execute(
            select(News)
            .where(News.published_at <= end, News.id.not_in(surfaced))
            .order_by(News.published_at.desc())
        )
        .scalars()
        .all()
    )
    return [_news_item(r) for r in rows]


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
    session: Session, ticker: str, start: datetime, start_date: date
) -> PriceSnapshot | None:
    """Most recent close strictly before the report window opens.

    Normally this is the latest close with trade_date < start_date. But if
    period_start falls BEFORE that day's market close (e.g. a premarket manual
    run), start_date's own close is captured DURING the window, not before it
    — _window_closes (below) pulls it into the window via the same
    captured_at > start test, so it must not also double as the baseline here.
    A trade_date == start_date close only counts as pre-window when it was
    captured at/before period_start.
    """
    return session.execute(
        select(PriceSnapshot)
        .where(
            PriceSnapshot.ticker == ticker,
            PriceSnapshot.session_node == "close",
            PriceSnapshot.close.is_not(None),
            or_(
                PriceSnapshot.trade_date < start_date,
                and_(
                    PriceSnapshot.trade_date == start_date,
                    PriceSnapshot.captured_at <= start,
                ),
            ),
        )
        .order_by(PriceSnapshot.trade_date.desc())
        .limit(1)
    ).scalar_one_or_none()


def _window_closes(
    session: Session, ticker: str, start: datetime, end: datetime
) -> list[PriceSnapshot]:
    """Daily close snapshots captured within the report window, oldest first.

    A close on trade_date D is in-window if D is strictly after start_date and
    on/before end_date (the normal multi-day case), OR D == start_date and the
    close was captured AFTER period_start (period_start fell before that day's
    market close — a premarket/intraday run — so the close happened during the
    window, not before it). This single condition also covers the same-day
    case (start_date == end_date), where the first clause is empty by
    construction.
    """
    start_date = start.astimezone(ET).date()
    end_date = end.astimezone(ET).date()
    conditions = [
        PriceSnapshot.ticker == ticker,
        PriceSnapshot.session_node == "close",
        PriceSnapshot.close.is_not(None),
        or_(
            and_(PriceSnapshot.trade_date > start_date, PriceSnapshot.trade_date <= end_date),
            and_(PriceSnapshot.trade_date == start_date, PriceSnapshot.captured_at > start),
        ),
    ]
    return list(
        session.execute(
            select(PriceSnapshot).where(*conditions).order_by(PriceSnapshot.trade_date.asc())
        )
        .scalars()
        .all()
    )


def latest_window_close_date(session: Session, start: datetime, end: datetime) -> date | None:
    """Most recent trade_date whose close was captured within the report window.

    This is the real cutoff of the report's PRICE data — distinct from period_end
    (the wall-clock cutoff). A premarket/intraday manual run has period_end this
    morning but price data only through the prior session's close; stating that
    explicitly (R-5) stops a reader assuming the report reflects intraday/premarket
    moves it never had. Membership matches _window_closes / detect_window_anomalies.
    """
    start_date = start.astimezone(ET).date()
    end_date = end.astimezone(ET).date()
    return session.execute(
        select(func.max(PriceSnapshot.trade_date)).where(
            PriceSnapshot.session_node == "close",
            PriceSnapshot.close.is_not(None),
            or_(
                and_(PriceSnapshot.trade_date > start_date, PriceSnapshot.trade_date <= end_date),
                and_(PriceSnapshot.trade_date == start_date, PriceSnapshot.captured_at > start),
            ),
        )
    ).scalar_one_or_none()


def _after_hours_last(session: Session, ticker: str, on: date) -> Decimal | None:
    snap = session.execute(
        select(PriceSnapshot).where(
            PriceSnapshot.ticker == ticker,
            PriceSnapshot.session_node == "after_close",
            PriceSnapshot.trade_date == on,
        )
    ).scalar_one_or_none()
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


# Keyed by the exact (start, end) window a batch fan-out is generating over —
# see detect_window_anomalies' `moves_cache` parameter.
MovesCache = dict[tuple[datetime, datetime], tuple[dict[str, "HoldingMove"], int]]


def _compute_identifier_move(
    session: Session, identifier: str, start: datetime, end: datetime, start_date: date
) -> HoldingMove | None:
    """Fetch + compute the window price move for one identifier; return None
    when there's no usable baseline/series. No threshold judgment here (see
    HoldingMove) — ported out of the old per-holding `_compute_holding_move`,
    which mixed "fetch the price series" with "does it clear THIS holding's
    threshold" and so recomputed the same identifier's series once per
    holding row that carried it, once per user.
    """
    baseline = _close_snapshot_before_window(session, identifier, start, start_date)
    series = _window_closes(session, identifier, start, end)
    if baseline is None or baseline.close is None or not series:
        return None
    latest = series[-1]
    if latest.close is None or baseline.close == 0:
        return None

    path = [baseline, *series]
    net_pct = ((latest.close - baseline.close) / baseline.close).quantize(_RATIO)

    max_day_pct: Decimal | None = None
    max_day_date: date | None = None
    for prev, cur in pairwise(path):
        if prev.close is None or cur.close is None or prev.close == 0:
            continue
        day_pct = ((cur.close - prev.close) / prev.close).quantize(_RATIO)
        if max_day_pct is None or abs(day_pct) > abs(max_day_pct):
            max_day_pct = day_pct
            max_day_date = cur.trade_date

    prev_close = path[-2].close if len(path) >= 2 else None
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
        prev_close=prev_close,
        day_open=latest.open,
        day_high=latest.high,
        day_low=latest.low,
        day_close=latest.close,
        after_hours=_after_hours_last(session, identifier, latest.trade_date),
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

    # Sort by current_value descending; fall back to 0 when value is unknown.
    def _val(h: Holding) -> Decimal:
        return h.current_value or Decimal("0")

    flagged_sorted = sorted(flagged, key=lambda t: _val(t[0]), reverse=True)
    _dominant_h, dominant_a = flagged_sorted[0]

    total_value = sum(_val(h) for h, _ in flagged)
    if total_value == 0:
        # Equal-weight fallback when no values are available.
        weighted_pct = (
            sum((a.pct_change for _, a in flagged), Decimal("0")) / len(flagged)
        ).quantize(_RATIO)
    else:
        weighted_pct = (sum(_val(h) * a.pct_change for h, a in flagged) / total_value).quantize(
            _RATIO
        )

    constituents = [
        ConstituentMove(
            name=h.name,
            identifier=a.identifier,
            pct_change=a.pct_change,
            current_value=_val(h),
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


def _count_trading_days(session: Session, start: datetime, start_date: date, end: datetime) -> int:
    """Distinct trade_dates with a captured close inside the window — shared by
    compute_global_moves and (via the wrapper) every caller of the old
    detect_window_anomalies signature."""
    end_date = end.astimezone(ET).date()
    return int(
        session.execute(
            select(func.count(func.distinct(PriceSnapshot.trade_date))).where(
                PriceSnapshot.session_node == "close",
                PriceSnapshot.close.is_not(None),
                or_(
                    and_(
                        PriceSnapshot.trade_date > start_date,
                        PriceSnapshot.trade_date <= end_date,
                    ),
                    and_(
                        PriceSnapshot.trade_date == start_date,
                        PriceSnapshot.captured_at > start,
                    ),
                ),
            )
        ).scalar_one()
        or 0
    )


def _load_theme_map(session: Session) -> dict[str, TickerTheme]:
    """ticker (upper) -> TickerTheme row."""
    return {row.ticker.upper(): row for row in session.execute(select(TickerTheme)).scalars().all()}


def compute_global_moves(
    session: Session, start: datetime, end: datetime
) -> tuple[dict[str, HoldingMove], int]:
    """Every identifier across ALL users' auto-priced holdings (design doc
    §1.3/§3.3, issue #128 A1), window price move computed exactly once each.

    price_capture's identifier universe is already global (no user_id
    filter — see design doc §1.3); this makes that explicit and shares the
    snapshot query + move computation across every user who happens to hold
    the same identifier, instead of recomputing it once per Holding row (the
    pre-A1 behavior, which meant N users each holding the same ticker paid
    for the same query N times).

    No threshold judgment happens here — see `select_user_anomalies`.
    Returns (identifier(upper) -> HoldingMove, trading_days_in_window).
    """
    universe = global_identifier_universe(session)
    start_date = start.astimezone(ET).date()
    trading_days = _count_trading_days(session, start, start_date, end)

    moves: dict[str, HoldingMove] = {}
    for identifier in universe:
        move = _compute_identifier_move(session, identifier, start, end, start_date)
        if move is not None:
            moves[identifier] = move
    return moves, trading_days


def resolve_global_moves(
    session: Session,
    start: datetime,
    end: datetime,
    moves_cache: MovesCache | None = None,
) -> tuple[dict[str, HoldingMove], int]:
    """`compute_global_moves` behind the batch-shared `moves_cache` — the one
    place the cache-or-compute decision lives.

    Public because the global move set has a SECOND consumer besides anomaly
    detection: the L1 shared ticker-intel cache (issue #128 A2) sources every
    numeric fact it caches from here. That consumer must never re-derive
    those numbers from `select_user_anomalies`' per-user output (design doc
    §4.8 addendum — three consecutive review rounds found a different
    per-user field leaking into the shared cache that way), and it must not
    pay for a second full computation to avoid doing so either.
    """
    cache_key = (start, end)
    cached = moves_cache.get(cache_key) if moves_cache is not None else None
    if cached is None:
        cached = compute_global_moves(session, start, end)
        if moves_cache is not None:
            moves_cache[cache_key] = cached
    return cached


def select_user_anomalies(
    moves: dict[str, HoldingMove],
    holdings: Sequence[Holding],
    trading_days: int,
    theme_map: dict[str, TickerTheme],
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
    """
    config = load_asset_class_config()
    theme_buckets: dict[str, list[tuple[Holding, PriceAnomaly]]] = {}
    standalone: list[PriceAnomaly] = []

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
        identifier = _normalize_ticker(raw).upper() if raw else None
        if not identifier:
            continue
        move = moves.get(identifier)
        if move is None:
            continue
        thresholds = config.by_class.get(h.asset_class)
        if thresholds is None:
            continue
        per_day, cumulative_cap = thresholds.anomaly_per_day, thresholds.anomaly_cumulative_cap
        window_threshold = _window_threshold(per_day, cumulative_cap, trading_days)
        single_day_hit = move.max_day_pct is not None and abs(move.max_day_pct) >= per_day
        cumulative_hit = abs(move.net_pct) >= window_threshold
        if not (single_day_hit or cumulative_hit):
            continue

        anomaly = PriceAnomaly(
            name=h.name,
            identifier=identifier,
            asset_type=h.asset_class,
            current_price=move.current_price,
            prev_price=move.prev_price,
            pct_change=move.net_pct,
            threshold=window_threshold,
            trigger="single_day" if single_day_hit else "cumulative",
            market=move.market,
            baseline_date=move.baseline_date,
            latest_date=move.latest_date,
            window_net_pct=move.net_pct,
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
            standalone.append(anomaly)

    theme_anomalies: list[PriceAnomaly] = []
    for members in theme_buckets.values():
        first_h = members[0][0]
        theme_key = (first_h.ticker or first_h.fund_code or "").upper()
        theme_row = theme_map[theme_key]
        theme_anomalies.append(_merge_theme_anomalies(members, theme_row))

    anomalies = theme_anomalies + standalone
    anomalies.sort(
        key=lambda a: max(abs(a.window_net_pct or a.pct_change), abs(a.max_day_pct or _RATIO)),
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
    anomalies = select_user_anomalies(moves, holdings, trading_days, theme_map)
    return anomalies, trading_days
