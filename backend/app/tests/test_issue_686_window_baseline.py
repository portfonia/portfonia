"""Real-Postgres acceptance tests for issue #686."""

from datetime import date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.timezones import ET
from app.models.holding import Holding
from app.models.price_snapshot import PriceSnapshot
from app.models.ticker_theme import TickerTheme
from app.services.price_capture import _upsert_chunk
from app.services.window_data import compute_global_moves, select_user_anomalies
from app.tests.conftest import TEST_USER_ID, seed_user

D = date(2026, 10, 5)
NEXT = date(2026, 10, 6)
FIRST = datetime(2026, 10, 5, 16, tzinfo=ET)
REPLACEMENT = datetime(2026, 10, 6, 16, tzinfo=ET)
RECAPTURE = datetime(2026, 10, 6, 16, 5, tzinfo=ET)
START = datetime(2026, 10, 5, 17, tzinfo=ET)
END = datetime(2026, 10, 6, 17, tzinfo=ET)


def _row(day: date, close: Decimal | None, captured_at: datetime) -> dict[str, object]:
    return dict(
        ticker="TEST686",
        market="US",
        session_node="close",
        trade_date=day,
        close=close,
        captured_at=captured_at,
    )


def _stored(session: Session) -> PriceSnapshot:
    session.expire_all()
    return session.scalars(select(PriceSnapshot).where(PriceSnapshot.ticker == "TEST686")).one()


def _holdings(session: Session) -> list[Holding]:
    seed_user(session, TEST_USER_ID)
    holdings = [
        Holding(
            user_id=TEST_USER_ID,
            name=name,
            ticker="TEST686",
            pricing_mode="auto",
            currency="USD",
            market="US",
            asset_type="stock",
            asset_class="STOCK",
            shares=Decimal(shares),
        )
        for name, shares in [("First lot", "10"), ("Second lot", "30")]
    ]
    session.add_all(holdings)
    session.flush()
    return holdings


def test_usable_close_recapture_preserves_first_capture(db_session: Session) -> None:
    _upsert_chunk(db_session, [_row(D, Decimal("100"), FIRST)])
    _upsert_chunk(db_session, [_row(D, Decimal("101"), REPLACEMENT)])
    stored = _stored(db_session)
    assert stored.close == Decimal("101")
    assert stored.captured_at == FIRST


@pytest.mark.parametrize("unusable_close", [None, Decimal("0")], ids=["null", "zero"])
def test_unusable_close_repair_sets_and_then_preserves_capture(
    db_session: Session, unusable_close: Decimal | None
) -> None:
    _upsert_chunk(db_session, [_row(D, unusable_close, FIRST)])
    _upsert_chunk(db_session, [_row(D, Decimal("100"), REPLACEMENT)])
    stored = _stored(db_session)
    assert stored.close == Decimal("100")
    assert stored.captured_at == REPLACEMENT
    _upsert_chunk(db_session, [_row(D, Decimal("101"), RECAPTURE)])
    stored = _stored(db_session)
    assert stored.close == Decimal("101")
    assert stored.captured_at == REPLACEMENT


def test_recaptured_previous_close_stays_window_baseline(db_session: Session) -> None:
    _holdings(db_session)
    _upsert_chunk(
        db_session,
        [
            _row(date(2026, 10, 2), Decimal("95"), datetime(2026, 10, 2, 16, tzinfo=ET)),
            _row(D, Decimal("100"), FIRST),
            _row(NEXT, Decimal("107"), REPLACEMENT),
        ],
    )
    _upsert_chunk(
        db_session, [_row(D, Decimal("100"), RECAPTURE), _row(NEXT, Decimal("107"), RECAPTURE)]
    )
    db_session.expire_all()
    moves, trading_days = compute_global_moves(db_session, START, END)
    move = moves["TEST686"]
    assert move.baseline_date == D
    assert trading_days == 1
    assert move.net_pct == Decimal("0.0700")
    assert move.max_day_pct == Decimal("0.0700")
    assert move.max_day_date == NEXT


def test_premarket_first_close_is_in_window(db_session: Session) -> None:
    _holdings(db_session)
    _upsert_chunk(
        db_session,
        [
            _row(date(2026, 10, 2), Decimal("100"), datetime(2026, 10, 2, 16, tzinfo=ET)),
            _row(D, Decimal("107"), FIRST),
        ],
    )
    moves, trading_days = compute_global_moves(
        db_session, datetime(2026, 10, 5, 8, tzinfo=ET), datetime(2026, 10, 5, 17, tzinfo=ET)
    )
    assert trading_days == 1
    assert moves["TEST686"].baseline_date == date(2026, 10, 2)
    assert moves["TEST686"].latest_date == D
    assert moves["TEST686"].net_pct == Decimal("0.0700")


def test_multi_lot_standalone_anomaly_keeps_first_row(db_session: Session) -> None:
    holdings = _holdings(db_session)
    _upsert_chunk(
        db_session, [_row(D, Decimal("100"), FIRST), _row(NEXT, Decimal("107"), REPLACEMENT)]
    )
    moves, trading_days = compute_global_moves(db_session, START, END)
    anomalies = select_user_anomalies(moves, holdings, trading_days, {}, {})
    assert len(anomalies) == 1
    assert anomalies[0].identifier == "TEST686"
    assert anomalies[0].name == "First lot"
    assert anomalies[0].asset_type == "STOCK"


def test_multi_lot_theme_keeps_all_constituents(db_session: Session) -> None:
    holdings = _holdings(db_session)
    theme = TickerTheme(
        ticker="TEST686",
        theme="test686_theme",
        theme_label_zh="Test theme",
        theme_label_en="Test theme",
        asset_class="STOCK",
    )
    db_session.add(theme)
    db_session.flush()
    _upsert_chunk(
        db_session, [_row(D, Decimal("100"), FIRST), _row(NEXT, Decimal("107"), REPLACEMENT)]
    )
    moves, trading_days = compute_global_moves(db_session, START, END)
    anomalies = select_user_anomalies(moves, holdings, trading_days, {"TEST686": theme}, {})
    assert len(anomalies) == 1
    anomaly = anomalies[0]
    assert anomaly.identifier == "test686_theme"
    assert anomaly.pct_change == Decimal("0.0700")
    assert anomaly.current_price == Decimal("107")
    assert anomaly.prev_price == Decimal("100")
    assert anomaly.baseline_date == D
    assert anomaly.latest_date == NEXT
    assert anomaly.threshold == Decimal("0.05")
    assert anomaly.trigger == "single_day"
    assert [
        (c.name, c.identifier, c.pct_change, c.current_value) for c in anomaly.constituents
    ] == [
        ("Second lot", "TEST686", Decimal("0.0700"), Decimal("3210")),
        ("First lot", "TEST686", Decimal("0.0700"), Decimal("1070")),
    ]
