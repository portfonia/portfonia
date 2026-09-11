"""FX catch-up: retry + Twelve Data fallback for a prior day's missing rate
(issue #426).

Runs at 00:05 ET the day after the target trading day (see the
`capture-fx-catchup-daily` beat entry) — well past the vendor publish-lag
window that can make `capture_fx_task`'s 17:15 ET fetch land a bar dated the
prior day instead of the one it ran for.
"""

from __future__ import annotations

from collections.abc import Generator
from datetime import date
from decimal import Decimal
from unittest.mock import patch

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.fx_rate import FxRate
from app.services.fx_fetcher import _PAIRS, FxFetchResult, fx_catchup

_TARGET = date(2026, 9, 9)


@pytest.fixture
def production_env() -> Generator[None, None, None]:
    """Same pattern as test_fx_fetcher.py's fixture of the same name: FX ops
    alerts are gated on APP_ENV=="production" so a local dev run never sends
    a real alert."""
    get_settings.cache_clear()
    with patch.dict("os.environ", {"APP_ENV": "production"}):
        get_settings.cache_clear()
        try:
            yield
        finally:
            get_settings.cache_clear()


def _seed_pairs(session: Session, d: date, pairs: list[str]) -> None:
    for pair in pairs:
        session.add(FxRate(pair=pair, rate=Decimal("7"), rate_date=d, source="yfinance"))
    session.flush()


def test_noop_when_target_date_already_complete(db_session: Session) -> None:
    _seed_pairs(db_session, _TARGET, list(_PAIRS))
    with patch("app.services.fx_fetcher.update_fx_rates") as mock_retry:
        result = fx_catchup(db_session, _TARGET)
    mock_retry.assert_not_called()
    assert result.initially_missing == []
    assert result.recovered_via_retry == []
    assert result.recovered_via_fallback == []
    assert result.still_missing == []


def test_recovers_via_retry_alone(db_session: Session) -> None:
    def fake_retry(session: Session) -> FxFetchResult:
        _seed_pairs(session, _TARGET, list(_PAIRS))
        return FxFetchResult(upserted=len(_PAIRS))

    with patch("app.services.fx_fetcher.update_fx_rates", side_effect=fake_retry) as mock_retry:
        result = fx_catchup(db_session, _TARGET)
    mock_retry.assert_called_once()
    assert set(result.initially_missing) == set(_PAIRS)
    assert set(result.recovered_via_retry) == set(_PAIRS)
    assert result.recovered_via_fallback == []
    assert result.still_missing == []


def test_falls_back_to_twelvedata_when_retry_does_not_recover_a_pair(
    db_session: Session,
) -> None:
    def fake_retry(session: Session) -> FxFetchResult:
        # Retry recovers every pair except USDCNY (still stuck on yesterday's bar).
        _seed_pairs(session, _TARGET, [p for p in _PAIRS if p != "USDCNY"])
        return FxFetchResult(upserted=len(_PAIRS) - 1, failed=[])

    with (
        patch("app.services.fx_fetcher.update_fx_rates", side_effect=fake_retry),
        patch("app.services.fx_fetcher._twelvedata_key", return_value="fake-key"),
        patch(
            "app.services.fx_fetcher.fetch_daily_history",
            return_value=[(_TARGET, Decimal("7.05"))],
        ) as mock_fetch,
    ):
        result = fx_catchup(db_session, _TARGET)

    mock_fetch.assert_called_once_with("USD/CNY", _TARGET, _TARGET, "fake-key")
    assert result.recovered_via_fallback == ["USDCNY"]
    assert result.still_missing == []
    row = db_session.execute(
        select(FxRate).where(FxRate.pair == "USDCNY", FxRate.rate_date == _TARGET)
    ).scalar_one()
    assert row.rate == Decimal("7.05")
    assert row.source == "twelvedata"


def test_still_missing_after_fallback_also_fails_sends_one_alert(
    db_session: Session, production_env: None
) -> None:
    with (
        patch("app.services.fx_fetcher.update_fx_rates"),
        patch("app.services.fx_fetcher._twelvedata_key", return_value="fake-key"),
        patch(
            "app.services.fx_fetcher.fetch_daily_history",
            side_effect=ValueError("twelvedata: no data"),
        ),
        patch("app.services.fx_fetcher.send_ops_alert", return_value=True) as mock_alert,
    ):
        result = fx_catchup(db_session, _TARGET)

    assert set(result.still_missing) == set(_PAIRS)
    mock_alert.assert_called_once()
    assert _TARGET.isoformat() in mock_alert.call_args.kwargs["body"]


def test_no_twelvedata_key_skips_fallback_without_raising(db_session: Session) -> None:
    with (
        patch("app.services.fx_fetcher.update_fx_rates"),
        patch("app.services.fx_fetcher._twelvedata_key", return_value=None),
        patch("app.services.fx_fetcher.fetch_daily_history") as mock_fetch,
    ):
        result = fx_catchup(db_session, _TARGET)

    mock_fetch.assert_not_called()
    assert set(result.still_missing) == set(_PAIRS)
