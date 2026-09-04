"""Finnhub US-market quote fallback (issue #56).

Narrow surface, deliberately: last price + previous close from Finnhub's
`/quote` endpoint only — no news, no other Finnhub endpoints. Triggered by
the caller (`price_capture.py`) only when yfinance failed/missed a ticker
AND that ticker's resolved market is US — verified live (2026-09-04) that
Finnhub's free tier returns `{"error": ...}` for HK/LSE/Tokyo symbols, so
this is a US-only fallback by confirmed capability, not a conservative
guess. This module does not itself check market — the caller decides when
to call it.

`FINNHUB_API_KEY` is shared with the unrelated Daily_Intelligence project's
production use (same free-tier 60 req/min bucket) — a deliberate, accepted
cross-project coupling, not something to route around here.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import httpx

from app.services._yfinance import _safe_scaled_price
from app.services.price_errors import classify_exception

logger = logging.getLogger(__name__)

_QUOTE_URL = "https://finnhub.io/api/v1/quote"


@dataclass(frozen=True)
class FinnhubQuote:
    last: float
    previous_close: float


def _log_telemetry(ticker_count: int, latency_ms: float, error_type: str | None = None) -> None:
    """Same log shape as _yfinance._log_fetch_telemetry — source differs only."""
    if error_type is not None:
        logger.info(
            "price_fetch source=finnhub ticker_count=%d latency_ms=%d error_type=%s",
            ticker_count,
            round(latency_ms),
            error_type,
        )
    else:
        logger.info(
            "price_fetch source=finnhub ticker_count=%d latency_ms=%d",
            ticker_count,
            round(latency_ms),
        )


def fetch_quotes(tickers: list[str], api_key: str) -> dict[str, FinnhubQuote]:
    """Best-effort last/previous-close per ticker. Fail-open: a single
    ticker's failure (HTTP error, network error, `{"error": ...}` body, an
    all-zero unknown-symbol response, or a malformed field) only skips that
    ticker — never raises, never aborts the batch.

    `api_key` is an explicit parameter, not read from Settings inside this
    module (matches fund_nav_fetcher.fetch_nav_history's dependency-
    injection convention, keeps this testable without mocking settings).
    """
    if not tickers:
        return {}

    out: dict[str, FinnhubQuote] = {}
    with httpx.Client() as client:
        for ticker in tickers:
            start = time.monotonic()
            try:
                resp = client.get(
                    _QUOTE_URL, params={"symbol": ticker, "token": api_key}, timeout=10
                )
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                logger.warning("finnhub quote request failed for %s", ticker, exc_info=True)
                _log_telemetry(
                    1, (time.monotonic() - start) * 1000, error_type=classify_exception(exc).value
                )
                continue
            latency_ms = (time.monotonic() - start) * 1000

            try:
                data = resp.json()
            except Exception:
                logger.warning("finnhub quote unparseable response for %s", ticker, exc_info=True)
                _log_telemetry(1, latency_ms, error_type="no_data")
                continue

            if not isinstance(data, dict) or "error" in data:
                logger.warning("finnhub quote error for %s: %r", ticker, data)
                _log_telemetry(1, latency_ms, error_type="no_data")
                continue

            c = data.get("c")
            pc = data.get("pc")
            if c is None or pc is None or c == 0:
                logger.warning("finnhub quote missing/zero data for %s: %r", ticker, data)
                _log_telemetry(1, latency_ms, error_type="no_data")
                continue

            # Same currency/scale-safety gate as yfinance prices (issue
            # #204/#311 GBX precedent) — Finnhub is US-only today so this is
            # a no-op, but that is not license to skip the gate.
            scaled_last = _safe_scaled_price(ticker, float(c), None)
            scaled_pc = _safe_scaled_price(ticker, float(pc), None)
            if scaled_last is None or scaled_pc is None:
                _log_telemetry(1, latency_ms, error_type="no_data")
                continue

            out[ticker] = FinnhubQuote(last=scaled_last, previous_close=scaled_pc)
            _log_telemetry(1, latency_ms)

    return out
