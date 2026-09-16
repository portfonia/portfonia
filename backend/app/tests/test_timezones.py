"""ET calendar-day primitive (issue #494)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from app.core.timezones import ET, today_et


def test_et_zone_is_new_york() -> None:
    assert ZoneInfo("America/New_York") == ET


def test_today_et_matches_et_wall_clock_date() -> None:
    assert today_et() == datetime.now(tz=ET).date()


def test_today_et_stays_on_prior_calendar_day_just_after_utc_midnight() -> None:
    """20:30 ET is 00:30 UTC the next calendar day; naive date.today() would
    stamp the UTC day. The Beat snapshot task fires at this instant."""
    instant = datetime(2026, 9, 15, 0, 30, tzinfo=UTC)
    assert instant.date() == date(2026, 9, 15)
    assert instant.astimezone(ET).date() == date(2026, 9, 14)

    with patch("app.core.timezones.datetime") as mock_datetime:
        mock_datetime.now.return_value = instant.astimezone(ET)
        assert today_et() == date(2026, 9, 14)
