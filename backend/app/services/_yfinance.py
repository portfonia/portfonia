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

# Type alias used by both fetch_last_close and fetch_last_two_closes.
ClosePoint = tuple[float, datetime]  # (price, exchange-timestamp)

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


def _extract_close_points(series: pd.Series, n: int) -> list[ClosePoint]:
    """Return up to the n most-recent (price, as_of) pairs from a Close series.

    NaN rows are dropped first so calendar gaps do not count as data points.
    Returns fewer than n items when fewer trading days are available.
    """
    clean = series.dropna()
    if clean.empty:
        return []
    points: list[ClosePoint] = []
    for ts, val in clean.iloc[-n:].items():
        as_of = (
            ts.to_pydatetime() if ts.tzinfo is not None else ts.to_pydatetime().replace(tzinfo=UTC)
        )
        points.append((float(val), as_of))
    return points


def _raw_download(tickers: list[str]) -> pd.DataFrame:
    """Shared yf.download call; returns the Close DataFrame or empty DataFrame on error."""
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
        return pd.DataFrame()
    if hist.empty:
        return pd.DataFrame()
    close = hist["Close"]
    if isinstance(close, pd.Series):
        close = close.to_frame(name=tickers[0])
    return close


def _download_batch(tickers: list[str]) -> dict[str, ClosePoint]:
    """Download one pre-sized batch and return {ticker: (close, as_of)}.

    Returns {} on network failure or when yfinance returns no usable data.
    """
    if not tickers:
        return {}
    close = _raw_download(tickers)
    if close.empty:
        return {}
    out: dict[str, ClosePoint] = {}
    for ticker in tickers:
        if ticker not in close.columns:
            continue
        pts = _extract_close_points(close[ticker], 1)
        if pts:
            out[ticker] = pts[0]
    return out


def _download_batch_two(
    tickers: list[str],
) -> dict[str, tuple[ClosePoint, ClosePoint | None]]:
    """Like _download_batch but returns (current_close, prev_close_or_None) per ticker."""
    if not tickers:
        return {}
    close = _raw_download(tickers)
    if close.empty:
        return {}
    out: dict[str, tuple[ClosePoint, ClosePoint | None]] = {}
    for ticker in tickers:
        if ticker not in close.columns:
            continue
        pts = _extract_close_points(close[ticker], 2)
        if not pts:
            continue
        current = pts[-1]
        prev: ClosePoint | None = pts[-2] if len(pts) >= 2 else None
        out[ticker] = (current, prev)
    return out


def fetch_last_close(tickers: list[str]) -> dict[str, ClosePoint]:
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

    out: dict[str, ClosePoint] = {}
    for i, batch in enumerate(batches):
        if i > 0:
            time.sleep(_INTER_BATCH_DELAY)
        out.update(_download_batch(batch))

    return out


def fetch_last_two_closes(
    tickers: list[str],
) -> dict[str, tuple[ClosePoint, ClosePoint | None]]:
    """
    Batch-download tickers and return {ticker: (current_close, prev_close_or_None)}.

    Applies the same market-split + batch-size + inter-batch-delay strategy as
    fetch_last_close.  `prev_close` is None when fewer than two trading days of
    data are available (e.g. a ticker listed this week).

    Used by price anomaly detection (E3) to compute daily percentage change.
    """
    if not tickers:
        return {}

    by_market: dict[str, list[str]] = {"us": [], "hk": [], "cn": []}
    for t in tickers:
        by_market[_classify_market(t)].append(t)

    batches: list[list[str]] = []
    for market_tickers in by_market.values():
        if market_tickers:
            batches.extend(_chunk(market_tickers, _MAX_BATCH_SIZE))

    out: dict[str, tuple[ClosePoint, ClosePoint | None]] = {}
    for i, batch in enumerate(batches):
        if i > 0:
            time.sleep(_INTER_BATCH_DELAY)
        out.update(_download_batch_two(batch))

    return out
