"""Tests for the incremental-window data layer (ADR-002 steps 3-4)."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models.holding import Holding
from app.models.news import News
from app.models.price_snapshot import PriceSnapshot
from app.models.report import Report
from app.services.window_data import (
    BOOTSTRAP_WATERMARK,
    detect_window_anomalies,
    load_news_window,
    user_watermark,
)

_USER = uuid.UUID("00000000-0000-0000-0000-000000000001")


def _news(url: str, when: datetime) -> News:
    return News(url_hash=url, title="t", source="S", url=url, summary="s", published_at=when)


def _close(ticker: str, d: date, close: float) -> PriceSnapshot:
    return PriceSnapshot(
        ticker=ticker,
        market="US",
        session_node="close",
        trade_date=d,
        close=Decimal(str(close)),
    )


# --- watermark ---------------------------------------------------------------


def test_watermark_cold_start(db_session: Session) -> None:
    assert user_watermark(db_session, _USER, "incremental") == BOOTSTRAP_WATERMARK


def test_watermark_from_last_report(db_session: Session) -> None:
    end = datetime(2026, 6, 10, 20, 30, tzinfo=UTC)
    db_session.add(
        Report(
            user_id=_USER,
            report_date=date(2026, 6, 10),
            report_type="incremental",
            status="success",
            period_end=end,
        )
    )
    db_session.flush()
    assert user_watermark(db_session, _USER, "incremental") == end


# --- news window -------------------------------------------------------------


def test_load_news_window_filters_by_published_at(db_session: Session) -> None:
    db_session.add_all(
        [
            _news("a", datetime(2026, 6, 1, tzinfo=UTC)),  # before window
            _news("b", datetime(2026, 6, 3, tzinfo=UTC)),  # in window
            _news("c", datetime(2026, 6, 5, tzinfo=UTC)),  # in window
            _news("d", datetime(2026, 6, 9, tzinfo=UTC)),  # after window
        ]
    )
    db_session.flush()
    items = load_news_window(
        db_session, datetime(2026, 6, 2, tzinfo=UTC), datetime(2026, 6, 6, tzinfo=UTC)
    )
    assert {i.url_hash for i in items} == {"b", "c"}


# --- anomalies from snapshots ------------------------------------------------


def test_detect_window_anomalies_flags_move_over_threshold(db_session: Session) -> None:
    db_session.add(
        Holding(
            user_id=_USER,
            name="Apple",
            ticker="AAPL",
            pricing_mode="auto",
            currency="USD",
            asset_type="stock",
        )
    )
    db_session.add_all(
        [
            _close("AAPL", date(2026, 6, 2), 100.0),  # baseline (at/before start)
            _close("AAPL", date(2026, 6, 5), 105.0),  # +5% > 3% stock threshold
        ]
    )
    db_session.flush()

    anomalies, trading_days = detect_window_anomalies(
        db_session,
        datetime(2026, 6, 2, 16, 0, tzinfo=UTC),
        datetime(2026, 6, 5, 20, 30, tzinfo=UTC),
    )
    assert len(anomalies) == 1
    a = anomalies[0]
    assert a.identifier == "AAPL"
    assert a.prev_price == Decimal("100.0")
    assert a.current_price == Decimal("105.0")
    assert trading_days == 1  # one close trade_date inside (start_date, end_date]


def test_detect_window_anomalies_ignores_small_move_and_new_position(db_session: Session) -> None:
    db_session.add_all(
        [
            Holding(
                user_id=_USER,
                name="Apple",
                ticker="AAPL",
                pricing_mode="auto",
                currency="USD",
                asset_type="stock",
            ),
            Holding(
                user_id=_USER,
                name="NewCo",
                ticker="NEW",
                pricing_mode="auto",
                currency="USD",
                asset_type="stock",
            ),
        ]
    )
    db_session.add_all(
        [
            _close("AAPL", date(2026, 6, 2), 100.0),
            _close("AAPL", date(2026, 6, 5), 101.0),  # +1% < 3% → not flagged
            _close("NEW", date(2026, 6, 5), 50.0),  # no baseline before start → skipped
        ]
    )
    db_session.flush()

    anomalies, _ = detect_window_anomalies(
        db_session,
        datetime(2026, 6, 2, 16, 0, tzinfo=UTC),
        datetime(2026, 6, 5, 20, 30, tzinfo=UTC),
    )
    assert anomalies == []
