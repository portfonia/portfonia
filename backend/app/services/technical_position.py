"""Descriptive price-structure metrics from captured OHLCV (#4).

This is the *factual* half of "technical analysis" and the only half this product
ships. It states where a price sits — distance to its 50/200-day moving average,
position inside the trailing 52-week range, recent realized volatility — as facts.
It NEVER emits a trading signal (support/resistance/breakout/overbought) or an
indicator-to-action mapping: that is the Layer-4 boundary and is forbidden.

Metrics are computed from the `price_snapshots` close series (capture layer). A
metric is `None` when there is not enough history for it (e.g. the 200-day average
needs ~200 captured closes); callers render that as "insufficient history" rather
than fabricating a number. Run `app/scripts/backfill_ohlcv.py` once to seed a year
of closes so the longer-window metrics populate immediately.
"""

from __future__ import annotations

import logging
import statistics
from bisect import bisect_left
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.timezones import today_et
from app.models.holding import Holding
from app.models.portfolio_value_snapshot import PortfolioValueSnapshot
from app.models.price_snapshot import PriceSnapshot
from app.schemas.technical import (
    TechnicalPointOut,
    TechnicalPositionOut,
    TechnicalSeriesOut,
    TechnicalTableOut,
)
from app.services.instrument_symbols import normalize_legacy_ticker

logger = logging.getLogger(__name__)

# Trading-day windows. 52w uses ~252 sessions; we require a near-full year of
# captured closes before quoting a range so the high/low is not a few-week proxy.
_SMA_SHORT = 50
_SMA_LONG = 200
_VOL_WINDOW = 20
_RANGE_MIN_BARS = 200
_TRADING_DAYS_YR = 252
_LOOKBACK_DAYS = 420  # calendar days to scan: comfortably > one trading year


@dataclass
class TechnicalPosition:
    """Where a holding's price sits, as facts. None = insufficient history."""

    ticker: str
    name: str
    last_close: float | None
    bars: int
    pct_vs_sma50: float | None
    pct_vs_sma200: float | None
    range_52w_low: float | None
    range_52w_high: float | None
    pct_in_52w_range: float | None
    vol_20d_annualized: float | None


def _load_closes(
    session: Session, ticker: str, floor: date, ceiling: date
) -> list[tuple[date, float]]:
    """Captured daily closes within inclusive calendar bounds, oldest first."""
    rows = session.execute(
        select(PriceSnapshot.trade_date, PriceSnapshot.close)
        .where(
            PriceSnapshot.ticker == ticker,
            PriceSnapshot.session_node == "close",
            PriceSnapshot.close.is_not(None),
            PriceSnapshot.trade_date >= floor,
            PriceSnapshot.trade_date <= ceiling,
        )
        .order_by(PriceSnapshot.trade_date.asc())
    ).all()
    return [(day, float(close)) for day, close in rows]


def _sma_gap(closes: list[float], window: int) -> float | None:
    if len(closes) < window:
        return None
    sma = statistics.fmean(closes[-window:])
    return closes[-1] / sma - 1 if sma else None


def _vol_annualized(closes: list[float], window: int) -> float | None:
    if len(closes) < window + 1:
        return None
    rets = [closes[i] / closes[i - 1] - 1 for i in range(len(closes) - window, len(closes))]
    if len(rets) < 2:
        return None
    return float(statistics.stdev(rets) * (_TRADING_DAYS_YR**0.5))


def compute_technical_position(
    session: Session, ticker: str, name: str, today: date
) -> TechnicalPosition:
    # issue #204: capture writes closes under the normalized ticker (e.g.
    # "0700.HK" for a holding whose raw ticker is the un-padded "700.HK")
    # — querying with the raw ticker here always found zero bars, leaving
    # §4.4 permanently empty for any normalized ticker regardless of
    # capture/valuation being otherwise correct.
    ticker = normalize_legacy_ticker(ticker)
    closes = [
        close
        for _, close in _load_closes(session, ticker, today - timedelta(days=_LOOKBACK_DAYS), today)
    ]
    last = closes[-1] if closes else None

    lo = hi = pos = None
    if len(closes) >= _RANGE_MIN_BARS and last is not None:
        window = closes[-_TRADING_DAYS_YR:]
        lo, hi = min(window), max(window)
        pos = (last - lo) / (hi - lo) if hi > lo else None

    return TechnicalPosition(
        ticker=ticker,
        name=name,
        last_close=last,
        bars=len(closes),
        pct_vs_sma50=_sma_gap(closes, _SMA_SHORT),
        pct_vs_sma200=_sma_gap(closes, _SMA_LONG),
        range_52w_low=lo,
        range_52w_high=hi,
        pct_in_52w_range=pos,
        vol_20d_annualized=_vol_annualized(closes, _VOL_WINDOW),
    )


def compute_technical_positions(
    session: Session, holdings: list[dict[str, Any]], today: date
) -> list[TechnicalPosition]:
    """Technical position per auto-priced holding that has a ticker.

    Holdings are the serialized portfolio snapshot rows (name + optional ticker);
    funds (no ticker) are skipped — only captured-OHLCV names have a price series.
    """
    out: list[TechnicalPosition] = []
    seen: set[str] = set()
    for h in holdings:
        ticker = h.get("ticker")
        if not ticker or ticker in seen:
            continue
        seen.add(ticker)
        out.append(compute_technical_position(session, ticker, h.get("name", ticker), today))
    return out


@dataclass
class TechnicalPoint:
    date: date
    close: float
    pct_vs_sma50: float | None
    pct_vs_sma200: float | None
    pct_in_52w_range: float | None
    vol_20d_annualized: float | None


def compute_technical_series(
    session: Session, ticker: str, name: str, start: date, end: date
) -> list[TechnicalPoint]:
    ticker = normalize_legacy_ticker(ticker)
    rows = _load_closes(session, ticker, start - timedelta(days=_LOOKBACK_DAYS), end)
    dates = [day for day, _ in rows]
    out: list[TechnicalPoint] = []
    for index, (day, close) in enumerate(rows):
        if day < start:
            continue
        floor = bisect_left(dates, day - timedelta(days=_LOOKBACK_DAYS))
        closes = [value for _, value in rows[floor : index + 1]]
        pos = None
        if len(closes) >= _RANGE_MIN_BARS:
            window = closes[-_TRADING_DAYS_YR:]
            low, high = min(window), max(window)
            pos = (close - low) / (high - low) if high > low else None
        out.append(
            TechnicalPoint(
                day,
                close,
                _sma_gap(closes, _SMA_SHORT),
                _sma_gap(closes, _SMA_LONG),
                pos,
                _vol_annualized(closes, _VOL_WINDOW),
            )
        )
    return out


def technical_holdings(
    session: Session, user_id: UUID, as_of: date | None
) -> tuple[date | None, list[dict[str, str]]]:
    live = session.scalars(
        select(Holding)
        .where(Holding.user_id == user_id)
        .order_by(Holding.position.asc().nulls_last(), Holding.created_at, Holding.id)
    ).all()
    names = {holding.id: holding.name for holding in live}
    rows: list[dict[str, str]] = []
    holdings_date: date | None
    if as_of is None:
        holdings_date = today_et()
        rows = [{"ticker": h.ticker, "name": h.name} for h in live if h.ticker]
    else:
        holdings_date = session.scalar(
            select(func.max(PortfolioValueSnapshot.snapshot_date)).where(
                PortfolioValueSnapshot.user_id == user_id,
                PortfolioValueSnapshot.snapshot_date <= as_of,
            )
        )
        if holdings_date is None:
            return None, []
        snapshots = session.scalars(
            select(PortfolioValueSnapshot)
            .where(
                PortfolioValueSnapshot.user_id == user_id,
                PortfolioValueSnapshot.snapshot_date == holdings_date,
            )
            .order_by(PortfolioValueSnapshot.created_at, PortfolioValueSnapshot.id)
        ).all()
        rows = [
            {
                "ticker": h.ticker,
                "name": names.get(h.holding_id, h.ticker) if h.holding_id else h.ticker,
            }
            for h in snapshots
            if h.ticker
        ]
    unique: dict[str, dict[str, str]] = {}
    for row in rows:
        ticker = normalize_legacy_ticker(row["ticker"])
        unique.setdefault(ticker, {"ticker": ticker, "name": row["name"]})
    return holdings_date, list(unique.values())


def held_tickers(session: Session, user_id: UUID, start: date, end: date) -> set[str]:
    current = session.scalars(select(Holding).where(Holding.user_id == user_id)).all()
    historical = session.scalars(
        select(PortfolioValueSnapshot).where(
            PortfolioValueSnapshot.user_id == user_id,
            PortfolioValueSnapshot.snapshot_date >= start,
            PortfolioValueSnapshot.snapshot_date <= end,
        )
    ).all()
    return {normalize_legacy_ticker(h.ticker) for h in current if h.ticker} | {
        normalize_legacy_ticker(h.ticker) for h in historical if h.ticker
    }


def technical_table(
    session: Session, user_id: UUID, requested_date: date | None
) -> TechnicalTableOut:
    as_of = requested_date or today_et()
    if as_of > today_et():
        raise HTTPException(
            422, detail={"code": "invalid_date", "message": "Date must not be in the future."}
        )
    holdings_date, holdings = technical_holdings(session, user_id, requested_date)
    return TechnicalTableOut(
        as_of=as_of,
        holdings_date=holdings_date,
        positions=[
            TechnicalPositionOut.model_validate(
                compute_technical_position(session, h["ticker"], h["name"], as_of),
                from_attributes=True,
            )
            for h in holdings
        ],
    )


def technical_series(
    session: Session, user_id: UUID, ticker: str, start: date | None, end: date | None
) -> TechnicalSeriesOut:
    end = end or today_et()
    start = start or end - timedelta(days=91)
    if start > end or end > today_et():
        raise HTTPException(
            422,
            detail={
                "code": "invalid_range",
                "message": "Use an ordered range ending on or before today.",
            },
        )
    if (end - start).days > 91:
        raise HTTPException(
            422,
            detail={
                "code": "range_too_long",
                "message": "Request at most 92 inclusive calendar days.",
            },
        )
    ticker = normalize_legacy_ticker(ticker)
    if ticker not in held_tickers(session, user_id, start, end):
        raise HTTPException(
            404, detail={"code": "ticker_not_held", "message": "Ticker was not held in this range."}
        )
    _, current = technical_holdings(session, user_id, None)
    name = next((h["name"] for h in current if h["ticker"] == ticker), ticker)
    points = compute_technical_series(session, ticker, name, start, end)
    return TechnicalSeriesOut(
        ticker=ticker,
        name=name,
        start=start,
        end=end,
        points=[TechnicalPointOut.model_validate(point, from_attributes=True) for point in points],
    )
