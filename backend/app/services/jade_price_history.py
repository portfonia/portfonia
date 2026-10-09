"""Nightly shared total-return history fill for active Jade users."""

import logging
import re
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import cast

import httpx
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models.holding import Holding
from app.models.jade_price import JadePricePoint, JadePriceSeries
from app.models.user import User
from app.services._yfinance import fetch_ohlcv_range_bounded
from app.services.fund_nav_fetcher import fetch_nav_history_pages
from app.services.instrument_symbols import normalize_legacy_ticker
from app.services.jade_replay_config import (
    FETCH_MARGIN_DAYS,
    FIXED_ETF_SYMBOLS,
    REPLAY_YEARS,
    years_before,
)
from app.services.markets import is_capture_supported

logger = logging.getLogger(__name__)
_DISTRIBUTION = re.compile(r"^每10份派现金(\d+(?:\.\d+)?)元$")


@dataclass
class FillSummary:
    attempted: int = 0
    written: int = 0
    failed: int = 0


def _upsert(session: Session, key: str, day: date, close: Decimal, raw: Decimal | None) -> None:
    stmt = insert(JadePricePoint).values(series_key=key, trade_date=day, close=close, raw_close=raw)
    session.execute(
        stmt.on_conflict_do_update(
            index_elements=["series_key", "trade_date"],
            set_={"close": stmt.excluded.close, "raw_close": stmt.excluded.raw_close},
        )
    )


def refresh_jade_price_history(session: Session, today: date) -> FillSummary:
    result = FillSummary()
    users = list(
        session.scalars(
            select(User.id).where(
                User.subscription_status == "active", User.subscription_type == "jade"
            )
        )
    )
    if not users:
        return result
    keys = {"yf:" + s for s in FIXED_ETF_SYMBOLS}
    for h in session.scalars(
        select(Holding).where(Holding.user_id.in_(users), Holding.pricing_mode == "auto")
    ):
        if is_capture_supported(h) and (h.ticker or h.fund_code):
            keys.add(
                "yf:" + normalize_legacy_ticker(h.ticker) if h.ticker else "nav:" + str(h.fund_code)
            )
    session.execute(
        insert(JadePriceSeries)
        .values([{"series_key": key} for key in sorted(keys)])
        .on_conflict_do_nothing(index_elements=["series_key"])
    )
    start = years_before(today, REPLAY_YEARS) - timedelta(days=FETCH_MARGIN_DAYS)
    symbols = sorted(k[3:] for k in keys if k.startswith("yf:"))
    bars = fetch_ohlcv_range_bounded(symbols, start, today + timedelta(days=1))
    for key in sorted(keys):
        result.attempted += 1
        try:
            s = session.get(JadePriceSeries, key)
            if s is None:
                s = JadePriceSeries(series_key=key)
                session.add(s)
                session.flush()
            s.last_attempt_on = today
            wrote = False
            if key.startswith("yf:"):
                for day, _, _, _, close, _ in bars.get(key[3:], []):
                    _upsert(session, key, day, Decimal(str(close)), None)
                    wrote = True
            elif not s.unusable_reason:
                last = session.scalar(
                    select(JadePricePoint)
                    .where(JadePricePoint.series_key == key)
                    .order_by(JadePricePoint.trade_date.desc())
                    .limit(1)
                )
                with httpx.Client() as client:
                    rows = fetch_nav_history_pages(
                        key[4:], client, start, today, last.trade_date if last else None
                    )
                if rows is not None:
                    prev_index = last.close if last else None
                    prev_nav = last.raw_close if last else None
                    points: list[tuple[date, Decimal, Decimal]] = []
                    for row in rows:
                        if last is not None and row.nav_date <= last.trade_date:
                            continue
                        distribution = _DISTRIBUTION.fullmatch(row.fhsp) if row.fhsp else None
                        if row.fhsp and distribution is None:
                            logger.warning(
                                "Unparsed distribution for fund %s: %s", key[4:], row.fhsp
                            )
                            s.unusable_reason = "unparsed_distribution"
                            session.execute(
                                delete(JadePricePoint).where(JadePricePoint.series_key == key)
                            )
                            points = []
                            break
                        cash = Decimal(distribution[1]) / 10 if distribution else Decimal(0)
                        index = (
                            row.unit_nav
                            if prev_index is None
                            else prev_index * (row.unit_nav + cash) / cast(Decimal, prev_nav)
                        )
                        points.append((row.nav_date, index, row.unit_nav))
                        prev_index = index
                        prev_nav = row.unit_nav
                    for day, index, nav in points:
                        _upsert(session, key, day, index, nav)
                        wrote = True
            if wrote:
                s.last_success_on = today
                result.written += 1
            else:
                result.failed += 1
            session.commit()
        except Exception:
            session.rollback()
            result.failed += 1
            logger.exception("[!] Jade history fill failed for %s", key)
    logger.info(
        "[OK] Jade history: attempted=%d written=%d failed=%d",
        result.attempted,
        result.written,
        result.failed,
    )
    return result
