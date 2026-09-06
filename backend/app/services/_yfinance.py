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
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime

import pandas as pd
import yfinance as yf

from app.services.instrument_symbols import _TICKER_SYMBOL_OVERRIDE as _TICKER_SYMBOL_OVERRIDE
from app.services.instrument_symbols import normalize_legacy_ticker
from app.services.markets import yf_batch_key
from app.services.price_errors import classify_exception

logger = logging.getLogger(__name__)

_YF_LOGGER = logging.getLogger("yfinance")


@contextmanager
def _quiet_yfinance_logs() -> Iterator[None]:
    """Demote the `yfinance` logger to CRITICAL for the duration of a call.

    yfinance emits a known false "possibly delisted; no price data found"
    ERROR on transient misses (a throttle or a genuinely quiet trading day),
    which is noise at our log level, not a signal — the caller here already
    handles the miss (empty result / omitted ticker). Restores the prior
    level on exit, including on exception (issue #56).
    """
    previous = _YF_LOGGER.level
    _YF_LOGGER.setLevel(logging.CRITICAL)
    try:
        yield
    finally:
        _YF_LOGGER.setLevel(previous)


def _log_fetch_telemetry(
    source: str, ticker_count: int, latency_ms: float, error_type: str | None = None
) -> None:
    """Structured provider telemetry (issue #56) — log shape only, no table/alerting."""
    if error_type is not None:
        logger.info(
            "price_fetch source=%s ticker_count=%d latency_ms=%d error_type=%s",
            source,
            ticker_count,
            round(latency_ms),
            error_type,
        )
    else:
        logger.info(
            "price_fetch source=%s ticker_count=%d latency_ms=%d",
            source,
            ticker_count,
            round(latency_ms),
        )


# Canonicalization rules moved to instrument_symbols (issue #57, stage 57-1).
# _TICKER_SYMBOL_OVERRIDE stays re-exported (not re-implemented) — this
# module's own internal call sites use `normalize_legacy_ticker` directly
# (stage 57-2); `_normalize_ticker` remains only as a forwarding shim for
# the still-unmigrated intelligence/report consumers (57-3) and their
# tests. Do not add new logic here — extend instrument_symbols instead.
def _normalize_ticker(ticker: str) -> str:
    """Forwarding shim to `instrument_symbols.normalize_legacy_ticker` (issue #57)."""
    return normalize_legacy_ticker(ticker)


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
        with _quiet_yfinance_logs():
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


def _is_lse_ticker(ticker: str) -> bool:
    """True when this symbol is batched as UK/LSE (suffix .L after normalize)."""
    return _market_key_for_ticker(ticker) == "uk"


def _safe_scaled_price(ticker: str, value: float, currency: str | None) -> float | None:
    """Scale GBp to pounds, or omit an LSE bar whose currency lookup failed.

    Issue #312 B1 / #311 req 6: `_scale_price(value, None)` is identity, so a
    UK close that got OHLCV but lost `fast_info.currency` would store pence as
    pounds (the 100x class #204/#311 exist to kill). Fail closed for LSE:
    unknown currency → None (caller omits the ticker). EUR/JPY/KRW (and US)
    stay unscaled when currency is present or missing — they have no subunit
    marker.

    Checks falsy, not just `currency is None` (issue #313 item 2):
    `_fetched_currency`'s return type (`str | None`) does not rule out
    yfinance itself handing back an empty string, and that value must be
    treated as unknown here too — an empty string is not `_GBPENCE` and
    would otherwise fall through to the final unscaled `return value`.
    """
    if currency == _GBPENCE:
        return value * 0.01
    if not currency and _is_lse_ticker(ticker):
        logger.warning(
            "omitting %s: LSE bar with unknown currency; refusing unscaled pence",
            ticker,
        )
        return None
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
    start = time.monotonic()
    try:
        with _quiet_yfinance_logs():
            hist = yf.download(
                tickers=ticker_str,
                period="5d",
                auto_adjust=True,
                progress=False,
            )
    except Exception as exc:
        logger.exception("yfinance download failed for %s", ticker_str)
        _log_fetch_telemetry(
            "yfinance",
            len(tickers),
            (time.monotonic() - start) * 1000,
            error_type=classify_exception(exc).value,
        )
        return pd.DataFrame()
    latency_ms = (time.monotonic() - start) * 1000
    if hist.empty:
        _log_fetch_telemetry("yfinance", len(tickers), latency_ms, error_type="no_data")
        return pd.DataFrame()
    _log_fetch_telemetry("yfinance", len(tickers), latency_ms)
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
            scaled = _safe_scaled_price(ticker, price, _fetched_currency(ticker))
            if scaled is None:
                continue
            out[ticker] = (scaled, as_of)
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

    tickers = [normalize_legacy_ticker(t) for t in tickers]

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
        # Do not make a second fast_info round-trip here: the caller already
        # looked up currency (or failed). Unknown LSE currency → omit bars.
        # Falsy, not just None (issue #313 item 2) — matches _safe_scaled_price.
        if not currency and _is_lse_ticker(ticker):
            logger.warning(
                "omitting %s: LSE OHLCV with unknown currency; refusing unscaled pence",
                ticker,
            )
            return []
        rows: list[OhlcvPoint] = []
        for ts, row in clean.iterrows():
            vol = row.get("Volume")
            close = _safe_scaled_price(ticker, float(row["Close"]), currency)
            if close is None:
                return []
            open_ = _safe_scaled_price(ticker, float(row["Open"]), currency)
            high = _safe_scaled_price(ticker, float(row["High"]), currency)
            low = _safe_scaled_price(ticker, float(row["Low"]), currency)
            if open_ is None or high is None or low is None:
                return []
            rows.append(
                (
                    ts.date(),
                    open_,
                    high,
                    low,
                    close,
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
    tickers = [normalize_legacy_ticker(t) for t in tickers]
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
        start = time.monotonic()
        try:
            with _quiet_yfinance_logs():
                hist = yf.download(
                    tickers=" ".join(batch), period=period, auto_adjust=True, progress=False
                )
        except Exception as exc:
            logger.exception("yfinance OHLCV download failed for %s", batch)
            _log_fetch_telemetry(
                "yfinance",
                len(batch),
                (time.monotonic() - start) * 1000,
                error_type=classify_exception(exc).value,
            )
            continue
        latency_ms = (time.monotonic() - start) * 1000
        if hist.empty:
            _log_fetch_telemetry("yfinance", len(batch), latency_ms, error_type="no_data")
            continue
        _log_fetch_telemetry("yfinance", len(batch), latency_ms)
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
        t = normalize_legacy_ticker(raw)
        start = time.monotonic()
        try:
            with _quiet_yfinance_logs():
                info = yf.Ticker(t).fast_info
            last = info.get("lastPrice")
            latency_ms = (time.monotonic() - start) * 1000
            if last is not None and not pd.isna(last):
                raw_cur = info.get("currency")
                currency = None if raw_cur is None else str(raw_cur)
                scaled = _safe_scaled_price(t, float(last), currency)
                if scaled is None:
                    _log_fetch_telemetry("yfinance", 1, latency_ms, error_type="no_data")
                    continue
                out[t] = scaled
                _log_fetch_telemetry("yfinance", 1, latency_ms)
            else:
                _log_fetch_telemetry("yfinance", 1, latency_ms, error_type="no_data")
        except Exception as exc:
            logger.exception("yfinance spot fetch failed for %s", t)
            _log_fetch_telemetry(
                "yfinance",
                1,
                (time.monotonic() - start) * 1000,
                error_type=classify_exception(exc).value,
            )
    return out
