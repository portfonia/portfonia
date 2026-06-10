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

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.timezones import ET
from app.models.holding import Holding
from app.models.news import News
from app.models.price_snapshot import PriceSnapshot
from app.models.report import Report
from app.services.news_fetcher import NewsItem
from app.services.price_anomaly_detector import PriceAnomaly

logger = logging.getLogger(__name__)

# Cold start: the first report covers from US regular close on 2026-06-01.
BOOTSTRAP_WATERMARK = datetime(2026, 6, 1, 16, 0, tzinfo=ET)

_RATIO = Decimal("0.0001")  # 4 dp for pct_change
# Per-trading-day move thresholds; the window threshold scales these by the
# number of trading days it spans (a 5-day window tolerates more drift than a
# 1-day one), capped so any move beyond _MAX_THRESHOLD is always flagged.
_ASSET_THRESHOLDS: dict[str, Decimal] = {"stock": Decimal("0.03"), "etf": Decimal("0.02")}
_MAX_THRESHOLD = Decimal("0.10")


def _window_threshold(per_day: Decimal, trading_days: int) -> Decimal:
    """flat% x trading_days, capped at 10% (>10% always flags)."""
    return min(per_day * max(trading_days, 1), _MAX_THRESHOLD)


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


def _close_snapshot_on_or_before(session: Session, ticker: str, on: date) -> PriceSnapshot | None:
    return session.execute(
        select(PriceSnapshot)
        .where(
            PriceSnapshot.ticker == ticker,
            PriceSnapshot.session_node == "close",
            PriceSnapshot.close.is_not(None),
            PriceSnapshot.trade_date <= on,
        )
        .order_by(PriceSnapshot.trade_date.desc())
        .limit(1)
    ).scalar_one_or_none()


def _window_closes(
    session: Session, ticker: str, start: datetime, end: datetime
) -> list[PriceSnapshot]:
    """Daily close snapshots captured within the report window, oldest first.

    Normally this is "trade_date in (start_date, end_date]" — clean date-level
    bounds for multi-day windows. When the window starts and ends on the same
    ET calendar date (a same-day rerun), that range is empty by construction
    even if today's close was captured during the window — so for that case we
    fall back to "today's close, if it was captured after period_start". This
    keeps multi-day windows untouched (no dependency on captured_at, which is
    stale/uniform for backfilled history) while fixing the same-day collapse.
    """
    start_date = start.astimezone(ET).date()
    end_date = end.astimezone(ET).date()
    conditions = [
        PriceSnapshot.ticker == ticker,
        PriceSnapshot.session_node == "close",
        PriceSnapshot.close.is_not(None),
    ]
    if start_date < end_date:
        conditions += [
            PriceSnapshot.trade_date > start_date,
            PriceSnapshot.trade_date <= end_date,
        ]
    else:
        conditions += [
            PriceSnapshot.trade_date == end_date,
            PriceSnapshot.captured_at > start,
        ]
    return list(
        session.execute(
            select(PriceSnapshot).where(*conditions).order_by(PriceSnapshot.trade_date.asc())
        )
        .scalars()
        .all()
    )


def _after_hours_last(session: Session, ticker: str, on: date) -> Decimal | None:
    snap = session.execute(
        select(PriceSnapshot).where(
            PriceSnapshot.ticker == ticker,
            PriceSnapshot.session_node == "after_close",
            PriceSnapshot.trade_date == on,
        )
    ).scalar_one_or_none()
    return snap.last if snap else None


def detect_window_anomalies(
    session: Session, start: datetime, end: datetime
) -> tuple[list[PriceAnomaly], int]:
    """Price moves over the report window, computed from stored snapshots.

    A holding flags as an anomaly when EITHER condition holds (points 7 + 10):

      * single-day  — any one trading day inside the window moved beyond the
        per-day threshold (stock 3 %, etf 2 %). This is what catches a violent
        session that the endpoint-to-endpoint net move smooths away.
      * cumulative  — the baseline-close → latest-close net move beyond the
        scaled window threshold (per-day x trading-days, capped at 10 %).

    For every flagged holding we also attach the most recent trading day's
    session arc (prior close, OHLC, after-hours) so the report can state the
    comparison basis and describe how the day ran, not just a percentage.

    A holding with no baseline close (added mid-window) is skipped. Returns
    (anomalies sorted by largest |move|, trading_days_in_window).
    """
    holdings = (
        session.execute(
            select(Holding).where(Holding.ticker.is_not(None), Holding.pricing_mode == "auto")
        )
        .scalars()
        .all()
    )
    start_date = start.astimezone(ET).date()
    end_date = end.astimezone(ET).date()

    if start_date < end_date:
        trading_days = int(
            session.execute(
                select(func.count(func.distinct(PriceSnapshot.trade_date))).where(
                    PriceSnapshot.session_node == "close",
                    PriceSnapshot.trade_date > start_date,
                    PriceSnapshot.trade_date <= end_date,
                )
            ).scalar_one()
            or 0
        )
    else:
        # Same-day window: a trading day "in the window" means today's close was
        # captured after period_start (see _window_closes).
        trading_days = int(
            session.execute(
                select(func.count(func.distinct(PriceSnapshot.trade_date))).where(
                    PriceSnapshot.session_node == "close",
                    PriceSnapshot.close.is_not(None),
                    PriceSnapshot.trade_date == end_date,
                    PriceSnapshot.captured_at > start,
                )
            ).scalar_one()
            or 0
        )

    anomalies: list[PriceAnomaly] = []
    for h in holdings:
        per_day = _ASSET_THRESHOLDS.get(h.asset_type or "")
        if per_day is None or not h.ticker:
            continue
        baseline = _close_snapshot_on_or_before(session, h.ticker, start_date)
        series = _window_closes(session, h.ticker, start, end)
        if baseline is None or baseline.close is None or not series:
            continue
        latest = series[-1]
        if latest.close is None or baseline.close == 0:
            continue

        # Full close path: baseline followed by every in-window close.
        path = [baseline, *series]
        net_pct = ((latest.close - baseline.close) / baseline.close).quantize(_RATIO)

        # Largest single-day move along the path (signed, keep the worst |move|).
        max_day_pct: Decimal | None = None
        max_day_date: date | None = None
        for prev, cur in pairwise(path):
            if prev.close is None or cur.close is None or prev.close == 0:
                continue
            day_pct = ((cur.close - prev.close) / prev.close).quantize(_RATIO)
            if max_day_pct is None or abs(day_pct) > abs(max_day_pct):
                max_day_pct = day_pct
                max_day_date = cur.trade_date

        window_threshold = _window_threshold(per_day, trading_days)
        single_day_hit = max_day_pct is not None and abs(max_day_pct) >= per_day
        cumulative_hit = abs(net_pct) >= window_threshold
        if not (single_day_hit or cumulative_hit):
            continue

        # A violent single session is labelled "single_day" even if the net also
        # cleared the cumulative bar; a quiet drift past the (capped) cumulative
        # threshold with no big day is "cumulative". The >10%-always-flags tier
        # falls out naturally (a >10% day is single_day; a >10% net with small
        # days is cumulative, since the cap makes window_threshold <= 10%).
        trigger = "single_day" if single_day_hit else "cumulative"

        prev_close = path[-2].close if len(path) >= 2 else None
        anomalies.append(
            PriceAnomaly(
                name=h.name,
                identifier=h.ticker,
                asset_type=h.asset_type or "stock",
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
                after_hours=_after_hours_last(session, h.ticker, latest.trade_date),
            )
        )

    anomalies.sort(
        key=lambda a: max(abs(a.window_net_pct or a.pct_change), abs(a.max_day_pct or _RATIO)),
        reverse=True,
    )
    return anomalies, trading_days
