"""Tests for the price capture service (ADR-002 step 2b)."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.holding import Holding
from app.models.price_snapshot import PriceSnapshot
from app.services.price_capture import capture_prices

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

    ohlcv = {"AAPL": (date(2026, 6, 5), 200.0, 205.0, 199.0, 203.5, 1000.0)}
    with patch("app.services.price_capture.fetch_daily_ohlcv", return_value=ohlcv):
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
    ohlcv = {"AAPL": (date(2026, 6, 5), 200.0, 205.0, 199.0, 203.5, 1000.0)}
    with patch("app.services.price_capture.fetch_daily_ohlcv", return_value=ohlcv):
        capture_prices(db_session, market="US", session_node="close")
        # Re-capture with a revised close → updates the same row, no duplicate.
        ohlcv["AAPL"] = (date(2026, 6, 5), 200.0, 206.0, 199.0, 204.0, 1100.0)
        capture_prices(db_session, market="US", session_node="close")

    rows = (
        db_session.execute(select(PriceSnapshot).where(PriceSnapshot.ticker == "AAPL"))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].close == Decimal("204.0")


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
