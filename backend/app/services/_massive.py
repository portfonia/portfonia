"""Massive.com (formerly Polygon.io) close-node OHLCV fallback (issue #56).

Free-tier shape is the opposite of Finnhub's: same-day data withheld
entirely (`403 NOT_AUTHORIZED`, verified live 2026-09-04), T-1-onward EOD
aggregates freely available. That matches the `close` capture node
specifically — it runs post-close and wants the finalized prior-session
OHLCV, never same-day intraday. Not usable for spot/intraday nodes or any
non-US market (official pricing page: US-stocks-only at every tier).

Uses the new `api.massive.com` domain, not the legacy `api.polygon.io` —
both work today but the old domain has no committed sunset date.

`_finnhub.py` and this module stay separate: different trigger conditions,
different response shapes, different capture nodes. Do not merge them into
one generic client (see the design doc's "no fetcher interface" exclusion).
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime

import httpx

from app.services._yfinance import OhlcvPoint, _safe_scaled_price
from app.services.price_errors import classify_exception

logger = logging.getLogger(__name__)

_PREV_CLOSE_URL = "https://api.massive.com/v2/aggs/ticker/{ticker}/prev"

# Free tier is rate-limited to 5 req/min (official pricing page) — an
# unpaced sequential loop over a large yfinance-miss batch can trip 429 and
# fail-open-skip tickers that would otherwise have succeeded (PR #342
# review). 12s is 60s/5, the minimum spacing that never exceeds the limit;
# only paid between requests (never before the first), so the common case
# of 0-1 missing tickers sees no added latency.
_INTER_REQUEST_DELAY_SECONDS = 12


def _log_telemetry(ticker_count: int, latency_ms: float, error_type: str | None = None) -> None:
    """Same log shape as _yfinance._log_fetch_telemetry — source differs only."""
    if error_type is not None:
        logger.info(
            "price_fetch source=massive ticker_count=%d latency_ms=%d error_type=%s",
            ticker_count,
            round(latency_ms),
            error_type,
        )
    else:
        logger.info(
            "price_fetch source=massive ticker_count=%d latency_ms=%d",
            ticker_count,
            round(latency_ms),
        )


def fetch_prev_close_ohlcv(tickers: list[str], api_key: str) -> dict[str, OhlcvPoint]:
    """Previous trading day's OHLCV bar per ticker, one GET each (free tier
    has no batch form). Fail-open: a single ticker's failure (network error,
    the current-day 403, empty `results`, or a malformed field) only skips
    that ticker — never raises, never aborts the batch.

    `api_key` is an explicit parameter, not read from Settings inside this
    module (matches fund_nav_fetcher.fetch_nav_history's dependency-
    injection convention, keeps this testable without mocking settings).
    """
    if not tickers:
        return {}

    out: dict[str, OhlcvPoint] = {}
    with httpx.Client() as client:
        for i, ticker in enumerate(tickers):
            if i > 0:
                time.sleep(_INTER_REQUEST_DELAY_SECONDS)
            start = time.monotonic()
            try:
                resp = client.get(
                    _PREV_CLOSE_URL.format(ticker=ticker),
                    params={"adjusted": "true", "apiKey": api_key},
                    timeout=10,
                )
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                logger.warning("massive prev-close request failed for %s", ticker, exc_info=True)
                _log_telemetry(
                    1, (time.monotonic() - start) * 1000, error_type=classify_exception(exc).value
                )
                continue
            latency_ms = (time.monotonic() - start) * 1000

            try:
                data = resp.json()
            except Exception:
                logger.warning(
                    "massive prev-close unparseable response for %s", ticker, exc_info=True
                )
                _log_telemetry(1, latency_ms, error_type="no_data")
                continue

            results = data.get("results") if isinstance(data, dict) else None
            if not results:
                logger.warning("massive prev-close no results for %s: %r", ticker, data)
                _log_telemetry(1, latency_ms, error_type="no_data")
                continue

            bar = results[0]
            try:
                o = float(bar["o"])
                h = float(bar["h"])
                low = float(bar["l"])
                c = float(bar["c"])
                trade_date = datetime.fromtimestamp(int(bar["t"]) / 1000, tz=UTC).date()
                raw_vol = bar.get("v")
                vol = None if raw_vol is None else float(raw_vol)
            except (KeyError, TypeError, ValueError):
                logger.warning(
                    "massive prev-close malformed bar for %s: %r", ticker, bar, exc_info=True
                )
                _log_telemetry(1, latency_ms, error_type="no_data")
                continue

            # Same currency/scale-safety gate as yfinance prices (issue
            # #204/#311 GBX precedent) — Massive is US-only today so this is
            # a no-op, but that is not license to skip the gate.
            scaled_o = _safe_scaled_price(ticker, o, None)
            scaled_h = _safe_scaled_price(ticker, h, None)
            scaled_low = _safe_scaled_price(ticker, low, None)
            scaled_c = _safe_scaled_price(ticker, c, None)
            if None in (scaled_o, scaled_h, scaled_low, scaled_c):
                _log_telemetry(1, latency_ms, error_type="no_data")
                continue

            assert scaled_o is not None
            assert scaled_h is not None
            assert scaled_low is not None
            assert scaled_c is not None
            out[ticker] = (trade_date, scaled_o, scaled_h, scaled_low, scaled_c, vol)
            _log_telemetry(1, latency_ms)

    return out
