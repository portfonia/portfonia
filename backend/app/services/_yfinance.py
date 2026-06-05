"""Shared yfinance access helpers.

Both price and FX fetchers need the same batch-download + last-non-NaN-close
extraction.  Kept in one place so the pandas Series/DataFrame normalisation and
timestamp handling do not drift between callers.

Throttle mitigation strategy (D6):
  1. Tickers are grouped by market (US / HK / A-share) before downloading.
  2. Each market group is further chunked to at most _MAX_BATCH_SIZE tickers per
     yf.download() call.  Yahoo Finance silently drops tickers from large batches;
     smaller homogeneous batches avoid the silent rejection.
  3. A short pause (_INTER_BATCH_DELAY) is inserted between consecutive calls.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

# Maximum tickers per yf.download() call.  Empirically, large mixed batches
# trigger silent partial rejection; keeping this at 8 avoids the threshold.
_MAX_BATCH_SIZE = 8

# Pause between consecutive yf.download() calls to reduce throttle risk.
_INTER_BATCH_DELAY = 0.5  # seconds


def _classify_market(ticker: str) -> str:
    """Return market key for a ticker: 'hk', 'cn', or 'us'."""
    upper = ticker.upper()
    if upper.endswith(".HK"):
        return "hk"
    if upper.endswith(".SS") or upper.endswith(".SZ"):
        return "cn"
    return "us"


def _chunk(items: list[str], size: int) -> list[list[str]]:
    """Split a list into sub-lists of at most `size` items."""
    return [items[i : i + size] for i in range(0, len(items), size)]


def _download_batch(tickers: list[str]) -> dict[str, tuple[float, datetime]]:
    """
    Download one pre-sized batch and return {ticker: (close, as_of)}.

    Returns {} on network failure or when yfinance returns no usable data.
    Single-ticker downloads return a Series; this is normalised to a DataFrame
    before column iteration so the extraction logic is uniform.
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

    out: dict[str, tuple[float, datetime]] = {}
    for ticker in tickers:
        if ticker not in close.columns:
            continue
        series = close[ticker].dropna()
        if series.empty:
            continue
        ts = series.index[-1]
        as_of = (
            ts.to_pydatetime() if ts.tzinfo is not None else ts.to_pydatetime().replace(tzinfo=UTC)
        )
        out[ticker] = (float(series.iloc[-1]), as_of)

    return out


def fetch_last_close(tickers: list[str]) -> dict[str, tuple[float, datetime]]:
    """
    Batch-download tickers and return {ticker: (close, as_of)}.

    Tickers are split by market (US / HK / A-share) then each market group is
    chunked to at most _MAX_BATCH_SIZE per yf.download() call.  A short
    inter-batch delay further reduces Yahoo Finance throttling risk.

    `period='5d'` gives each market at least one trading day regardless of
    non-overlapping calendars (A-share 15:00 CST / HK 16:00 HKT / US 16:00 ET).
    `as_of` is the exchange timestamp from the yfinance index, made
    timezone-aware (UTC when naive).  Tickers with no usable data are omitted.
    """
    if not tickers:
        return {}

    # Group by market, preserving insertion order within each group.
    by_market: dict[str, list[str]] = {"us": [], "hk": [], "cn": []}
    for t in tickers:
        by_market[_classify_market(t)].append(t)

    # Build ordered list of sub-batches, each <= _MAX_BATCH_SIZE tickers.
    batches: list[list[str]] = []
    for market_tickers in by_market.values():
        if market_tickers:
            batches.extend(_chunk(market_tickers, _MAX_BATCH_SIZE))

    out: dict[str, tuple[float, datetime]] = {}
    for i, batch in enumerate(batches):
        if i > 0:
            time.sleep(_INTER_BATCH_DELAY)
        out.update(_download_batch(batch))

    return out
