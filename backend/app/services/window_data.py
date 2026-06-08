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


def user_watermark(session: Session, user_id: object, report_type: str) -> datetime:
    """period_start for the next report = max(period_end) over the user's completed
    reports of this type, or the cold-start baseline when there are none."""
    latest = session.execute(
        select(func.max(Report.period_end)).where(
            Report.user_id == user_id,
            Report.report_type == report_type,
            Report.status.in_(_DONE_STATUSES),
        )
    ).scalar_one_or_none()
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


def _latest_close_on_or_before(session: Session, ticker: str, on: date) -> PriceSnapshot | None:
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


def detect_window_anomalies(
    session: Session, start: datetime, end: datetime
) -> tuple[list[PriceAnomaly], int]:
    """Price moves since the baseline close, computed from stored snapshots.

    Baseline = the close at/just-before period_start; latest = the close
    at/just-before period_end. A holding with no baseline (added mid-window) is
    skipped. Returns (anomalies sorted by |move|, trading_days_in_window).
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

    anomalies: list[PriceAnomaly] = []
    for h in holdings:
        per_day = _ASSET_THRESHOLDS.get(h.asset_type or "")
        if per_day is None or not h.ticker:
            continue
        baseline = _latest_close_on_or_before(session, h.ticker, start_date)
        latest = _latest_close_on_or_before(session, h.ticker, end_date)
        if baseline is None or latest is None or baseline.close is None or latest.close is None:
            continue
        if latest.trade_date <= baseline.trade_date or baseline.close == 0:
            continue
        pct = ((latest.close - baseline.close) / baseline.close).quantize(_RATIO)
        threshold = _window_threshold(per_day, trading_days)
        if abs(pct) >= threshold:
            anomalies.append(
                PriceAnomaly(
                    name=h.name,
                    identifier=h.ticker,
                    asset_type=h.asset_type or "stock",
                    current_price=latest.close,
                    prev_price=baseline.close,
                    pct_change=pct,
                    threshold=threshold,  # the effective (scaled) threshold applied
                )
            )
    anomalies.sort(key=lambda a: abs(a.pct_change), reverse=True)

    return anomalies, trading_days
