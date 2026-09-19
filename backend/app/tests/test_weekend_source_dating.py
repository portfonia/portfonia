"""Issue #487 acceptance 4: benchmark / fund-NAV capture on a date with no
new source bar must not write a row dated that call date.

Fund-NAV dating was an implementation-time check: `_nav_rows` keys
`trade_date` off `point.nav_date`, not fetch-time (`captured_at` is
metadata only). That is why `capture-fund-navs-daily` is included in the
every-day Beat widening.

FX used to be covered by this same acceptance criterion (a daily-bar
fetch, source-dated), but issue #519 switched it to a live-quote fetch —
there is no source bar to defer to anymore, so a Saturday fetch
legitimately writes a Saturday-dated row (see
`test_fx_capture_on_saturday_dates_the_row_saturday` below, which replaces
the old "does not date a row Saturday" FX case).
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import patch

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.timezones import CST
from app.models.benchmark_price import BenchmarkPrice
from app.models.fx_rate import FxRate
from app.models.holding import Holding
from app.models.price_snapshot import PriceSnapshot
from app.services import benchmark_prices, fx_fetcher
from app.services.capture_results import NavHistoryOutcome, NavPoint
from app.services.china_session_calendar import freeze_capture_window
from app.services.fx_fetcher import _PAIRS
from app.services.price_capture import capture_fund_navs_attempt
from app.tests.conftest import seed_user

_FRI = date(2026, 9, 11)
_SAT = date(2026, 9, 12)
_FRI_AS_OF = datetime(2026, 9, 11, 21, 15, tzinfo=UTC)
_USER = uuid.UUID("00000000-0000-0000-0000-0000000000d4")


def test_fx_capture_on_saturday_dates_the_row_saturday(db_session: Session) -> None:
    """Issue #519: FX is a live-quote fetch now, not a daily-bar fetch — a
    Saturday capture legitimately writes a Saturday-dated row using
    whatever live quote comes back (there is no source bar's own date to
    defer to instead), unlike the benchmark/fund-NAV tests below."""
    for pair in _PAIRS:
        db_session.add(FxRate(pair=pair, rate=Decimal("7"), rate_date=_FRI, source="yfinance"))
    db_session.flush()
    saturday_as_of = datetime(2026, 9, 12, 20, 0, tzinfo=UTC)
    saturday_points = {yf_ticker: (7.18, saturday_as_of) for yf_ticker in _PAIRS.values()}
    with patch.object(fx_fetcher, "fetch_live_rate", return_value=saturday_points):
        fx_fetcher.update_fx_rates(db_session)
    saturday_rows = db_session.execute(
        select(func.count()).select_from(FxRate).where(FxRate.rate_date == _SAT)
    ).scalar_one()
    assert saturday_rows == len(_PAIRS)


def test_benchmark_capture_on_saturday_does_not_date_a_row_saturday(
    db_session: Session,
) -> None:
    friday_closes = {
        yf_ticker: [(_FRI, Decimal("100"))]
        for yf_ticker in benchmark_prices.INDEX_YF_TICKERS.values()
    }
    with (
        patch.object(benchmark_prices, "_today", return_value=_SAT),
        patch.object(benchmark_prices, "_fetch_index_closes", return_value=friday_closes),
        patch.object(benchmark_prices, "_fetch_tencent_csi300_closes", return_value={}),
    ):
        benchmark_prices.capture_benchmark_index_prices(db_session)
        before = db_session.execute(select(func.count()).select_from(BenchmarkPrice)).scalar_one()
        benchmark_prices.capture_benchmark_index_prices(db_session)
    after = db_session.execute(select(func.count()).select_from(BenchmarkPrice)).scalar_one()
    assert after == before
    saturday_rows = db_session.execute(
        select(func.count()).select_from(BenchmarkPrice).where(BenchmarkPrice.price_date == _SAT)
    ).scalar_one()
    assert saturday_rows == 0
    assert db_session.execute(
        select(func.count()).select_from(BenchmarkPrice).where(BenchmarkPrice.price_date == _FRI)
    ).scalar_one() == len(benchmark_prices.INDEX_YF_TICKERS)


def test_fund_nav_capture_on_saturday_dates_by_source_nav_not_fetch_day(
    db_session: Session,
) -> None:
    seed_user(db_session, _USER)
    db_session.add(
        Holding(
            user_id=_USER,
            name="fund-008142",
            fund_code="008142",
            pricing_mode="auto",
            currency="CNY",
            market="A-Share",
            asset_class="EQUITY_CN",
            asset_type="fund",
        )
    )
    db_session.add(
        PriceSnapshot(
            ticker="008142",
            market="A-Share",
            session_node="close",
            trade_date=_FRI,
            close=Decimal("2.1766"),
            source="eastmoney",
        )
    )
    db_session.flush()
    before = db_session.execute(select(func.count()).select_from(PriceSnapshot)).scalar_one()
    window = freeze_capture_window(
        datetime(2026, 9, 12, 20, 0, tzinfo=CST), lookback_days=30, max_lag_sessions=2
    )
    friday_nav = NavHistoryOutcome(
        points=(NavPoint(_FRI, Decimal("2.1766"), "eastmoney"),),
    )
    with patch(
        "app.services.price_capture.fetch_nav_history_outcome",
        return_value=friday_nav,
    ):
        capture_fund_navs_attempt(db_session, window=window, lookback_days=30, allow_fallback=False)
    after = db_session.execute(select(func.count()).select_from(PriceSnapshot)).scalar_one()
    assert after == before
    saturday_rows = db_session.execute(
        select(func.count()).select_from(PriceSnapshot).where(PriceSnapshot.trade_date == _SAT)
    ).scalar_one()
    assert saturday_rows == 0
    row = db_session.execute(
        select(PriceSnapshot).where(
            PriceSnapshot.ticker == "008142", PriceSnapshot.trade_date == _FRI
        )
    ).scalar_one()
    assert row.trade_date == _FRI
