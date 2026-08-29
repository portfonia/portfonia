"""Fetch latest close prices and sector metadata from yfinance."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

import yfinance as yf
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.holding import Holding
from app.services._yfinance import _normalize_ticker, fetch_last_close
from app.services.sector_taxonomy import map_yf_sector

logger = logging.getLogger(__name__)

# Asset types we attempt to classify by sector (funds/cash/wmf have no single sector).
_SECTOR_ASSET_TYPES = {"stock", "etf"}


@dataclass
class PriceFetchResult:
    updated: int = 0
    failed: list[str] = field(default_factory=list)


def _fetch_yf_sector(ticker: str) -> str | None:
    """Best-effort single-ticker sector lookup. Returns None on any failure."""
    try:
        info = yf.Ticker(ticker).info
    except Exception:
        logger.warning("sector lookup failed for %s", ticker)
        return None
    sector = info.get("sector") if isinstance(info, dict) else None
    return sector if isinstance(sector, str) else None


def update_holding_prices(session: Session) -> PriceFetchResult:
    """
    Load all auto-mode holdings with a ticker, batch-fetch prices, and
    write market_price / price_as_of / price_fetched_at back to the DB.

    - price_as_of    : exchange timestamp of the price data point
    - price_fetched_at: when this process called yfinance

    Retries once on total failure before giving up.
    """
    result = PriceFetchResult()

    rows: list[Holding] = list(
        session.execute(
            select(Holding).where(
                Holding.pricing_mode == "auto",
                Holding.ticker.isnot(None),
            )
        ).scalars()
    )
    if not rows:
        return result

    unique_tickers = list({r.ticker for r in rows if r.ticker})

    points = fetch_last_close(unique_tickers)

    # Total-failure retry: nothing came back at all — wait and retry the full list.
    if not points:
        logger.warning("yfinance returned no data, retrying full list after 5s")
        time.sleep(5)
        points = fetch_last_close(unique_tickers)

    if not points:
        result.failed = unique_tickers
        logger.error("yfinance returned no data after retry for: %s", unique_tickers)
        return result

    # Partial-failure retry: any ticker absent from the first pass gets a
    # single-ticker retry with a short pause between calls.  Single-ticker
    # requests hit a different Yahoo endpoint path and are more reliable.
    # `points` is keyed by fetch_last_close's normalized ticker (issue #204:
    # e.g. "PSH.L" for the raw "PSH"), so membership must check the
    # normalized form or every normalized ticker looks perpetually missing.
    failed_first_pass = [t for t in unique_tickers if _normalize_ticker(t) not in points]
    if failed_first_pass:
        logger.warning(
            "yfinance partial failure (%d/%d tickers missing), retrying individually: %s",
            len(failed_first_pass),
            len(unique_tickers),
            failed_first_pass,
        )
        for failed_ticker in failed_first_pass:
            time.sleep(1)
            retry = fetch_last_close([failed_ticker])
            if retry:
                points.update(retry)
                logger.info("single-ticker retry succeeded for %s", failed_ticker)
            else:
                logger.warning("single-ticker retry also failed for %s", failed_ticker)

    fetched_at = datetime.now(tz=UTC)

    for row in rows:
        ticker = row.ticker
        assert ticker is not None  # filtered in query above
        normalized = _normalize_ticker(ticker)
        if normalized not in points:
            result.failed.append(ticker)
            logger.warning("no price data for ticker %s", ticker)
            continue
        price, as_of = points[normalized]
        row.market_price = Decimal(str(price))
        row.price_as_of = as_of
        row.price_fetched_at = fetched_at
        result.updated += 1

    result.failed = sorted(set(result.failed))
    session.flush()
    return result


def backfill_sectors(session: Session) -> int:
    """
    Populate the sector column for stock/etf holdings that lack one.

    Sector rarely changes, so we only look it up when missing — this is a
    one-time backfill per holding, not part of every price refresh.
    A-share / HK tickers that yfinance cannot classify resolve to "Other"
    via the taxonomy (design §6.4 Ring 0 strategy). Returns rows updated.
    """
    rows: list[Holding] = list(
        session.execute(
            select(Holding).where(
                Holding.ticker.isnot(None),
                Holding.sector.is_(None),
                Holding.asset_type.in_(_SECTOR_ASSET_TYPES),
            )
        ).scalars()
    )

    updated = 0
    for row in rows:
        assert row.ticker is not None
        row.sector = map_yf_sector(_fetch_yf_sector(row.ticker))
        updated += 1

    session.flush()
    return updated
