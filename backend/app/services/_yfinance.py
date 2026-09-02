"""Shared yfinance access helpers.

Both price and FX fetchers need the same batch-download + last-non-NaN-close
extraction.  Kept in one place so the pandas Series/DataFrame normalisation and
timestamp handling do not drift between callers.

Throttle mitigation strategy (D6):
  1. Tickers are grouped by market (US / HK / A-share / UK / Europe / Japan /
     Korea) before downloading.
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

from app.services.markets import yf_batch_key

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


# Bare tickers that silently collide with an unrelated US-listed security on
# yfinance and need an explicit exchange suffix to resolve to the intended
# instrument (issue #204: bare "PSH" resolved to an unrelated US ETF instead
# of Pershing Square Holdings, which trades on the LSE as PSH.L).
_TICKER_SYMBOL_OVERRIDE: dict[str, str] = {
    "PSH": "PSH.L",
}


def _normalize_ticker(ticker: str) -> str:
    """Canonicalize a ticker to its yfinance-resolvable form.

    Composes the known-collision override table above with HK suffix
    normalization (issue #64) — the two sets are disjoint today, so order
    between them doesn't matter.
    """
    overridden = _TICKER_SYMBOL_OVERRIDE.get(ticker.upper(), ticker)
    return _normalize_hk_ticker(overridden)


# yfinance marks LSE ordinary shares in pence with currency="GBp" (lowercase
# p, distinct from "GBP"). This is an LSE-wide convention, not a per-ticker
# quirk (issue #311, verified on VOD.L/BARC.L/TSCO.L/HSBA.L/ULVR.L plus the
# original PSH.L). Scale by 1/100 whenever the *fetched* currency is GBp —
# never a per-ticker table. Europe (EUR) / Japan (JPY) / Korea (KRW) have no
# equivalent subunit marker.
_GBPENCE = "GBp"


def _fetched_currency(ticker: str) -> str | None:
    """Best-effort yfinance currency for `ticker`. None on any failure."""
    try:
        info = yf.Ticker(ticker).fast_info
        cur = info.get("currency") if info is not None else None
    except Exception:
        logger.exception("yfinance currency lookup failed for %s", ticker)
        return None
    if cur is None:
        return None
    return str(cur)


def _scale_price(value: float, currency: str | None) -> float:
    """Scale a raw yfinance price into the holding's major-unit currency."""
    if currency == _GBPENCE:
        return value * 0.01
    return value


# Type alias used by fetch_last_close.
ClosePoint = tuple[float, datetime]  # (price, exchange-timestamp)

# (trade_date, open, high, low, close, volume) — volume may be None.
OhlcvPoint = tuple[date, float, float, float, float, float | None]

# Maximum tickers per yf.download() call.  Empirically, large mixed batches
# trigger silent partial rejection; keeping this at 8 avoids the threshold.
_MAX_BATCH_SIZE = 8

# Pause between consecutive yf.download() calls to reduce throttle risk.
_INTER_BATCH_DELAY = 0.5  # seconds


def _market_key_for_ticker(ticker: str) -> str:
    """Return yfinance batch-grouping key for a ticker.

    Delegates to `markets.yf_batch_key` so suffix classification cannot
    drift from capture-market resolution (issue #311). Unknown exchange
    suffixes group as 'other' rather than silently joining the US batch.
    """
    return yf_batch_key(ticker)


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
            price, as_of = pts[0]
            out[ticker] = (_scale_price(price, _fetched_currency(ticker)), as_of)
    return out


def fetch_last_close(tickers: list[str]) -> dict[str, ClosePoint]:
    """
    Batch-download tickers and return {ticker: (close, as_of)}.

    Tickers are split by market (US / HK / A-share / UK / Europe / Japan /
    Korea) then each market group is chunked to at most _MAX_BATCH_SIZE per
    yf.download() call.  A short inter-batch delay further reduces Yahoo
    Finance throttling risk.

    `period='5d'` gives each market at least one trading day regardless of
    non-overlapping calendars (A-share 15:00 CST / HK 16:00 HKT / US 16:00 ET).
    `as_of` is the exchange timestamp from the yfinance index, made
    timezone-aware (UTC when naive).  Tickers with no usable data are omitted.
    """
    if not tickers:
        return {}

    tickers = [_normalize_ticker(t) for t in tickers]

    # Group by market, preserving insertion order within each group.
    by_market: dict[str, list[str]] = {}
    for t in tickers:
        by_market.setdefault(_market_key_for_ticker(t), []).append(t)

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


def _ohlcv_rows_for_ticker(
    hist: pd.DataFrame, ticker: str, currency: str | None = None
) -> list[OhlcvPoint]:
    """Extract all available OHLCV bars (oldest→newest) for one ticker."""
    try:
        # MultiIndex columns for multi-ticker downloads; flat for a single ticker.
        sub = hist.xs(ticker, axis=1, level=1) if isinstance(hist.columns, pd.MultiIndex) else hist
        clean = sub.dropna(subset=["Close"])
        if currency is None:
            currency = _fetched_currency(ticker)
        rows: list[OhlcvPoint] = []
        for ts, row in clean.iterrows():
            vol = row.get("Volume")
            rows.append(
                (
                    ts.date(),
                    _scale_price(float(row["Open"]), currency),
                    _scale_price(float(row["High"]), currency),
                    _scale_price(float(row["Low"]), currency),
                    _scale_price(float(row["Close"]), currency),
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
    tickers = [_normalize_ticker(t) for t in tickers]
    period = f"{max(lookback_days, 2)}d"
    by_market: dict[str, list[str]] = {}
    for t in tickers:
        by_market.setdefault(_market_key_for_ticker(t), []).append(t)
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
            rows = _ohlcv_rows_for_ticker(hist, t, currency=_fetched_currency(t))
            if rows:
                out[t] = rows
    return out


def fetch_spot(tickers: list[str]) -> dict[str, float]:
    """Best-effort current/last price per ticker for intraday capture nodes.

    Uses yfinance fast_info; tickers with no usable value are omitted (the
    caller stores null for them). Intraday/extended-hours data is flaky by
    nature — this is best-effort, the close node is the authoritative path.

    Tickers are normalized the same way as fetch_last_close/fetch_ohlcv_range
    (issue #204: this used to query yfinance with the raw, un-normalized
    ticker, so a bare "PSH" would query the wrong instrument here even after
    the close-node path was fixed to query "PSH.L") — the returned dict is
    keyed by the normalized ticker so it lines up with the close-node keys.
    """
    out: dict[str, float] = {}
    for raw in tickers:
        t = _normalize_ticker(raw)
        try:
            info = yf.Ticker(t).fast_info
            last = info.get("lastPrice")
            if last is not None and not pd.isna(last):
                currency = info.get("currency")
                out[t] = _scale_price(float(last), None if currency is None else str(currency))
        except Exception:
            logger.exception("yfinance spot fetch failed for %s", t)
    return out
