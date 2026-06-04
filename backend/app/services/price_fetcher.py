"""Fetch latest close prices from yfinance and persist to holdings."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

import pandas as pd
import yfinance as yf
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.holding import Holding

logger = logging.getLogger(__name__)


@dataclass
class PricePoint:
    price: float
    # Timestamp from the yfinance data index — the trading-day close time
    # as reported by the exchange, timezone-aware (UTC for US, HKT for HK, etc.).
    as_of: datetime


@dataclass
class PriceFetchResult:
    updated: int = 0
    failed: list[str] = field(default_factory=list)
    skipped: int = 0  # manual-mode or no-ticker holdings


def _batch_fetch(tickers: list[str]) -> dict[str, PricePoint]:
    """
    Download the most recent close price for each ticker via yfinance.

    Uses period='5d' so non-overlapping market calendars (US / HK / A-share)
    each have at least one trading day in the window. Takes the last non-NaN
    close value and its index timestamp per ticker.

    Returns {ticker: PricePoint}. Tickers with no data are omitted.
    """
    if not tickers:
        return {}

    ticker_str = " ".join(tickers)
    try:
        hist = yf.download(
            tickers=ticker_str,
            period="5d",
            auto_adjust=True,
            progress=False,
        )
    except Exception:
        logger.exception("yfinance download failed for %s", ticker_str)
        return {}

    if hist.empty:
        return {}

    close = hist["Close"]

    # Single-ticker download returns a Series; normalise to DataFrame.
    if isinstance(close, pd.Series):
        close = close.to_frame(name=tickers[0])

    points: dict[str, PricePoint] = {}
    for ticker in tickers:
        if ticker not in close.columns:
            continue
        series = close[ticker].dropna()
        if series.empty:
            continue
        ts = series.index[-1]
        # yfinance index is a DatetimeIndex; ensure timezone-aware UTC.
        if hasattr(ts, "tzinfo") and ts.tzinfo is not None:
            as_of = ts.to_pydatetime()
        else:
            as_of = ts.to_pydatetime().replace(tzinfo=UTC)
        points[ticker] = PricePoint(price=float(series.iloc[-1]), as_of=as_of)

    return points


def update_holding_prices(session: Session) -> PriceFetchResult:
    """
    Load all auto-mode holdings with a ticker, batch-fetch prices, and
    write market_price / price_as_of / price_fetched_at back to the DB.

    - price_as_of    : timestamp of the price data point from the exchange
    - price_fetched_at: when this process called yfinance

    Retries once on total failure before giving up.
    """
    result = PriceFetchResult()

    rows: list[Holding] = list(
        session.execute(
            select(Holding).where(
                Holding.pricing_mode == "auto",
                Holding.ticker.isnot(None),
            )
        ).scalars()
    )

    if not rows:
        return result

    unique_tickers = list({r.ticker for r in rows if r.ticker})
    result.skipped = len([r for r in rows if r.pricing_mode != "auto" or not r.ticker])

    points = _batch_fetch(unique_tickers)

    if not points:
        logger.warning("yfinance returned no data, retrying once")
        time.sleep(5)
        points = _batch_fetch(unique_tickers)

    if not points:
        result.failed = unique_tickers
        logger.error("yfinance returned no data after retry for: %s", unique_tickers)
        return result

    fetched_at = datetime.now(tz=UTC)

    for row in rows:
        ticker = row.ticker
        assert ticker is not None  # filtered in query above
        if ticker not in points:
            result.failed.append(ticker)
            logger.warning("no price data for ticker %s", ticker)
            continue
        pt = points[ticker]
        row.market_price = Decimal(str(pt.price))
        row.price_as_of = pt.as_of
        row.price_fetched_at = fetched_at
        result.updated += 1

    result.failed = list(set(result.failed))
    session.flush()
    return result
