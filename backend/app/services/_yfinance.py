"""Shared yfinance access helpers.

Both price and FX fetchers need the same batch-download + last-non-NaN-close
extraction. Kept in one place so the pandas Series/DataFrame normalisation and
timestamp handling do not drift between callers.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)


def fetch_last_close(tickers: list[str]) -> dict[str, tuple[float, datetime]]:
    """
    Batch-download tickers and return {ticker: (close, as_of)}.

    `period='5d'` gives non-overlapping market calendars (US / HK / A-share)
    at least one trading day each. `as_of` is the exchange timestamp from the
    yfinance index, made timezone-aware (UTC when naive). Tickers with no
    usable data are omitted.
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
