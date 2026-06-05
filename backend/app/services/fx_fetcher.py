"""Fetch daily FX rates from yfinance and upsert into fx_rates table."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal

import pandas as pd
import yfinance as yf
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models.fx_rate import FxRate

logger = logging.getLogger(__name__)

# Pairs to fetch: DB name → yfinance ticker
_PAIRS: dict[str, str] = {
    "USDCNY": "USDCNY=X",
    "USDHKD": "USDHKD=X",
    "USDCNH": "USDCNH=X",
}

# rate_date uses US Eastern Time per design doc §6.2
_ET = timezone(timedelta(hours=-5))  # EST; close enough for date boundaries


@dataclass
class FxFetchResult:
    upserted: int = 0
    failed: list[str] = field(default_factory=list)


def _fetch_rates(pairs: dict[str, str]) -> dict[str, tuple[Decimal, date]]:
    """
    Batch-fetch close rates for the given yfinance FX tickers.

    Returns {pair_name: (rate, rate_date_et)} where rate_date_et is the
    trading-day date in US Eastern Time. Pairs with no data are omitted.
    """
    ticker_str = " ".join(pairs.values())
    try:
        hist = yf.download(
            tickers=ticker_str,
            period="5d",
            auto_adjust=True,
            progress=False,
        )
    except Exception:
        logger.exception("yfinance download failed for FX tickers: %s", ticker_str)
        return {}

    if hist.empty:
        return {}

    close = hist["Close"]
    if isinstance(close, pd.Series):
        # Single ticker — only happens if pairs has one entry
        only_pair = next(iter(pairs.keys()))
        close = close.to_frame(name=pairs[only_pair])

    result: dict[str, tuple[Decimal, date]] = {}
    for pair_name, yf_ticker in pairs.items():
        if yf_ticker not in close.columns:
            continue
        series = close[yf_ticker].dropna()
        if series.empty:
            continue
        rate = Decimal(str(float(series.iloc[-1])))
        ts = series.index[-1]
        as_of: datetime = (
            ts.to_pydatetime() if ts.tzinfo is not None else ts.to_pydatetime().replace(tzinfo=UTC)
        )
        rate_date = as_of.astimezone(_ET).date()
        result[pair_name] = (rate, rate_date)

    return result


def update_fx_rates(session: Session) -> FxFetchResult:
    """
    Fetch today's FX rates and upsert into fx_rates table.

    Uses INSERT ... ON CONFLICT DO UPDATE so re-running is safe.
    fetched_at is always updated on conflict to reflect the latest fetch time.
    """
    result = FxFetchResult()
    fetched_at = datetime.now(tz=UTC)

    rates = _fetch_rates(_PAIRS)
    if not rates:
        result.failed = list(_PAIRS.keys())
        logger.error("yfinance returned no FX data")
        return result

    for pair_name, (rate, rate_date) in rates.items():
        stmt = (
            insert(FxRate)
            .values(
                pair=pair_name,
                rate=rate,
                rate_date=rate_date,
                source="yfinance",
                fetched_at=fetched_at,
            )
            .on_conflict_do_update(
                constraint="uq_fx_rates_pair_rate_date",
                set_={"rate": rate, "fetched_at": fetched_at},
            )
        )
        session.execute(stmt)
        result.upserted += 1
        logger.info("FX %s = %.6f  rate_date=%s", pair_name, rate, rate_date)

    for pair_name in _PAIRS:
        if pair_name not in rates:
            result.failed.append(pair_name)
            logger.warning("no data for FX pair %s", pair_name)

    session.flush()
    return result
