"""Tests for the price capture service (ADR-002 step 2b)."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.holding import Holding
from app.models.price_snapshot import PriceSnapshot
from app.services.price_capture import _upsert, capture_prices

_USER = uuid.UUID("00000000-0000-0000-0000-000000000001")


def _holding(name: str, ticker: str, market: str | None = None) -> Holding:
    return Holding(
        user_id=_USER,
        name=name,
        ticker=ticker,
        pricing_mode="auto",
        currency="USD",
        market=market,
    )


def test_capture_close_stores_ohlcv(db_session: Session) -> None:
    db_session.add_all([_holding("Apple", "AAPL"), _holding("Tencent", "0700.HK")])
    db_session.flush()

    ohlcv = {"AAPL": [(date(2026, 6, 5), 200.0, 205.0, 199.0, 203.5, 1000.0)]}
    with patch("app.services.price_capture.fetch_ohlcv_range", return_value=ohlcv):
        n = capture_prices(db_session, market="US", session_node="close")

    assert n == 1  # only AAPL is a US ticker
    row = db_session.execute(
        select(PriceSnapshot).where(PriceSnapshot.ticker == "AAPL")
    ).scalar_one()
    assert row.close == Decimal("203.5")
    assert row.open == Decimal("200.0")
    assert row.trade_date == date(2026, 6, 5)
    assert row.last is None


def test_capture_close_is_idempotent(db_session: Session) -> None:
    db_session.add(_holding("Apple", "AAPL"))
    db_session.flush()
    ohlcv = {"AAPL": [(date(2026, 6, 5), 200.0, 205.0, 199.0, 203.5, 1000.0)]}
    with patch("app.services.price_capture.fetch_ohlcv_range", return_value=ohlcv):
        capture_prices(db_session, market="US", session_node="close")
        # Re-capture with a revised close → updates the same row, no duplicate.
        ohlcv["AAPL"] = [(date(2026, 6, 5), 200.0, 206.0, 199.0, 204.0, 1100.0)]
        capture_prices(db_session, market="US", session_node="close")

    rows = (
        db_session.execute(select(PriceSnapshot).where(PriceSnapshot.ticker == "AAPL"))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].close == Decimal("204.0")


def test_capture_close_backfills_multiple_days(db_session: Session) -> None:
    """A range fetch stores one row per trading day — this is catch-up."""
    db_session.add(_holding("Apple", "AAPL"))
    db_session.flush()
    bars = {
        "AAPL": [
            (date(2026, 6, 3), 1.0, 1.0, 1.0, 100.0, 1.0),
            (date(2026, 6, 4), 1.0, 1.0, 1.0, 101.0, 1.0),
            (date(2026, 6, 5), 1.0, 1.0, 1.0, 102.0, 1.0),
        ]
    }
    with patch("app.services.price_capture.fetch_ohlcv_range", return_value=bars):
        n = capture_prices(db_session, market="US", session_node="close")
    assert n == 3
    rows = (
        db_session.execute(select(PriceSnapshot).where(PriceSnapshot.ticker == "AAPL"))
        .scalars()
        .all()
    )
    assert {r.trade_date for r in rows} == {date(2026, 6, 3), date(2026, 6, 4), date(2026, 6, 5)}


def test_capture_intraday_stores_last(db_session: Session) -> None:
    db_session.add(_holding("Apple", "AAPL"))
    db_session.flush()
    with patch("app.services.price_capture.fetch_spot", return_value={"AAPL": 201.25}):
        n = capture_prices(
            db_session, market="US", session_node="open", trade_date=date(2026, 6, 5)
        )
    assert n == 1
    row = db_session.execute(
        select(PriceSnapshot).where(PriceSnapshot.session_node == "open")
    ).scalar_one()
    assert row.last == Decimal("201.25")
    assert row.close is None


def test_capture_declared_market_routes_ticker(db_session: Session) -> None:
    # A US-listed ticker the user declared as HK must capture under HK, not US.
    db_session.add(_holding("Weird", "AAPL", market="HK"))
    db_session.flush()
    with patch("app.services.price_capture.fetch_spot", return_value={"AAPL": 50.0}) as spot:
        assert capture_prices(db_session, market="US", session_node="open") == 0
        spot.assert_not_called()  # AAPL is not in the US bucket here


def test_capture_prices_tickers_filter_restricts_fetch(db_session: Session) -> None:
    """A confirm-time backfill must not pull the whole market universe (#194)."""
    db_session.add_all([_holding("Apple", "AAPL"), _holding("Nvidia", "NVDA")])
    db_session.flush()
    ohlcv = {"AAPL": [(date(2026, 6, 5), 1.0, 1.0, 1.0, 100.0, 1.0)]}
    with patch("app.services.price_capture.fetch_ohlcv_range", return_value=ohlcv) as fetch:
        n = capture_prices(db_session, market="US", session_node="close", tickers=["AAPL"])
    fetch.assert_called_once_with(["AAPL"], lookback_days=7)
    assert n == 1
    assert (
        db_session.execute(
            select(func.count()).select_from(PriceSnapshot).where(PriceSnapshot.ticker == "NVDA")
        ).scalar_one()
        == 0
    )


def test_capture_prices_empty_tickers_filter_fetches_nothing(db_session: Session) -> None:
    db_session.add(_holding("Apple", "AAPL"))
    db_session.flush()
    with patch("app.services.price_capture.fetch_ohlcv_range") as fetch:
        n = capture_prices(db_session, market="US", session_node="close", tickers=[])
    assert n == 0
    fetch.assert_not_called()


# Close-node rows bind 10 parameters each. PostgreSQL/psycopg hard-cap a
# single query at 65535 parameters, so 6554+ rows in one INSERT overflows
# (issue #194). 7000 rows = 70000 params, past that cap with margin.
_PARAM_OVERFLOW_ROW_COUNT = 7000


def test_upsert_chunks_past_postgres_parameter_limit(db_session: Session) -> None:
    """A batch that used to exceed psycopg's 65535-param cap must still write.

    Production `backfill_ohlcv_task` hit this on 2026-08-25 when a second
    user's holdings widened the system-wide US ticker set enough that
    420 days x tickers x 10 bound params overflowed a single INSERT.
    """
    now = datetime.now(tz=UTC)
    start = date(2000, 1, 1)
    rows: list[dict[str, object]] = [
        {
            "ticker": "BULK",
            "market": "US",
            "session_node": "close",
            "trade_date": start + timedelta(days=i),
            "open": 1.0,
            "high": 1.0,
            "low": 1.0,
            "close": 1.0,
            "volume": 1.0,
            "captured_at": now,
        }
        for i in range(_PARAM_OVERFLOW_ROW_COUNT)
    ]

    written = _upsert(db_session, rows)

    assert written == _PARAM_OVERFLOW_ROW_COUNT
    count = db_session.execute(
        select(func.count()).select_from(PriceSnapshot).where(PriceSnapshot.ticker == "BULK")
    ).scalar_one()
    assert count == _PARAM_OVERFLOW_ROW_COUNT
