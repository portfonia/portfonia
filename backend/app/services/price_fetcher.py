"""Fetch latest close prices from yfinance and persist to holdings.market_price."""

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
class PriceFetchResult:
    updated: int = 0
    failed: list[str] = field(default_factory=list)
    skipped: int = 0  # manual-mode or no-ticker holdings


def _batch_fetch(tickers: list[str]) -> dict[str, float]:
    """
    Download the most recent close price for each ticker via yfinance.

    Uses period='5d' so that non-overlapping market calendars (US/HK/A-share)
    each have at least one trading day in the window. Takes the last non-NaN
    close value per ticker.

    Returns a dict of {ticker: price}. Tickers with no data are omitted.
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

    # When a single ticker is downloaded, yfinance returns a Series not a DataFrame.
    # Normalise to DataFrame so the column-iteration below works uniformly.
    if isinstance(close, pd.Series):
        close = close.to_frame(name=tickers[0])

    prices: dict[str, float] = {}
    for ticker in tickers:
        if ticker not in close.columns:
            continue
        series = close[ticker].dropna()
        if series.empty:
            continue
        prices[ticker] = float(series.iloc[-1])

    return prices


def update_holding_prices(session: Session) -> PriceFetchResult:
    """
    Load all auto-mode holdings with a ticker, batch-fetch prices, and
    write market_price + price_fetched_at back to the DB.

    Retries the full yfinance download once on total failure before giving up.
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

    # Deduplicate: one yfinance call per unique ticker.
    unique_tickers = list({r.ticker for r in rows if r.ticker})
    result.skipped = len([r for r in rows if r.pricing_mode != "auto" or not r.ticker])

    prices = _batch_fetch(unique_tickers)

    # Single retry on empty result (transient network issue).
    if not prices:
        logger.warning("yfinance returned no data, retrying once")
        time.sleep(5)
        prices = _batch_fetch(unique_tickers)

    if not prices:
        result.failed = unique_tickers
        logger.error("yfinance returned no data after retry for: %s", unique_tickers)
        return result

    fetched_at = datetime.now(tz=UTC)

    for row in rows:
        ticker = row.ticker
        assert ticker is not None  # filtered in query above
        if ticker not in prices:
            result.failed.append(ticker)
            logger.warning("no price data for ticker %s", ticker)
            continue
        row.market_price = Decimal(str(prices[ticker]))
        row.price_fetched_at = fetched_at
        result.updated += 1

    failed_tickers = set(result.failed)
    result.failed = list(failed_tickers)

    session.flush()
    return result
