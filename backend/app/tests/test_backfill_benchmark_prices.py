"""Test for the benchmark-prices one-off history backfill script."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import patch

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.benchmark_price import BenchmarkPrice
from app.services import benchmark_prices
from app.services.benchmark_prices import backfill_benchmark_prices


def test_backfill_benchmark_prices_writes_history(db_session: Session) -> None:
    fake = {
        "^GSPC": [(date(2021, 1, 4), Decimal("3700"))],
        "^DJI": [(date(2021, 1, 4), Decimal("30000"))],
        "^IXIC": [(date(2021, 1, 4), Decimal("12800"))],
    }
    with patch.object(benchmark_prices, "_fetch_index_closes", return_value=fake) as mock_fetch:
        written = backfill_benchmark_prices(db_session, years=5)
    assert written == 3
    # Requested a ~5-year lookback window, not the daily task's short one.
    assert mock_fetch.call_args.kwargs["lookback_days"] == 365 * 5

    rows = db_session.execute(select(BenchmarkPrice)).scalars().all()
    assert len(rows) == 3
