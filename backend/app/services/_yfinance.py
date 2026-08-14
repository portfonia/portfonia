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
import re
import time
from datetime import UTC, date, datetime

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

_HK_TICKER_RE = re.compile(r"^0*(\d+)\.HK$", re.IGNORECASE)


def _normalize_hk_ticker(ticker: str) -> str:
    """Canonicalize an HK ticker to yfinance's 4-digit form (issue #64).

    Strips leading zeros then left-pads codes below 10000 back to 4 digits.
    Genuine 5-digit codes (>=10000) are left as-is. Non-HK tickers pass through.
    """
    m = _HK_TICKER_RE.match(ticker)
    if not m:
        return ticker
    num = int(m.group(1))
    digits = f"{num:04d}" if num < 10000 else str(num)
    return f"{digits}.HK"


# Type alias used by fetch_last_close.
ClosePoint = tuple[float, datetime]  # (price, exchange-timestamp)

# (trade_date, open, high, low, close, volume) — volume may be None.
OhlcvPoint = tuple[date, float, float, float, float, float | None]

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

    tickers = [_normalize_hk_ticker(t) for t in tickers]

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


def _ohlcv_rows_for_ticker(hist: pd.DataFrame, ticker: str) -> list[OhlcvPoint]:
    """Extract all available OHLCV bars (oldest→newest) for one ticker."""
    try:
        # MultiIndex columns for multi-ticker downloads; flat for a single ticker.
        sub = hist.xs(ticker, axis=1, level=1) if isinstance(hist.columns, pd.MultiIndex) else hist
        clean = sub.dropna(subset=["Close"])
        rows: list[OhlcvPoint] = []
        for ts, row in clean.iterrows():
            vol = row.get("Volume")
            rows.append(
                (
                    ts.date(),
                    float(row["Open"]),
                    float(row["High"]),
                    float(row["Low"]),
                    float(row["Close"]),
                    None if vol is None or pd.isna(vol) else float(vol),
                )
            )
        return rows
    except Exception:
        logger.exception("ohlcv extraction failed for %s", ticker)
        return []


def fetch_ohlcv_range(tickers: list[str], lookback_days: int = 7) -> dict[str, list[OhlcvPoint]]:
    """Daily OHLCV bars over the last `lookback_days` per ticker (oldest→newest).

    Returning a range (not just the latest bar) is what lets the `close` capture
    node backfill missed trading days: each bar is upserted by its own
    trade_date. ~7 calendar days covers ~5 trading days of catch-up.
    """
    if not tickers:
        return {}
    tickers = [_normalize_hk_ticker(t) for t in tickers]
    period = f"{max(lookback_days, 2)}d"
    by_market: dict[str, list[str]] = {"us": [], "hk": [], "cn": []}
    for t in tickers:
        by_market[_classify_market(t)].append(t)
    batches: list[list[str]] = []
    for market_tickers in by_market.values():
        if market_tickers:
            batches.extend(_chunk(market_tickers, _MAX_BATCH_SIZE))

    out: dict[str, list[OhlcvPoint]] = {}
    for i, batch in enumerate(batches):
        if i > 0:
            time.sleep(_INTER_BATCH_DELAY)
        try:
            hist = yf.download(
                tickers=" ".join(batch), period=period, auto_adjust=True, progress=False
            )
        except Exception:
            logger.exception("yfinance OHLCV download failed for %s", batch)
            continue
        if hist.empty:
            continue
        for t in batch:
            rows = _ohlcv_rows_for_ticker(hist, t)
            if rows:
                out[t] = rows
    return out


def fetch_spot(tickers: list[str]) -> dict[str, float]:
    """Best-effort current/last price per ticker for intraday capture nodes.

    Uses yfinance fast_info; tickers with no usable value are omitted (the
    caller stores null for them). Intraday/extended-hours data is flaky by
    nature — this is best-effort, the close node is the authoritative path.
    """
    out: dict[str, float] = {}
    for t in tickers:
        try:
            last = yf.Ticker(t).fast_info.get("lastPrice")
            if last is not None and not pd.isna(last):
                out[t] = float(last)
        except Exception:
            logger.exception("yfinance spot fetch failed for %s", t)
    return out
