"""Twelve Data FX fetch (issue #406 historical gap-fill; issue #426 daily fallback).

yfinance classifies `USDCNH=X` as a limited-history quote type (rejects
`period="max"`, returns ~1 month at most regardless of fetch strategy —
confirmed live, see issue #406's Exploration). Twelve Data's free tier was
confirmed live to carry full multi-year USD/CNH daily history. This module
originally existed solely to back `app/scripts/backfill_usdcnh_history.py`'s
one-off gap fill.

Issue #426 widened its use: `fx_fetcher.fx_catchup()` (the 00:05 ET
next-day recovery pass for a pair still missing its prior day's rate after
a retry) now also calls `fetch_daily_history` here, one pair at a time, as
its fallback source. Every pair's routine daily capture (`update_fx_rates`,
17:15 ET) stays yfinance-only — this is a fallback for the recovery path
only, not a general yfinance replacement (see `Settings.TWELVEDATA_API_KEY`'s
docstring for the current scope).
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

import httpx

logger = logging.getLogger(__name__)

_TIME_SERIES_URL = "https://api.twelvedata.com/time_series"
_PRICE_URL = "https://api.twelvedata.com/price"

# Free tier: 800 requests/day, 8/minute, 5000 data points/request (Twelve
# Data docs) — a multi-year daily-bar request for one symbol is a single
# call, nowhere close to any of those caps.
_MAX_OUTPUTSIZE = 5000


def fetch_daily_history(
    symbol: str, start_date: date, end_date: date, api_key: str
) -> list[tuple[date, Decimal]]:
    """Daily close history for `symbol` (e.g. "USD/CNH") in [start_date, end_date].

    Returns `(rate_date, close)` pairs, oldest first. Raises `httpx.HTTPError`
    on a transport/HTTP failure and `ValueError` on a malformed or
    error-shaped response — this is a one-off operator-run script's fetch,
    not a fail-open production capture path, so a bad response should stop
    the run rather than silently write partial/wrong data.
    """
    with httpx.Client(timeout=30) as client:
        resp = client.get(
            _TIME_SERIES_URL,
            params={
                "symbol": symbol,
                "interval": "1day",
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "outputsize": _MAX_OUTPUTSIZE,
                "format": "JSON",
            },
            headers={"Authorization": f"apikey {api_key}"},
        )
        resp.raise_for_status()
        data = resp.json()

    if not isinstance(data, dict) or data.get("status") != "ok":
        raise ValueError(f"twelvedata time_series error response: {data!r}")
    values = data.get("values")
    if not isinstance(values, list):
        raise ValueError(f"twelvedata time_series missing 'values': {data!r}")

    out: list[tuple[date, Decimal]] = []
    for point in values:
        try:
            rate_date = datetime.strptime(point["datetime"], "%Y-%m-%d").date()
            close = Decimal(str(point["close"]))
        except (KeyError, ValueError, InvalidOperation) as exc:
            raise ValueError(f"twelvedata time_series malformed point: {point!r}") from exc
        # Decimal() parses "NaN"/"Infinity" without raising InvalidOperation
        # (confirmed live) -- a malformed upstream close would otherwise
        # write straight into fx_rates and only fail later, at read time,
        # when a Decimal comparison against NaN raises InvalidOperation
        # deep inside a reader that has no reason to expect it (PR #411
        # review). An FX rate is a real price: reject non-finite and
        # non-positive values at this fetch boundary instead.
        if not close.is_finite() or close <= 0:
            raise ValueError(f"twelvedata time_series non-finite/non-positive close: {point!r}")
        out.append((rate_date, close))
    out.sort(key=lambda pair: pair[0])
    return out


def fetch_live_rate(symbol: str, api_key: str) -> Decimal:
    """Live spot quote for `symbol` (e.g. "USD/CNH") via Twelve Data's
    `/price` endpoint (issue #519).

    `/time_series` (`fetch_daily_history` above) is a daily-bar route —
    it rejects a same-day-range query outright (`start_date == end_date`
    -> 400 "No data is available", confirmed live) independent of publish
    timing, because it is fetching a bar for a day that has not been
    aggregated yet. `/price` is Twelve Data's always-live quote for a
    24/5 FX pair, the direct counterpart to yfinance's `fast_info`
    (`_yfinance.fetch_live_rate`). Used only as this pipeline's fallback
    when yfinance's live quote fails for a pair — `fetch_daily_history`
    stays the only route for `backfill_usdcnh_history.py`'s historical
    multi-year seed, which legitimately wants daily bars.

    Raises `httpx.HTTPError` on a transport/HTTP failure and `ValueError`
    on a malformed or non-finite/non-positive price.
    """
    with httpx.Client(timeout=30) as client:
        resp = client.get(
            _PRICE_URL,
            params={"symbol": symbol},
            headers={"Authorization": f"apikey {api_key}"},
        )
        resp.raise_for_status()
        data = resp.json()

    if not isinstance(data, dict) or "price" not in data:
        raise ValueError(f"twelvedata price response missing 'price': {data!r}")
    try:
        price = Decimal(str(data["price"]))
    except InvalidOperation as exc:
        raise ValueError(f"twelvedata price response malformed price: {data!r}") from exc
    # Same non-finite/non-positive guard as fetch_daily_history (issue #411
    # review) — Decimal() parses "NaN"/"Infinity" without raising.
    if not price.is_finite() or price <= 0:
        raise ValueError(f"twelvedata price response non-finite/non-positive: {data!r}")
    return price
