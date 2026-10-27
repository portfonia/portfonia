"""Unit tests for technical-position metrics (#4).

Pure-math helpers are exercised directly on synthetic close series; the
session-backed loaders are covered indirectly via the report-generator suite
(plus a couple of direct DB-backed cases below for issue #204's PSH/PSH.L
ticker-normalization gap).
"""

from __future__ import annotations

import statistics
from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models.price_snapshot import PriceSnapshot
from app.services import technical_position as tp


def test_sma_gap_none_when_insufficient_bars() -> None:
    assert tp._sma_gap([1.0, 2.0, 3.0], window=50) is None


def test_sma_gap_computes_distance_to_average() -> None:
    closes = [10.0] * 49 + [20.0]  # 50 bars, last far above the mean
    sma = statistics.fmean(closes[-50:])
    assert tp._sma_gap(closes, 50) == 20.0 / sma - 1


def test_vol_annualized_none_when_insufficient() -> None:
    assert tp._vol_annualized([1.0] * 10, window=20) is None


def test_vol_annualized_zero_for_flat_series() -> None:
    # 21 identical closes → all returns 0 → volatility 0.
    assert tp._vol_annualized([100.0] * 21, window=20) == 0.0


def test_vol_annualized_positive_for_moving_series() -> None:
    closes = [100.0 + (i % 2) for i in range(40)]  # oscillates → nonzero vol
    vol = tp._vol_annualized(closes, 20)
    assert vol is not None and vol > 0


def test_compute_technical_position_insufficient_history_returns_nones() -> None:
    # A tiny series: only short metrics are None; nothing should raise.
    pos = tp.TechnicalPosition(
        ticker="X",
        name="X",
        last_close=10.0,
        bars=5,
        pct_vs_sma50=None,
        pct_vs_sma200=None,
        range_52w_low=None,
        range_52w_high=None,
        pct_in_52w_range=None,
        vol_20d_annualized=None,
    )
    assert pos.pct_vs_sma50 is None and pos.bars == 5


def test_compute_technical_position_normalizes_known_collision_ticker(
    db_session: Session,
) -> None:
    """issue #204 PR #253 review: capture writes a raw un-padded HK ticker's
    closes under the normalized '0700.HK' key, so a query for the raw
    holding ticker '700.HK' always found zero bars — §4.4 stayed
    permanently empty for this holding even after capture/valuation were
    fixed."""
    db_session.add_all(
        [
            PriceSnapshot(
                ticker="0700.HK",
                market="HK",
                session_node="close",
                trade_date=date(2026, 6, 1),
                close=Decimal("58.00"),
            ),
            PriceSnapshot(
                ticker="0700.HK",
                market="HK",
                session_node="close",
                trade_date=date(2026, 6, 2),
                close=Decimal("59.00"),
            ),
        ]
    )
    db_session.flush()

    pos = tp.compute_technical_position(db_session, "700.HK", "Tencent", date(2026, 6, 3))

    assert pos.bars == 2
    assert pos.last_close == 59.00
    assert pos.ticker == "0700.HK"


def test_compute_technical_positions_normalizes_known_collision_ticker(
    db_session: Session,
) -> None:
    db_session.add(
        PriceSnapshot(
            ticker="0700.HK",
            market="HK",
            session_node="close",
            trade_date=date(2026, 6, 1),
            close=Decimal("58.00"),
        )
    )
    db_session.flush()

    positions = tp.compute_technical_positions(
        db_session,
        [{"ticker": "700.HK", "name": "Tencent"}],
        date(2026, 6, 3),
    )

    assert len(positions) == 1
    assert positions[0].bars == 1
    assert positions[0].ticker == "0700.HK"


def test_829_position_ignores_future_closes(db_session: Session) -> None:
    db_session.add_all(
        [
            PriceSnapshot(
                ticker="FUTURE829",
                market="US",
                session_node="close",
                trade_date=date(2026, 1, day),
                close=Decimal(day),
            )
            for day in (1, 2)
        ]
    )
    db_session.flush()
    pos = tp.compute_technical_position(db_session, "FUTURE829", "Future", date(2026, 1, 1))
    assert pos.last_close == 1.0
    assert pos.bars == 1


def test_829_series_matches_same_day_position_with_sparse_history(db_session: Session) -> None:
    from datetime import timedelta

    end = date(2026, 10, 26)
    start = end - timedelta(days=91)
    days = [end - timedelta(days=9 * (49 - i)) for i in range(50)]
    db_session.add_all(
        [
            PriceSnapshot(
                ticker="SPARSE829",
                market="US",
                session_node="close",
                trade_date=day,
                close=Decimal(100 + i),
            )
            for i, day in enumerate(days)
        ]
    )
    db_session.flush()
    points = tp.compute_technical_series(db_session, "SPARSE829", "Sparse", start, end)
    assert [point.date for point in points] == [day for day in days if start <= day <= end]
    last = tp.compute_technical_position(db_session, "SPARSE829", "Sparse", end)
    assert points[-1].pct_vs_sma50 == last.pct_vs_sma50 is None
    assert points[-1].close == last.last_close
    assert points[-1].pct_vs_sma200 == last.pct_vs_sma200
    assert points[-1].pct_in_52w_range == last.pct_in_52w_range
    assert points[-1].vol_20d_annualized == last.vol_20d_annualized


def test_829_historical_holdings_uses_prior_snapshot(db_session: Session) -> None:
    from uuid import uuid4

    from app.models.portfolio_value_snapshot import PortfolioValueSnapshot
    from app.tests.conftest import seed_user

    uid = uuid4()
    seed_user(db_session, uid)
    db_session.add_all(
        [
            PortfolioValueSnapshot(
                user_id=uid,
                snapshot_date=date(2026, 1, day),
                ticker=ticker,
                currency="USD",
                base_currency="USD",
            )
            for day, ticker in ((1, "AAPL"), (3, "MSFT"))
        ]
    )
    db_session.flush()
    assert tp.technical_holdings(db_session, uid, date(2026, 1, 2)) == (
        date(2026, 1, 1),
        [{"ticker": "AAPL", "name": "AAPL"}],
    )
    assert tp.technical_holdings(db_session, uid, date(2025, 12, 31)) == (None, [])
