"""Descriptive price-structure metrics from captured OHLCV (#4).

This is the *factual* half of "technical analysis" and the only half this product
ships. It states where a price sits — distance to its 50/200-day moving average,
position inside the trailing 52-week range, recent realized volatility — as facts.
It NEVER emits a trading signal (support/resistance/breakout/overbought) or an
indicator-to-action mapping: that is the Layer-4 boundary and is forbidden.

Metrics are computed from the `price_snapshots` close series (capture layer). A
metric is `None` when there is not enough history for it (e.g. the 200-day average
needs ~200 captured closes); callers render that as "insufficient history" rather
than fabricating a number. Run `app/scripts/backfill_ohlcv.py` once to seed a year
of closes so the longer-window metrics populate immediately.
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.price_snapshot import PriceSnapshot
from app.services.instrument_symbols import normalize_legacy_ticker

logger = logging.getLogger(__name__)

# Trading-day windows. 52w uses ~252 sessions; we require a near-full year of
# captured closes before quoting a range so the high/low is not a few-week proxy.
_SMA_SHORT = 50
_SMA_LONG = 200
_VOL_WINDOW = 20
_RANGE_MIN_BARS = 200
_TRADING_DAYS_YR = 252
_LOOKBACK_DAYS = 420  # calendar days to scan: comfortably > one trading year


@dataclass
class TechnicalPosition:
    """Where a holding's price sits, as facts. None = insufficient history."""

    ticker: str
    name: str
    last_close: float | None
    bars: int
    pct_vs_sma50: float | None
    pct_vs_sma200: float | None
    range_52w_low: float | None
    range_52w_high: float | None
    pct_in_52w_range: float | None
    vol_20d_annualized: float | None


def _load_closes(session: Session, ticker: str, floor: date) -> list[float]:
    """Daily closes on-or-after `floor`, oldest first (capture layer, close node)."""
    rows = (
        session.execute(
            select(PriceSnapshot.close)
            .where(
                PriceSnapshot.ticker == ticker,
                PriceSnapshot.session_node == "close",
                PriceSnapshot.close.is_not(None),
                PriceSnapshot.trade_date >= floor,
            )
            .order_by(PriceSnapshot.trade_date.asc())
        )
        .scalars()
        .all()
    )
    return [float(c) for c in rows if c is not None]


def _sma_gap(closes: list[float], window: int) -> float | None:
    if len(closes) < window:
        return None
    sma = statistics.fmean(closes[-window:])
    return closes[-1] / sma - 1 if sma else None


def _vol_annualized(closes: list[float], window: int) -> float | None:
    if len(closes) < window + 1:
        return None
    rets = [closes[i] / closes[i - 1] - 1 for i in range(len(closes) - window, len(closes))]
    if len(rets) < 2:
        return None
    return float(statistics.stdev(rets) * (_TRADING_DAYS_YR**0.5))


def compute_technical_position(
    session: Session, ticker: str, name: str, today: date
) -> TechnicalPosition:
    # issue #204: capture writes closes under the normalized ticker (e.g.
    # "PSH.L" for a holding whose raw ticker is "PSH") — querying with the
    # raw ticker here always found zero bars, leaving §4.4 permanently
    # empty for any normalized ticker regardless of capture/valuation being
    # otherwise correct.
    ticker = normalize_legacy_ticker(ticker)
    closes = _load_closes(session, ticker, today - timedelta(days=_LOOKBACK_DAYS))
    last = closes[-1] if closes else None

    lo = hi = pos = None
    if len(closes) >= _RANGE_MIN_BARS and last is not None:
        window = closes[-_TRADING_DAYS_YR:]
        lo, hi = min(window), max(window)
        pos = (last - lo) / (hi - lo) if hi > lo else None

    return TechnicalPosition(
        ticker=ticker,
        name=name,
        last_close=last,
        bars=len(closes),
        pct_vs_sma50=_sma_gap(closes, _SMA_SHORT),
        pct_vs_sma200=_sma_gap(closes, _SMA_LONG),
        range_52w_low=lo,
        range_52w_high=hi,
        pct_in_52w_range=pos,
        vol_20d_annualized=_vol_annualized(closes, _VOL_WINDOW),
    )


def compute_technical_positions(
    session: Session, holdings: list[dict[str, Any]], today: date
) -> list[TechnicalPosition]:
    """Technical position per auto-priced holding that has a ticker.

    Holdings are the serialized portfolio snapshot rows (name + optional ticker);
    funds (no ticker) are skipped — only captured-OHLCV names have a price series.
    """
    out: list[TechnicalPosition] = []
    seen: set[str] = set()
    for h in holdings:
        ticker = h.get("ticker")
        if not ticker or ticker in seen:
            continue
        seen.add(ticker)
        out.append(compute_technical_position(session, ticker, h.get("name", ticker), today))
    return out
