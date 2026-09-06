"""Tests for benchmark index price capture (issue #360 Phase 1, D9)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import patch

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.benchmark_price import BenchmarkPrice
from app.services import benchmark_prices


def _fake_closes() -> dict[str, list[tuple[date, Decimal]]]:
    return {
        "^GSPC": [(date(2026, 9, 3), Decimal("5500.1234")), (date(2026, 9, 4), Decimal("5510.5"))],
        "^DJI": [(date(2026, 9, 3), Decimal("40000")), (date(2026, 9, 4), Decimal("40100"))],
        "^IXIC": [(date(2026, 9, 3), Decimal("17000")), (date(2026, 9, 4), Decimal("17100"))],
    }


def test_capture_benchmark_index_prices_writes_all_three_indexes(db_session: Session) -> None:
    with patch.object(benchmark_prices, "_fetch_index_closes", return_value=_fake_closes()):
        written = benchmark_prices.capture_benchmark_index_prices(db_session)
    assert written == 6

    codes = {row.index_code for row in db_session.execute(select(BenchmarkPrice)).scalars()}
    assert codes == {"sp500", "dow30", "nasdaq"}


def test_nasdaq_index_is_composite_not_ndx() -> None:
    assert benchmark_prices.INDEX_YF_TICKERS["nasdaq"] == "^IXIC"
    assert benchmark_prices.INDEX_YF_TICKERS["nasdaq"] != "^NDX"


def test_capture_is_idempotent_upsert(db_session: Session) -> None:
    with patch.object(benchmark_prices, "_fetch_index_closes", return_value=_fake_closes()):
        benchmark_prices.capture_benchmark_index_prices(db_session)
        benchmark_prices.capture_benchmark_index_prices(db_session)

    rows = (
        db_session.execute(select(BenchmarkPrice).where(BenchmarkPrice.index_code == "sp500"))
        .scalars()
        .all()
    )
    assert len(rows) == 2  # two distinct dates, not four


def test_historical_benchmark_price_finds_latest_at_or_before(db_session: Session) -> None:
    with patch.object(benchmark_prices, "_fetch_index_closes", return_value=_fake_closes()):
        benchmark_prices.capture_benchmark_index_prices(db_session)

    result = benchmark_prices.historical_benchmark_price(db_session, "sp500", date(2026, 9, 5))
    assert result is not None
    price, price_date = result
    assert price_date == date(2026, 9, 4)
    assert price == Decimal("5510.5")
