"""Tencent kline fallback for csi300 Yahoo gaps (issue #407)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import patch

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.benchmark_price import BenchmarkPrice
from app.services import benchmark_prices

_CSI300 = "000300.SS"


def test_csi300_tencent_symbol_is_sh000300() -> None:
    assert benchmark_prices.INDEX_YF_TICKERS["csi300"] == _CSI300
    assert benchmark_prices.CSI300_TENCENT_SYMBOL == "sh000300"


def test_parse_tencent_day_bars_uses_close_not_high() -> None:
    payload = {
        "code": 0,
        "data": {
            "sh000300": {
                "day": [["2026-09-09", "4578.51", "4572.60", "4581.90", "4548.36"]],
            }
        },
    }
    parsed = benchmark_prices._parse_tencent_csi300_day_bars(payload)
    assert parsed == {date(2026, 9, 9): Decimal("4572.60")}


def test_fallback_fills_missing_weekday_when_anchor_agrees() -> None:
    yahoo = [
        (date(2026, 9, 3), Decimal("4500.00")),
        (date(2026, 9, 7), Decimal("4520.00")),
    ]
    tencent = {
        date(2026, 9, 3): Decimal("4500.00"),
        date(2026, 9, 4): Decimal("4508.12"),
        date(2026, 9, 7): Decimal("4520.00"),
    }
    with patch.object(benchmark_prices, "_fetch_tencent_csi300_closes", return_value=tencent):
        filled = benchmark_prices._csi300_fallback_points(yahoo, date(2026, 9, 3), date(2026, 9, 7))
    assert filled == [(date(2026, 9, 4), Decimal("4508.12"))]


def test_fallback_rejects_without_yahoo_anchor() -> None:
    with patch.object(benchmark_prices, "_fetch_tencent_csi300_closes") as fetch:
        filled = benchmark_prices._csi300_fallback_points([], date(2026, 9, 3), date(2026, 9, 7))
    assert filled == []
    fetch.assert_not_called()


def test_fallback_rejects_anchor_disagreement() -> None:
    yahoo = [
        (date(2026, 9, 3), Decimal("4500.00")),
        (date(2026, 9, 7), Decimal("4520.00")),
    ]
    tencent = {
        date(2026, 9, 3): Decimal("4490.00"),
        date(2026, 9, 4): Decimal("4508.12"),
        date(2026, 9, 7): Decimal("4520.00"),
    }
    with patch.object(benchmark_prices, "_fetch_tencent_csi300_closes", return_value=tencent):
        filled = benchmark_prices._csi300_fallback_points(yahoo, date(2026, 9, 3), date(2026, 9, 7))
    assert filled == []


def test_sp500_gaps_do_not_invoke_tencent(db_session: Session) -> None:
    fake = {
        "^GSPC": [(date(2026, 9, 3), Decimal("5500")), (date(2026, 9, 9), Decimal("5600"))],
        "^DJI": [(date(2026, 9, 3), Decimal("40000")), (date(2026, 9, 9), Decimal("40100"))],
        "^IXIC": [(date(2026, 9, 3), Decimal("17000")), (date(2026, 9, 9), Decimal("17100"))],
        _CSI300: [
            (date(2026, 9, 3), Decimal("4500")),
            (date(2026, 9, 4), Decimal("4510")),
            (date(2026, 9, 7), Decimal("4520")),
            (date(2026, 9, 8), Decimal("4530")),
            (date(2026, 9, 9), Decimal("4540")),
        ],
    }
    with (
        patch.object(benchmark_prices, "_fetch_index_closes", return_value=fake),
        patch.object(benchmark_prices, "_today", return_value=date(2026, 9, 9)),
        patch.object(benchmark_prices, "_fetch_tencent_csi300_closes", return_value={}) as tencent,
    ):
        benchmark_prices.capture_benchmark_index_prices(db_session, lookback_days=7)
    tencent.assert_not_called()


def test_capture_writes_verified_tencent_rows_for_csi300_gap(db_session: Session) -> None:
    fake = {
        "^GSPC": [(date(2026, 9, 3), Decimal("5500")), (date(2026, 9, 9), Decimal("5600"))],
        "^DJI": [(date(2026, 9, 3), Decimal("40000")), (date(2026, 9, 9), Decimal("40100"))],
        "^IXIC": [(date(2026, 9, 3), Decimal("17000")), (date(2026, 9, 9), Decimal("17100"))],
        _CSI300: [
            (date(2026, 9, 3), Decimal("4500.00")),
            (date(2026, 9, 9), Decimal("4540.00")),
        ],
    }
    tencent_closes = {
        date(2026, 9, 3): Decimal("4500.00"),
        date(2026, 9, 4): Decimal("4508.12"),
        date(2026, 9, 7): Decimal("4516.00"),
        date(2026, 9, 8): Decimal("4522.00"),
        date(2026, 9, 9): Decimal("4540.00"),
    }
    with (
        patch.object(benchmark_prices, "_fetch_index_closes", return_value=fake),
        patch.object(benchmark_prices, "_today", return_value=date(2026, 9, 9)),
        patch.object(benchmark_prices, "_fetch_tencent_csi300_closes", return_value=tencent_closes),
    ):
        benchmark_prices.capture_benchmark_index_prices(db_session, lookback_days=7)

    csi_dates = {
        row.price_date: row.close_price
        for row in db_session.execute(
            select(BenchmarkPrice).where(BenchmarkPrice.index_code == "csi300")
        )
        .scalars()
        .all()
    }
    assert csi_dates[date(2026, 9, 4)] == Decimal("4508.12")
    assert set(csi_dates) == {
        date(2026, 9, 3),
        date(2026, 9, 4),
        date(2026, 9, 7),
        date(2026, 9, 8),
        date(2026, 9, 9),
    }


def test_capture_does_not_write_tencent_when_anchor_disagrees(db_session: Session) -> None:
    fake = {
        "^GSPC": [(date(2026, 9, 3), Decimal("5500"))],
        "^DJI": [(date(2026, 9, 3), Decimal("40000"))],
        "^IXIC": [(date(2026, 9, 3), Decimal("17000"))],
        _CSI300: [
            (date(2026, 9, 3), Decimal("4500.00")),
            (date(2026, 9, 9), Decimal("4540.00")),
        ],
    }
    tencent_closes = {
        date(2026, 9, 3): Decimal("4400.00"),
        date(2026, 9, 4): Decimal("4508.12"),
        date(2026, 9, 9): Decimal("4540.00"),
    }
    with (
        patch.object(benchmark_prices, "_fetch_index_closes", return_value=fake),
        patch.object(benchmark_prices, "_today", return_value=date(2026, 9, 9)),
        patch.object(benchmark_prices, "_fetch_tencent_csi300_closes", return_value=tencent_closes),
    ):
        benchmark_prices.capture_benchmark_index_prices(db_session, lookback_days=7)

    csi_dates = {
        row.price_date
        for row in db_session.execute(
            select(BenchmarkPrice).where(BenchmarkPrice.index_code == "csi300")
        )
        .scalars()
        .all()
    }
    assert csi_dates == {date(2026, 9, 3), date(2026, 9, 9)}


def test_live_tencent_kline_endpoint_returns_csi300_day_bars() -> None:
    closes = benchmark_prices._fetch_tencent_csi300_closes(date(2026, 7, 15), date(2026, 9, 9))
    assert date(2026, 7, 17) in closes
    assert date(2026, 7, 20) in closes
    assert benchmark_prices._csi300_closes_agree(closes[date(2026, 7, 17)], Decimal("4529.10"))
