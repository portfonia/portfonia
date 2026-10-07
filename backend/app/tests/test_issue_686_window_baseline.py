"""Real-Postgres exchange-session acceptance tests for issue #686."""

from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.core.timezones import ET
from app.models.holding import Holding
from app.models.price_snapshot import PriceSnapshot
from app.models.ticker_theme import TickerTheme
from app.services.instrument_universe import UniverseEntry
from app.services.intel_deepen_config import load_intel_deepen_config
from app.services.intel_signals import compute_signals
from app.services.window_data import (
    compute_global_moves,
    detect_window_anomalies,
    latest_window_close_date,
)
from app.tests.conftest import TEST_USER_ID, seed_user

START = datetime(2026, 10, 5, 17, tzinfo=ET)
END = datetime(2026, 10, 6, 17, tzinfo=ET)


def _holding(
    session: Session,
    ticker: str = "AAA686",
    market: str = "US",
    position: int | None = 0,
    name: str = "First",
    shares: int = 10,
) -> Holding:
    row = Holding(
        user_id=TEST_USER_ID,
        name=name,
        ticker=ticker,
        market=market,
        pricing_mode="auto",
        currency="USD",
        asset_class="STOCK",
        shares=Decimal(shares),
        position=position,
    )
    session.add(row)
    session.flush()
    return row


def _close(
    session: Session, day: date, value: int, ticker: str = "AAA686", market: str = "US"
) -> None:
    session.add(
        PriceSnapshot(
            ticker=ticker,
            market=market,
            session_node="close",
            trade_date=day,
            close=Decimal(value),
            captured_at=END - timedelta(hours=1),
        )
    )
    session.flush()


@pytest.fixture(autouse=True)
def _user(db_session: Session) -> None:
    seed_user(db_session, TEST_USER_ID)


def test_686_01_recapture_is_irrelevant(db_session: Session) -> None:
    _holding(db_session)
    for day, value in [(2, 95), (5, 100), (6, 107)]:
        _close(db_session, date(2026, 10, day), value)
    moves, count = compute_global_moves(db_session, START, END)
    move = next(iter(moves.values()))
    assert move.baseline_date == date(2026, 10, 5)
    assert move.net_pct == Decimal(".0700")
    assert count == 1


def test_686_02_missing_exact_baseline_has_no_move(db_session: Session) -> None:
    _holding(db_session)
    _close(db_session, date(2026, 10, 2), 95)
    _close(db_session, date(2026, 10, 6), 107)
    moves, count = compute_global_moves(db_session, START, END)
    assert moves == {}
    assert count == 1


def test_686_03_premarket_uses_official_close(db_session: Session) -> None:
    _holding(db_session)
    _close(db_session, date(2026, 10, 5), 100)
    _close(db_session, date(2026, 10, 6), 107)
    moves, count = compute_global_moves(db_session, START.replace(day=6, hour=8), END)
    assert count == 1
    move = next(iter(moves.values()))
    assert move.baseline_date == date(2026, 10, 5)
    assert move.latest_date == date(2026, 10, 6)


def test_686_04_holiday_market_has_no_move(db_session: Session) -> None:
    _holding(db_session)
    _holding(db_session, "CN686", "A-Share")
    for market, ticker in [("US", "AAA686"), ("A-Share", "CN686")]:
        for day, value in [(2, 95), (5, 100), (6, 107)]:
            _close(db_session, date(2026, 10, day), value, ticker, market)
    moves, count = compute_global_moves(db_session, START, END)
    assert {m.identifier for m in moves.values()} == {"AAA686"}
    assert count == 1


def test_686_05_missing_middle_session_does_not_make_day_move(db_session: Session) -> None:
    _holding(db_session)
    for day, value in [(1, 100), (2, 100), (5, 100), (7, 120)]:
        _close(db_session, date(2026, 10, day), value)
    moves, count = compute_global_moves(db_session, START.replace(day=2), END.replace(day=7))
    move = next(iter(moves.values()))
    assert count == 3
    assert move.net_pct == Decimal(".2000")
    assert move.max_day_pct == Decimal(".0000")
    assert move.max_day_date == date(2026, 10, 5)
    assert move.d3_pct is None
    assert move.prev_close is None


@pytest.mark.parametrize(
    "missing",
    [None, 1, 3, 5, 0],
    ids=["present", "missing-d1", "missing-d3", "missing-d5", "missing-latest"],
)
def test_686_06_intel_requires_calendar_comparison(
    db_session: Session, missing: int | None
) -> None:
    days = [date(2026, 10, d) for d in [6, 5, 2, 1]] + [date(2026, 9, d) for d in [30, 29, 28]]
    for i, day in enumerate(days):
        if i != missing:
            _close(db_session, day, 120 if i == 0 else 100)
    signal = compute_signals(
        db_session,
        [UniverseEntry("AAA686", "AAA686", "US")],
        END.date(),
        START,
        load_intel_deepen_config(),
        slot="post_close",
        now=END,
    )["AAA686"]
    if missing == 0:
        assert signal.d1 is signal.d3 is signal.d5 is None
        assert not signal.mover
    else:
        for name, offset in [("d1", 1), ("d3", 3), ("d5", 5)]:
            value = getattr(signal, name)
            if missing is not None and missing <= offset:
                assert value is None
            else:
                assert value == pytest.approx(0.2)


def test_686_07_standalone_keeps_lowest_position(db_session: Session) -> None:
    _holding(db_session, position=1, name="Second")
    _holding(db_session, position=0, name="First")
    for day, value in [(2, 100), (5, 100), (6, 107)]:
        _close(db_session, date(2026, 10, day), value)
    anomalies, _ = detect_window_anomalies(db_session, START, END, TEST_USER_ID)
    assert len(anomalies) == 1
    assert anomalies[0].name == "First"


def test_686_08_themed_lots_keep_both_values(db_session: Session) -> None:
    _holding(db_session, position=1, name="Second", shares=30)
    _holding(db_session, position=0, name="First", shares=10)
    db_session.add(
        TickerTheme(
            ticker="AAA686",
            theme="theme686",
            theme_label_zh="Test",
            theme_label_en="Test",
            asset_class="STOCK",
        )
    )
    for day, value in [(2, 100), (5, 100), (6, 107)]:
        _close(db_session, date(2026, 10, day), value)
    anomalies, _ = detect_window_anomalies(db_session, START, END, TEST_USER_ID)
    assert len(anomalies) == 1
    assert anomalies[0].pct_change == Decimal(".0700")
    assert [(c.name, c.current_value) for c in anomalies[0].constituents] == [
        ("Second", Decimal(3210)),
        ("First", Decimal(1070)),
    ]


def test_686_markets_are_isolated_and_user_market_follows_position(db_session: Session) -> None:
    _holding(db_session, market="US", position=1, name="US lot")
    _holding(db_session, market="UK", position=0, name="UK lot")
    for market, latest in [("US", 150), ("UK", 107)]:
        for day, value in [(2, 100), (5, 100), (6, latest)]:
            _close(db_session, date(2026, 10, day), value, market=market)
    anomalies, _ = detect_window_anomalies(db_session, START, END, TEST_USER_ID)
    assert len(anomalies) == 1
    assert anomalies[0].market == "UK"
    assert anomalies[0].name == "UK lot"
    assert anomalies[0].pct_change == Decimal(".0700")


def test_686_calendar_count_and_data_cutoff_differ(db_session: Session) -> None:
    _holding(db_session)
    for day in [2, 5]:
        _close(db_session, date(2026, 10, day), 100)
    moves, count = compute_global_moves(db_session, START.replace(day=2), END)
    assert count == 2
    assert latest_window_close_date(db_session, START.replace(day=2), END) == date(2026, 10, 5)
    assert len(moves) == 1
