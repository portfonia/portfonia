"""Fetch daily FX rates from yfinance and upsert into fx_rates table."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.timezones import ET
from app.models.fx_rate import FxRate
from app.services._yfinance import fetch_last_close

logger = logging.getLogger(__name__)

# Pairs to fetch: DB name → yfinance ticker
_PAIRS: dict[str, str] = {
    "USDCNY": "USDCNY=X",
    "USDHKD": "USDHKD=X",
    "USDCNH": "USDCNH=X",
}


@dataclass
class FxFetchResult:
    upserted: int = 0
    failed: list[str] = field(default_factory=list)


def _fetch_rates(pairs: dict[str, str]) -> dict[str, tuple[Decimal, date]]:
    """
    Batch-fetch close rates for the given yfinance FX tickers.

    Returns {pair_name: (rate, rate_date_et)} where rate_date_et is the
    trading-day date in US Eastern Time (design §6.2). Pairs with no data
    are omitted.
    """
    points = fetch_last_close(list(pairs.values()))

    result: dict[str, tuple[Decimal, date]] = {}
    for pair_name, yf_ticker in pairs.items():
        point = points.get(yf_ticker)
        if point is None:
            continue
        rate_value, as_of = point
        rate_date = as_of.astimezone(ET).date()
        result[pair_name] = (Decimal(str(rate_value)), rate_date)

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
