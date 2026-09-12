"""Test for the benchmark-prices one-off history backfill script."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import patch

import pandas as pd
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
        "000300.SS": [(date(2021, 1, 4), Decimal("5200"))],
    }
    with (
        patch.object(benchmark_prices, "_fetch_index_closes", return_value=fake) as mock_fetch,
        patch.object(benchmark_prices, "_fetch_tencent_csi300_closes", return_value={}),
    ):
        written = backfill_benchmark_prices(db_session, years=5)
    assert written == 4
    # Review 5124107298 finding 2: a multi-year backfill must use yfinance's
    # `Ny` period form, not an arbitrary `Nd` day count.
    assert mock_fetch.call_args.kwargs["period"] == "5y"

    rows = db_session.execute(select(BenchmarkPrice)).scalars().all()
    assert len(rows) == 4
    csi = next(row for row in rows if row.index_code == "csi300")
    assert csi.currency == "CNY"
    assert csi.close_price == Decimal("5200")


def test_capture_benchmark_index_prices_uses_short_day_period(db_session: Session) -> None:
    """The daily catch-up path keeps the short `Nd` form — only the
    multi-year backfill needed to change (finding 2)."""
    with (
        patch.object(benchmark_prices, "_fetch_index_closes", return_value={}) as mock_fetch,
        patch.object(benchmark_prices, "_fetch_tencent_csi300_closes", return_value={}),
    ):
        benchmark_prices.capture_benchmark_index_prices(db_session, lookback_days=7)
    assert mock_fetch.call_args.kwargs["period"] == "7d"


def test_fetch_index_closes_period_reaches_yfinance_download_verbatim() -> None:
    """Narrow, real (unmocked-at-our-boundary) check that the exact period
    string built by a caller is what actually reaches `yf.download` — the
    mocked tests above never exercise this boundary, which is exactly how
    review 5124107298 finding 2 slipped through the first time."""
    with patch("app.services.benchmark_prices.yf.download", return_value=pd.DataFrame()) as mock_dl:
        benchmark_prices._fetch_index_closes(["^GSPC"], "5y")
    assert mock_dl.call_args.kwargs["period"] == "5y"


def test_fetch_index_closes_retries_on_exception_via_shared_backoff() -> None:
    """issue #132/§6.8: benchmark_prices had zero retry logic before this
    change — confirm it now goes through the same backoff as price/FX."""
    call_count = {"n": 0}

    def fake_download(**kwargs: object) -> pd.DataFrame:
        call_count["n"] += 1
        if call_count["n"] < 2:
            raise OSError("network error")
        return pd.DataFrame()

    with (
        patch("app.services.benchmark_prices.yf.download", side_effect=fake_download),
        patch("app.services._yfinance.time.sleep") as mock_sleep,
    ):
        benchmark_prices._fetch_index_closes(["^GSPC"], "5y")

    assert call_count["n"] == 2
    mock_sleep.assert_called_once_with(5.0)


def test_fetch_index_closes_still_fails_open_after_exhausting_retries() -> None:
    """Existing contract (never raise past this function) must survive
    the retry wrapper — final exception still becomes an empty dict."""

    def always_fails(**kwargs: object) -> pd.DataFrame:
        raise OSError("network error")

    with (
        patch("app.services.benchmark_prices.yf.download", side_effect=always_fails),
        patch("app.services._yfinance.time.sleep"),
    ):
        result = benchmark_prices._fetch_index_closes(["^GSPC"], "5y")

    assert result == {}
