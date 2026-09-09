"""Tests for the one-off multi-year fx_rates seed (issue #398)."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.fx_rate import FxRate
from app.services import fx_fetcher
from app.services.fx_fetcher import _PAIRS, backfill_fx_rates


def _history_one_day() -> dict[str, list[tuple[date, Decimal]]]:
    return {pair: [(date(2021, 1, 4), Decimal("1.25"))] for pair in _PAIRS}


def _history_real_volume() -> dict[str, list[tuple[date, Decimal]]]:
    """~5 years of daily closes for every pair -- real production data
    volume (issue #402). 14 pairs x ~1255 days x 5 columns/row ~= 87,850
    params, over PostgreSQL's 65,535-per-statement limit if
    `_upsert_fx_history` ever regresses to a single unbatched INSERT."""
    days_per_pair = 1255
    return {
        pair: [
            (date(2020, 1, 1) + timedelta(days=i), Decimal("1.25")) for i in range(days_per_pair)
        ]
        for pair in _PAIRS
    }


def test_backfill_fx_rates_requests_every_pair_with_ny_period(db_session: Session) -> None:
    with patch.object(
        fx_fetcher, "_fetch_rate_history", return_value=_history_one_day()
    ) as mock_fetch:
        written = backfill_fx_rates(db_session, years=5)

    assert written == len(_PAIRS)
    assert mock_fetch.call_args.kwargs["period"] == "5y"
    requested = mock_fetch.call_args.kwargs.get("pairs") or mock_fetch.call_args.args[0]
    assert set(requested) == set(_PAIRS)


def test_backfill_fx_rates_years_override_uses_ny_not_nd(db_session: Session) -> None:
    with patch.object(fx_fetcher, "_fetch_rate_history", return_value={}) as mock_fetch:
        backfill_fx_rates(db_session, years=3)
    assert mock_fetch.call_args.kwargs["period"] == "3y"


def test_backfill_fx_rates_upsert_is_idempotent(db_session: Session) -> None:
    fake = _history_one_day()
    with patch.object(fx_fetcher, "_fetch_rate_history", return_value=fake):
        first = backfill_fx_rates(db_session, years=5)
        second = backfill_fx_rates(db_session, years=5)

    assert first == len(_PAIRS)
    assert second == len(_PAIRS)
    count = db_session.execute(select(func.count()).select_from(FxRate)).scalar_one()
    assert count == len(_PAIRS)


def test_backfill_fx_rates_does_not_delete_existing_rows(db_session: Session) -> None:
    live_date = date(2020, 6, 1)
    db_session.add(
        FxRate(pair="USDCNY", rate=Decimal("7.01"), rate_date=live_date, source="yfinance")
    )
    db_session.flush()

    with patch.object(fx_fetcher, "_fetch_rate_history", return_value=_history_one_day()):
        backfill_fx_rates(db_session, years=5)

    live = db_session.execute(
        select(FxRate).where(FxRate.pair == "USDCNY", FxRate.rate_date == live_date)
    ).scalar_one()
    assert live.rate == Decimal("7.01")
    seeded = db_session.execute(
        select(FxRate).where(FxRate.pair == "USDCNY", FxRate.rate_date == date(2021, 1, 4))
    ).scalar_one()
    assert seeded.rate == Decimal("1.25")


def test_backfill_fx_rates_handles_real_production_data_volume(db_session: Session) -> None:
    """issue #402: a single unbatched INSERT over real 5-year/14-pair
    volume exceeds PostgreSQL's 65,535-bound-parameter limit and raises
    psycopg.OperationalError. Reproduced live in production running the
    script's own documented command; this pins the fix at the real
    parameter-count scale rather than the 1-row-per-pair fixture every
    other test in this file uses."""
    fake = _history_real_volume()
    expected_rows = sum(len(points) for points in fake.values())

    with patch.object(fx_fetcher, "_fetch_rate_history", return_value=fake):
        written = backfill_fx_rates(db_session, years=5)

    assert written == expected_rows
    count = db_session.execute(select(func.count()).select_from(FxRate)).scalar_one()
    assert count == expected_rows


def test_fetch_rate_history_period_reaches_yfinance_download_verbatim() -> None:
    with patch("app.services.fx_fetcher.yf.download", return_value=pd.DataFrame()) as mock_dl:
        fx_fetcher._fetch_rate_history(_PAIRS, period="5y")
    assert mock_dl.call_args.kwargs["period"] == "5y"
    tickers = mock_dl.call_args.kwargs["tickers"]
    for yf_ticker in _PAIRS.values():
        assert yf_ticker in tickers
