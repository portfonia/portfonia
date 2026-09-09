"""Tests for benchmark index price capture (issue #360 Phase 1, D9)."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.benchmark_price import BenchmarkPrice
from app.services import benchmark_prices


def _fake_closes() -> dict[str, list[tuple[date, Decimal]]]:
    return {
        "^GSPC": [(date(2026, 9, 3), Decimal("5500.1234")), (date(2026, 9, 4), Decimal("5510.5"))],
        "^DJI": [(date(2026, 9, 3), Decimal("40000")), (date(2026, 9, 4), Decimal("40100"))],
        "^IXIC": [(date(2026, 9, 3), Decimal("17000")), (date(2026, 9, 4), Decimal("17100"))],
        "000300.SS": [(date(2026, 9, 3), Decimal("4500")), (date(2026, 9, 4), Decimal("4510"))],
    }


def test_capture_benchmark_index_prices_writes_catalog_indexes(db_session: Session) -> None:
    with patch.object(benchmark_prices, "_fetch_index_closes", return_value=_fake_closes()):
        written = benchmark_prices.capture_benchmark_index_prices(db_session)
    assert written == 8

    rows = db_session.execute(select(BenchmarkPrice)).scalars().all()
    codes = {row.index_code for row in rows}
    assert codes == {"sp500", "dow30", "nasdaq", "csi300"}
    by_code = {row.index_code: row.currency for row in rows}
    assert by_code["sp500"] == "USD"
    assert by_code["csi300"] == "CNY"


def test_nasdaq_index_is_composite_not_ndx() -> None:
    assert benchmark_prices.INDEX_YF_TICKERS["nasdaq"] == "^IXIC"
    assert benchmark_prices.INDEX_YF_TICKERS["nasdaq"] != "^NDX"


def test_csi300_is_sse_price_index_in_cny() -> None:
    assert benchmark_prices.INDEX_YF_TICKERS["csi300"] == "000300.SS"
    assert benchmark_prices.INDEX_CURRENCIES["csi300"] == "CNY"
    assert set(benchmark_prices.INDEX_CURRENCIES) == set(benchmark_prices.INDEX_YF_TICKERS)


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


def test_db_rejects_unknown_index_code(db_session: Session) -> None:
    db_session.add(
        BenchmarkPrice(
            index_code="a50",
            price_date=date(2026, 9, 3),
            close_price=Decimal("1"),
            currency="CNY",
        )
    )
    with pytest.raises(IntegrityError, match="ck_benchmark_prices_index_code"):
        db_session.commit()


# Benchmark rows bind 4 parameters each. PostgreSQL/psycopg hard-cap a
# single query at 65535 parameters, so 16384+ rows in one INSERT overflows
# (same class of bug as issue #194's price_capture.py precedent).
# 17000 rows = 68000 params, past that cap with margin.
_PARAM_OVERFLOW_ROW_COUNT = 17000


def test_upsert_chunks_past_postgres_parameter_limit(db_session: Session) -> None:
    """A batch that would exceed psycopg's 65535-param cap must still write.

    Mirrors test_price_capture.py::test_upsert_chunks_past_postgres_parameter_limit
    (issue #194) — benchmark_prices.py's _upsert never picked up that fix.
    """
    start = date(2000, 1, 1)
    rows: list[dict[str, object]] = [
        {
            "index_code": "sp500",
            "price_date": start + timedelta(days=i),
            "close_price": Decimal("1.0"),
            "currency": "USD",
        }
        for i in range(_PARAM_OVERFLOW_ROW_COUNT)
    ]
    # Rows bind one param per dict key. Lock the chunk math the source
    # comment states, derived from this row shape not a frozen 10.
    assert benchmark_prices._UPSERT_CHUNK_SIZE * len(rows[0]) <= 65_535

    written = benchmark_prices._upsert(db_session, rows)

    assert written == _PARAM_OVERFLOW_ROW_COUNT
    count = db_session.execute(
        select(func.count()).select_from(BenchmarkPrice).where(BenchmarkPrice.index_code == "sp500")
    ).scalar_one()
    assert count == _PARAM_OVERFLOW_ROW_COUNT


def test_historical_benchmark_price_finds_latest_at_or_before(db_session: Session) -> None:
    with patch.object(benchmark_prices, "_fetch_index_closes", return_value=_fake_closes()):
        benchmark_prices.capture_benchmark_index_prices(db_session)

    result = benchmark_prices.historical_benchmark_price(db_session, "sp500", date(2026, 9, 5))
    assert result is not None
    price, price_date = result
    assert price_date == date(2026, 9, 4)
    assert price == Decimal("5510.5")
