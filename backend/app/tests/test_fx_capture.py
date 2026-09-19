"""FX capture: live-quote fetch with a per-pair Twelve Data fallback
(issue #519, replaces #426's daily-bar catch-up).

Both scheduled attempts (16:00 ET day-session close, 20:00 ET evening-
session close) call the same `capture_fx_rates` — neither is a "catch-up"
of the other, each is a fresh live-quote fetch that either succeeds now
or falls back now.
"""

from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import call, patch

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.timezones import today_et
from app.models.fx_rate import FxRate
from app.services.fx_fetcher import _PAIRS, capture_fx_rates

_AS_OF = datetime(2026, 9, 18, 20, 0, tzinfo=UTC)


@pytest.fixture
def production_env() -> Generator[None, None, None]:
    get_settings.cache_clear()
    with patch.dict("os.environ", {"APP_ENV": "production"}):
        get_settings.cache_clear()
        try:
            yield
        finally:
            get_settings.cache_clear()


def test_all_pairs_recovered_via_yfinance_needs_no_fallback(db_session: Session) -> None:
    fake_points = {yf: (7.18, _AS_OF) for yf in _PAIRS.values()}
    with (
        patch("app.services.fx_fetcher.fetch_live_rate", return_value=fake_points),
        patch("app.services.fx_fetcher.time.sleep") as mock_sleep,
    ):
        result = capture_fx_rates(db_session)

    assert set(result.recovered_via_yfinance) == set(_PAIRS)
    assert result.recovered_via_fallback == []
    assert result.still_missing == []
    mock_sleep.assert_not_called()


def test_falls_back_to_twelvedata_for_pair_yfinance_missed(db_session: Session) -> None:
    fake_points = {yf: (7.18, _AS_OF) for pair, yf in _PAIRS.items() if pair != "USDCNY"}
    with (
        patch("app.services.fx_fetcher.fetch_live_rate", return_value=fake_points),
        patch("app.services.fx_fetcher._twelvedata_key", return_value="fake-key"),
        patch(
            "app.services.fx_fetcher.twelvedata_fetch_live_rate",
            return_value=Decimal("7.05"),
        ) as mock_td,
        patch("app.services.fx_fetcher.time.sleep"),
    ):
        result = capture_fx_rates(db_session)

    mock_td.assert_called_once_with("USD/CNY", "fake-key")
    assert result.recovered_via_fallback == ["USDCNY"]
    assert result.still_missing == []
    row = db_session.execute(
        select(FxRate).where(FxRate.pair == "USDCNY", FxRate.rate_date == today_et())
    ).scalar_one()
    assert row.rate == Decimal("7.05")
    assert row.source == "twelvedata"


def test_still_missing_after_fallback_also_fails_sends_one_alert(
    db_session: Session, production_env: None
) -> None:
    with (
        patch("app.services.fx_fetcher.fetch_live_rate", return_value={}),
        patch("app.services.fx_fetcher._twelvedata_key", return_value="fake-key"),
        patch(
            "app.services.fx_fetcher.twelvedata_fetch_live_rate",
            side_effect=ValueError("twelvedata: no data"),
        ),
        patch("app.services.fx_fetcher.time.sleep"),
        patch("app.services.fx_fetcher.send_ops_alert", return_value=True) as mock_alert,
    ):
        result = capture_fx_rates(db_session)

    assert set(result.still_missing) == set(_PAIRS)
    still_missing_alert = next(
        c for c in mock_alert.call_args_list if "FX capture still missing" in c.kwargs["subject"]
    )
    assert still_missing_alert.kwargs["severity"] == "WARNING"


def test_no_twelvedata_key_skips_fallback_without_raising(db_session: Session) -> None:
    with (
        patch("app.services.fx_fetcher.fetch_live_rate", return_value={}),
        patch("app.services.fx_fetcher._twelvedata_key", return_value=None),
        patch("app.services.fx_fetcher.twelvedata_fetch_live_rate") as mock_td,
    ):
        result = capture_fx_rates(db_session)

    mock_td.assert_not_called()
    assert set(result.still_missing) == set(_PAIRS)


def test_fallback_paces_calls_to_stay_under_rate_limit(db_session: Session) -> None:
    """Twelve Data free tier: 8 req/min (_twelvedata.py's own comment). A
    burst of N missing pairs must not fire N unthrottled requests — #518's
    root cause (8 pairs 400'd on the date-range bug, the next 6 429'd
    because the loop never paused)."""
    missing_pairs = list(_PAIRS)[:3]
    fake_points = {yf: (7.18, _AS_OF) for pair, yf in _PAIRS.items() if pair not in missing_pairs}
    with (
        patch("app.services.fx_fetcher.fetch_live_rate", return_value=fake_points),
        patch("app.services.fx_fetcher._twelvedata_key", return_value="fake-key"),
        patch("app.services.fx_fetcher.twelvedata_fetch_live_rate", return_value=Decimal("7.05")),
        patch("app.services.fx_fetcher.time.sleep") as mock_sleep,
    ):
        capture_fx_rates(db_session)

    # 3 fallback calls -> 2 inter-call sleeps (no sleep before the first),
    # each exactly the documented per-call interval (not just "some" delay).
    assert mock_sleep.call_count == 2
    assert mock_sleep.call_args_list == [call(8.0), call(8.0)]


def test_fallback_stays_within_rate_limit_over_a_full_14_pair_burst(
    db_session: Session,
) -> None:
    """The real #518 shape: all 14 pairs miss yfinance in one run. Drives a
    fake clock forward by each `time.sleep` call and asserts no rolling
    60-second window ever sees more than 8 Twelve Data calls — a genuine
    check against the vendor's actual limit, not just "some sleeps
    happened"."""
    clock = {"now": 0.0}
    call_times: list[float] = []

    def fake_sleep(seconds: float) -> None:
        clock["now"] += seconds

    def fake_twelvedata_call(symbol: str, api_key: str) -> Decimal:
        call_times.append(clock["now"])
        return Decimal("7.05")

    with (
        patch("app.services.fx_fetcher.fetch_live_rate", return_value={}),
        patch("app.services.fx_fetcher._twelvedata_key", return_value="fake-key"),
        patch(
            "app.services.fx_fetcher.twelvedata_fetch_live_rate", side_effect=fake_twelvedata_call
        ),
        patch("app.services.fx_fetcher.time.sleep", side_effect=fake_sleep),
    ):
        result = capture_fx_rates(db_session)

    assert len(call_times) == len(_PAIRS)
    assert result.still_missing == []
    for window_start in call_times:
        calls_in_window = sum(1 for t in call_times if window_start <= t < window_start + 60.0)
        assert calls_in_window <= 8, (
            f"{calls_in_window} Twelve Data calls fell inside a 60s window "
            f"starting at t={window_start} -- exceeds the free-tier 8/min cap"
        )


def test_upsert_is_idempotent(db_session: Session) -> None:
    fake_points = {yf: (7.18, _AS_OF) for yf in _PAIRS.values()}
    with (
        patch("app.services.fx_fetcher.fetch_live_rate", return_value=fake_points),
        patch("app.services.fx_fetcher.time.sleep"),
    ):
        capture_fx_rates(db_session)
        capture_fx_rates(db_session)

    count = len(list(db_session.execute(select(FxRate)).scalars()))
    assert count == len(_PAIRS)
