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
from app.services.price_capture import (
    _UPSERT_CHUNK_SIZE,
    _upsert,
    capture_fund_navs,
    capture_prices,
)

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


def _fund_holding(name: str, fund_code: str) -> Holding:
    return Holding(
        user_id=_USER,
        name=name,
        fund_code=fund_code,
        pricing_mode="auto",
        currency="CNY",
        market="A-Share",
        asset_class="EQUITY_US_BROAD",
    )


def test_capture_fund_navs_stores_close_under_fund_code(db_session: Session) -> None:
    db_session.add(_fund_holding("Huaxia SSE 50 ETF", "513100"))
    db_session.flush()
    history = [(date(2026, 8, 22), Decimal("1.23"))]
    with patch("app.services.fund_nav_fetcher.fetch_nav_history", return_value=history):
        n = capture_fund_navs(db_session)

    assert n == 1
    row = db_session.execute(
        select(PriceSnapshot).where(PriceSnapshot.ticker == "513100")
    ).scalar_one()
    assert row.close == Decimal("1.23")
    assert row.session_node == "close"
    assert row.market == "A-Share"
    assert row.trade_date == date(2026, 8, 22)


def test_capture_fund_navs_fund_codes_filter_restricts_fetch(db_session: Session) -> None:
    """Confirm-time NAV capture must not rescan every fund in the system (#196)."""
    db_session.add_all(
        [
            _fund_holding("Huaxia SSE 50 ETF", "513100"),
            _fund_holding("Huatai CSI 300 ETF", "510300"),
        ]
    )
    db_session.flush()
    history = [(date(2026, 8, 22), Decimal("1.23"))]
    with patch("app.services.fund_nav_fetcher.fetch_nav_history", return_value=history) as fetch:
        n = capture_fund_navs(db_session, fund_codes=["513100"])

    fetch.assert_called_once()
    assert fetch.call_args.args[0] == "513100"
    assert n == 1
    assert (
        db_session.execute(
            select(func.count()).select_from(PriceSnapshot).where(PriceSnapshot.ticker == "510300")
        ).scalar_one()
        == 0
    )


def test_capture_fund_navs_empty_fund_codes_filter_fetches_nothing(
    db_session: Session,
) -> None:
    db_session.add(_fund_holding("Huaxia SSE 50 ETF", "513100"))
    db_session.flush()
    with patch("app.services.fund_nav_fetcher.fetch_nav_history") as fetch:
        n = capture_fund_navs(db_session, fund_codes=[])
    assert n == 0
    fetch.assert_not_called()


def test_capture_fund_navs_dedupes_same_fund_code_across_holdings(
    db_session: Session,
) -> None:
    """Two users (or lots) of the same fund must not double-fetch the NAV API."""
    other = uuid.uuid4()
    db_session.add_all(
        [
            _fund_holding("Huaxia SSE 50 ETF", "513100"),
            Holding(
                user_id=other,
                name="Same fund, other user",
                fund_code="513100",
                pricing_mode="auto",
                currency="CNY",
                market="A-Share",
                asset_class="EQUITY_US_BROAD",
            ),
        ]
    )
    db_session.flush()
    history = [(date(2026, 8, 22), Decimal("1.23"))]
    with patch("app.services.fund_nav_fetcher.fetch_nav_history", return_value=history) as fetch:
        n = capture_fund_navs(db_session)
    fetch.assert_called_once()
    assert n == 1


def test_capture_fund_navs_prefers_declared_market_over_null_default(
    db_session: Session,
) -> None:
    """Same fund_code, mixed NULL vs declared market: declared wins, stably."""
    other = uuid.uuid4()
    db_session.add_all(
        [
            Holding(
                user_id=_USER,
                name="Null market lot",
                fund_code="513100",
                pricing_mode="auto",
                currency="CNY",
                market=None,
                asset_class="EQUITY_US_BROAD",
            ),
            Holding(
                user_id=other,
                name="Declared HK lot",
                fund_code="513100",
                pricing_mode="auto",
                currency="CNY",
                market="HK",
                asset_class="EQUITY_US_BROAD",
            ),
        ]
    )
    db_session.flush()
    history = [(date(2026, 8, 22), Decimal("1.23"))]
    with patch("app.services.fund_nav_fetcher.fetch_nav_history", return_value=history):
        n = capture_fund_navs(db_session)
    assert n == 1
    row = db_session.execute(
        select(PriceSnapshot).where(PriceSnapshot.ticker == "513100")
    ).scalar_one()
    assert row.market == "HK"


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
    # Close-node rows bind one param per dict key. Lock the chunk math the
    # source comment states, derived from this row shape not a frozen 10.
    assert _UPSERT_CHUNK_SIZE * len(rows[0]) <= 65_535

    written = _upsert(db_session, rows)

    assert written == _PARAM_OVERFLOW_ROW_COUNT
    count = db_session.execute(
        select(func.count()).select_from(PriceSnapshot).where(PriceSnapshot.ticker == "BULK")
    ).scalar_one()
    assert count == _PARAM_OVERFLOW_ROW_COUNT
