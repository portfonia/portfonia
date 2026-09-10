"""Tests for the one-off USDCNH-only historical gap-fill (issue #406)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import patch

import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models.fx_rate import FxRate
from app.scripts import backfill_usdcnh_history as script


def _fake_settings(api_key: str | None) -> Settings:
    settings = Settings.model_construct()
    settings.TWELVEDATA_API_KEY = SecretStr(api_key) if api_key is not None else None
    return settings


def _history(*points: tuple[date, str]) -> list[tuple[date, Decimal]]:
    return [(d, Decimal(rate)) for d, rate in points]


def test_computes_end_date_from_earliest_existing_row(db_session: Session) -> None:
    db_session.add(
        FxRate(pair="USDCNH", rate=Decimal("6.70"), rate_date=date(2026, 8, 5), source="yfinance")
    )
    db_session.flush()

    with (
        patch.object(script, "fetch_daily_history", return_value=[]) as mock_fetch,
        patch.object(script, "get_settings", return_value=_fake_settings("k")),
    ):
        script.backfill_usdcnh_history(db_session, years=5, apply_changes=False)

    args = mock_fetch.call_args.args
    assert args[0] == "USD/CNH"
    assert args[2] == date(2026, 8, 4)  # end_date = earliest existing row - 1 day


def test_end_date_defaults_to_today_when_no_existing_rows(db_session: Session) -> None:
    with (
        patch.object(script, "fetch_daily_history", return_value=[]) as mock_fetch,
        patch.object(script, "get_settings", return_value=_fake_settings("k")),
    ):
        script.backfill_usdcnh_history(db_session, years=5, apply_changes=False)

    args = mock_fetch.call_args.args
    assert args[2] == date.today()


def test_dry_run_does_not_write_rows(db_session: Session) -> None:
    fake = _history((date(2021, 9, 9), "6.45"), (date(2021, 9, 10), "6.46"))
    with (
        patch.object(script, "fetch_daily_history", return_value=fake),
        patch.object(script, "get_settings", return_value=_fake_settings("k")),
    ):
        written = script.backfill_usdcnh_history(db_session, years=5, apply_changes=False)

    assert written == 2
    count = db_session.execute(select(FxRate)).scalars().all()
    assert count == []


def test_apply_writes_rows_tagged_twelvedata(db_session: Session) -> None:
    fake = _history((date(2021, 9, 9), "6.45"), (date(2021, 9, 10), "6.46"))
    with (
        patch.object(script, "fetch_daily_history", return_value=fake),
        patch.object(script, "get_settings", return_value=_fake_settings("k")),
    ):
        written = script.backfill_usdcnh_history(db_session, years=5, apply_changes=True)

    assert written == 2
    rows = db_session.execute(select(FxRate).order_by(FxRate.rate_date)).scalars().all()
    assert [r.rate_date for r in rows] == [date(2021, 9, 9), date(2021, 9, 10)]
    assert all(r.source == "twelvedata" for r in rows)
    assert all(r.pair == "USDCNH" for r in rows)


def test_apply_is_idempotent(db_session: Session) -> None:
    fake = _history((date(2021, 9, 9), "6.45"))
    with (
        patch.object(script, "fetch_daily_history", return_value=fake),
        patch.object(script, "get_settings", return_value=_fake_settings("k")),
    ):
        script.backfill_usdcnh_history(db_session, years=5, apply_changes=True)
        script.backfill_usdcnh_history(db_session, years=5, apply_changes=True)

    rows = db_session.execute(select(FxRate)).scalars().all()
    assert len(rows) == 1


def test_never_overlaps_existing_yfinance_rows_even_if_source_returns_extra(
    db_session: Session,
) -> None:
    """Defensive filter: even if Twelve Data's API ever returned a point on
    or after the existing coverage's start date, this script must not write
    it -- the daily capture path owns that end of the series exclusively."""
    db_session.add(
        FxRate(pair="USDCNH", rate=Decimal("6.70"), rate_date=date(2026, 8, 5), source="yfinance")
    )
    db_session.flush()
    fake = _history(
        (date(2026, 8, 3), "6.71"),
        (date(2026, 8, 4), "6.72"),
        (date(2026, 8, 5), "6.99"),  # would collide with the existing yfinance row
    )
    with (
        patch.object(script, "fetch_daily_history", return_value=fake),
        patch.object(script, "get_settings", return_value=_fake_settings("k")),
    ):
        written = script.backfill_usdcnh_history(db_session, years=5, apply_changes=True)

    assert written == 2
    live = db_session.execute(
        select(FxRate).where(FxRate.rate_date == date(2026, 8, 5))
    ).scalar_one()
    assert live.source == "yfinance"
    assert live.rate == Decimal("6.70")


def test_missing_api_key_raises(db_session: Session) -> None:
    with (
        patch.object(script, "get_settings", return_value=_fake_settings(None)),
        pytest.raises(SystemExit),
    ):
        script.backfill_usdcnh_history(db_session, years=5, apply_changes=False)
