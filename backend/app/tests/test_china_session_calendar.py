"""China session calendar used by fund-NAV lag and A-Share ETF gap checks (#389)."""

from __future__ import annotations

from datetime import date, datetime

from app.core.timezones import CST
from app.services.china_session_calendar import (
    completed_sessions,
    freeze_capture_window,
)


def test_default_lag_cutoffs_on_2026_09_08() -> None:
    morning = freeze_capture_window(
        datetime(2026, 9, 8, 8, 0, tzinfo=CST), lookback_days=30, max_lag_sessions=2
    )
    evening = freeze_capture_window(
        datetime(2026, 9, 8, 20, 0, tzinfo=CST), lookback_days=30, max_lag_sessions=2
    )
    assert morning.calendar_status == "ok"
    assert evening.calendar_status == "ok"
    assert morning.latest_session == date(2026, 9, 7)
    assert evening.latest_session == date(2026, 9, 8)
    assert morning.cutoff == date(2026, 9, 3)
    assert evening.cutoff == date(2026, 9, 4)
    assert morning.window_end == date(2026, 9, 8)
    assert morning.window_start == date(2026, 8, 9)
    assert date(2026, 9, 4) >= morning.cutoff
    assert date(2026, 9, 4) >= evening.cutoff
    assert date(2026, 9, 3) >= morning.cutoff
    assert date(2026, 9, 3) < evening.cutoff


def test_weekend_does_not_increase_lag() -> None:
    saturday = freeze_capture_window(
        datetime(2026, 9, 5, 8, 0, tzinfo=CST), lookback_days=30, max_lag_sessions=2
    )
    assert saturday.calendar_status == "ok"
    assert saturday.latest_session == date(2026, 9, 4)
    assert saturday.cutoff == date(2026, 9, 2)


def test_china_holiday_is_not_a_lag_session() -> None:
    """2026-10-01 is a mainland holiday; it must not count as a completed session."""
    friday_before = freeze_capture_window(
        datetime(2026, 9, 30, 20, 0, tzinfo=CST), lookback_days=7, max_lag_sessions=2
    )
    saturday_holiday = freeze_capture_window(
        datetime(2026, 10, 3, 8, 0, tzinfo=CST), lookback_days=7, max_lag_sessions=2
    )
    assert friday_before.latest_session == date(2026, 9, 30)
    assert saturday_holiday.latest_session == date(2026, 9, 30)
    assert friday_before.cutoff == saturday_holiday.cutoff


def test_unsupported_calendar_range_is_unknown_not_a_weekday_guess() -> None:
    far_future = freeze_capture_window(
        datetime(2200, 1, 6, 8, 0, tzinfo=CST), lookback_days=30, max_lag_sessions=2
    )
    far_past = freeze_capture_window(
        datetime(1990, 1, 8, 8, 0, tzinfo=CST), lookback_days=30, max_lag_sessions=2
    )
    assert far_future.calendar_status == "calendar_unknown"
    assert far_past.calendar_status == "calendar_unknown"
    assert far_future.cutoff is None
    assert far_past.cutoff is None
    assert far_future.latest_session is None
    assert far_past.latest_session is None


def test_zero_lag_cutoff_is_latest_completed_session() -> None:
    evening = freeze_capture_window(
        datetime(2026, 9, 8, 20, 0, tzinfo=CST), lookback_days=7, max_lag_sessions=0
    )
    assert evening.cutoff == evening.latest_session == date(2026, 9, 8)


def test_us_labor_day_does_not_remove_china_session_09_07() -> None:
    sessions = completed_sessions(
        datetime(2026, 9, 8, 20, 0, tzinfo=CST),
        date(2026, 9, 4),
        date(2026, 9, 8),
    )
    assert sessions == (date(2026, 9, 4), date(2026, 9, 7), date(2026, 9, 8))


def test_morning_excludes_same_day_session_not_yet_closed() -> None:
    sessions = completed_sessions(
        datetime(2026, 9, 8, 8, 0, tzinfo=CST),
        date(2026, 9, 4),
        date(2026, 9, 8),
    )
    assert sessions == (date(2026, 9, 4), date(2026, 9, 7))
