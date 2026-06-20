"""Incremental-report window data (ADR-002 report layer).

The report covers `[period_start, period_end]` where period_start is the user's
watermark (previous report's period_end, derived — not a stored pointer) and
period_end is the run cutoff. News and price moves over that window are read from
the capture-layer stores, never re-fetched live.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from decimal import Decimal
from itertools import pairwise

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.core.timezones import ET
from app.models.holding import Holding
from app.models.news import News
from app.models.price_snapshot import PriceSnapshot
from app.models.report import Report
from app.models.ticker_theme import TickerTheme
from app.services.news_fetcher import NewsItem
from app.services.price_anomaly_detector import ConstituentMove, PriceAnomaly

logger = logging.getLogger(__name__)

# Cold start: the first report covers from US regular close on 2026-06-01.
BOOTSTRAP_WATERMARK = datetime(2026, 6, 1, 16, 0, tzinfo=ET)

_RATIO = Decimal("0.0001")  # 4 dp for pct_change

# Per-asset-class thresholds: (per_day_trigger, cumulative_window_cap).
#
# The window threshold = per_day * trading_days, capped at the class cap.
# Broad funds have a high cap (40%) so a normal weekly drift never fires;
# individual stocks cap at 10% (any week-long run above that is noteworthy).
# Per-day trigger is the same (5%) for most equity classes so a single
# violent session is still caught regardless of cumulative behaviour.
_ASSET_THRESHOLDS: dict[str, tuple[Decimal, Decimal]] = {
    # Individual equities
    "STOCK": (Decimal("0.05"), Decimal("0.10")),
    # US equity — split by index/style; classified by underlying exposure, not listing location
    "EQUITY_US_BROAD": (Decimal("0.05"), Decimal("0.40")),  # S&P 500 / total market
    "EQUITY_US_TECH": (Decimal("0.05"), Decimal("0.35")),  # Nasdaq 100 / tech-heavy
    # Geographic equity buckets
    "EQUITY_DM": (Decimal("0.05"), Decimal("0.30")),  # non-US developed markets
    "EQUITY_CN": (Decimal("0.05"), Decimal("0.20")),  # China (A-share / HK / QDII)
    "EQUITY_EM": (Decimal("0.05"), Decimal("0.25")),  # EM ex-China
    # Catch-all for unclassified global / sector funds
    "EQUITY_BROAD": (Decimal("0.05"), Decimal("0.40")),
    "EQUITY_REGION": (Decimal("0.04"), Decimal("0.20")),  # legacy; migrate to specific bucket
    "EQUITY_SECTOR": (Decimal("0.04"), Decimal("0.20")),
    # Non-equity
    "COMMODITY": (Decimal("0.04"), Decimal("0.20")),
    "BOND_FUND": (Decimal("0.02"), Decimal("0.20")),
    "CASH_EQUIV": (Decimal("0.01"), Decimal("0.20")),
}


def _window_threshold(per_day: Decimal, cap: Decimal, trading_days: int) -> Decimal:
    """per_day * trading_days, capped at the per-class cumulative cap."""
    return min(per_day * max(trading_days, 1), cap)


# Completed statuses whose period_end counts toward the watermark.
_DONE_STATUSES = ("success", "skipped", "needs_review")


def user_watermark(
    session: Session,
    user_id: object,
    report_type: str,
    exclude_report_id: object | None = None,
) -> datetime:
    """period_start for the next report = max(period_end) over the user's completed
    reports of this type, or the cold-start baseline when there are none.

    ``exclude_report_id`` drops the report currently being (re)generated from the
    watermark. Without it, regenerating an existing failed/needs_review/skipped row
    would read that row's OWN period_end back as its period_start — collapsing the
    window to a few minutes (the session uses autoflush=False, so the in-flight
    status reset is not yet visible to this query). Always pass the row's id when
    regenerating in place.
    """
    stmt = select(func.max(Report.period_end)).where(
        Report.user_id == user_id,
        Report.report_type == report_type,
        Report.status.in_(_DONE_STATUSES),
    )
    if exclude_report_id is not None:
        stmt = stmt.where(Report.id != exclude_report_id)
    latest = session.execute(stmt).scalar_one_or_none()
    return latest or BOOTSTRAP_WATERMARK


def load_news_window(session: Session, start: datetime, end: datetime) -> list[NewsItem]:
    """News captured in (start, end], newest first, from the `news` store."""
    rows = (
        session.execute(
            select(News)
            .where(News.published_at > start, News.published_at <= end)
            .order_by(News.published_at.desc())
        )
        .scalars()
        .all()
    )
    return [
        NewsItem(
            url_hash=r.url_hash,
            title=r.title,
            url=r.url,
            source=r.source,
            published_at=r.published_at,
            summary=r.summary or "",
        )
        for r in rows
    ]


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


def _compute_holding_move(
    session: Session,
    h: Holding,
    start: datetime,
    end: datetime,
    start_date: date,
    trading_days: int,
) -> PriceAnomaly | None:
    """Compute the window move for a single holding; return None if not anomalous.

    Uses h.ticker when present; falls back to h.fund_code as the price_snapshots
    key (fund NAV is stored under fund_code via capture_fund_navs).
    """
    identifier = h.ticker or h.fund_code
    thresholds = _ASSET_THRESHOLDS.get(h.asset_class)
    if thresholds is None or not identifier:
        return None
    per_day, cumulative_cap = thresholds
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

    window_threshold = _window_threshold(per_day, cumulative_cap, trading_days)
    single_day_hit = max_day_pct is not None and abs(max_day_pct) >= per_day
    cumulative_hit = abs(net_pct) >= window_threshold
    if not (single_day_hit or cumulative_hit):
        return None

    trigger = "single_day" if single_day_hit else "cumulative"
    prev_close = path[-2].close if len(path) >= 2 else None
    return PriceAnomaly(
        name=h.name,
        identifier=identifier,
        asset_type=h.asset_class,
        current_price=latest.close,
        prev_price=baseline.close,
        pct_change=net_pct,
        threshold=window_threshold,
        trigger=trigger,
        market=latest.market,
        baseline_date=baseline.trade_date,
        latest_date=latest.trade_date,
        window_net_pct=net_pct,
        max_day_pct=max_day_pct,
        max_day_date=max_day_date,
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


def detect_window_anomalies(
    session: Session, start: datetime, end: datetime
) -> tuple[list[PriceAnomaly], int]:
    """Price moves over the report window, computed from stored snapshots.

    A holding flags as an anomaly when EITHER condition holds:

      * single-day  — any one trading day inside the window moved beyond the
        per-day threshold. Catches a violent session that the endpoint-to-
        endpoint net move would smooth away.
      * cumulative  — the baseline-close → latest-close net move beyond the
        scaled window threshold (per-day x trading-days, capped at the per-class
        cumulative cap).

    Holdings that share a theme in ``ticker_themes`` are merged into a single
    anomaly entry if ANY constituent flags.  The headline pct_change is the
    value-weighted average across all theme members; the session arc comes from
    the value-dominant constituent.  Holdings with no theme entry are emitted as
    individual anomaly rows.

    A holding with no baseline close (added mid-window) is skipped. Returns
    (anomalies sorted by largest |move|, trading_days_in_window).
    """
    holdings = (
        session.execute(
            select(Holding).where(
                or_(Holding.ticker.is_not(None), Holding.fund_code.is_not(None)),
                Holding.pricing_mode == "auto",
            )
        )
        .scalars()
        .all()
    )
    start_date = start.astimezone(ET).date()
    end_date = end.astimezone(ET).date()

    trading_days = int(
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

    # Load theme map: ticker (upper) → TickerTheme row.
    theme_map: dict[str, TickerTheme] = {
        row.ticker.upper(): row for row in session.execute(select(TickerTheme)).scalars().all()
    }

    # Compute per-holding moves; bucket into themed vs standalone.
    # theme_buckets: theme key → [(Holding, PriceAnomaly)]
    theme_buckets: dict[str, list[tuple[Holding, PriceAnomaly]]] = {}
    standalone: list[PriceAnomaly] = []

    for h in holdings:
        anomaly = _compute_holding_move(session, h, start, end, start_date, trading_days)
        if anomaly is None:
            continue
        identifier = (h.ticker or h.fund_code or "").upper()
        theme_row = theme_map.get(identifier)
        if theme_row is not None:
            theme_buckets.setdefault(theme_row.theme, []).append((h, anomaly))
        else:
            standalone.append(anomaly)

    # Build merged theme anomalies.
    theme_anomalies: list[PriceAnomaly] = []
    for _theme_key, members in theme_buckets.items():
        # Use the first member's TickerTheme row (all share the same theme).
        first_h = members[0][0]
        identifier_upper = (first_h.ticker or first_h.fund_code or "").upper()
        theme_row = theme_map[identifier_upper]
        theme_anomalies.append(_merge_theme_anomalies(members, theme_row))

    anomalies = theme_anomalies + standalone
    anomalies.sort(
        key=lambda a: max(abs(a.window_net_pct or a.pct_change), abs(a.max_day_pct or _RATIO)),
        reverse=True,
    )
    return anomalies, trading_days
