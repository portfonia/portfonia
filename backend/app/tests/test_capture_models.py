"""Smoke tests for the ADR-002 capture-layer tables (news, price_snapshots)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.news import News
from app.models.price_snapshot import PriceSnapshot
from app.services.intel_records import build_headline_record
from app.services.news_fetcher import NewsItem


def test_news_url_hash_unique(db_session: Session) -> None:
    db_session.add(
        News(
            url_hash="abc123",
            published_at=datetime(2026, 6, 6, 12, 0, tzinfo=UTC),
            record=build_headline_record(
                NewsItem(
                    "abc123",
                    "Fed holds rates",
                    "",
                    "",
                    datetime(2026, 6, 6, 12, 0, tzinfo=UTC),
                    "...",
                ),
                "article",
                None,
            ),
        )
    )
    db_session.flush()
    db_session.add(
        News(
            url_hash="abc123",
            published_at=datetime(2026, 6, 6, 13, 0, tzinfo=UTC),
            record=build_headline_record(
                NewsItem("abc123", "dup", "", "", datetime(2026, 6, 6, 13, 0, tzinfo=UTC), None),
                "article",
                None,
            ),
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_price_snapshot_key_unique_and_ohlcv(db_session: Session) -> None:
    snap = PriceSnapshot(
        ticker="AAPL",
        market="US",
        session_node="close",
        trade_date=date(2026, 6, 5),
        open=Decimal("200.0"),
        high=Decimal("205.0"),
        low=Decimal("199.0"),
        close=Decimal("203.5"),
        volume=Decimal("123456"),
    )
    db_session.add(snap)
    db_session.flush()

    row = db_session.execute(
        select(PriceSnapshot).where(PriceSnapshot.ticker == "AAPL")
    ).scalar_one()
    assert row.close == Decimal("203.5")
    assert row.last is None  # close node leaves intraday last null

    # Same (ticker, market, session_node, trade_date) collides.
    db_session.add(
        PriceSnapshot(ticker="AAPL", market="US", session_node="close", trade_date=date(2026, 6, 5))
    )
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_price_snapshot_intraday_last(db_session: Session) -> None:
    db_session.add(
        PriceSnapshot(
            ticker="AAPL",
            market="US",
            session_node="open",
            trade_date=date(2026, 6, 5),
            last=Decimal("201.2"),
        )
    )
    db_session.flush()
    row = db_session.execute(
        select(PriceSnapshot).where(PriceSnapshot.session_node == "open")
    ).scalar_one()
    assert row.last == Decimal("201.2")
    assert row.close is None
