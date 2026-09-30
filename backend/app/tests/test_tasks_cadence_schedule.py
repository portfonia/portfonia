"""next_occurrence_for_cadence (issue #202) — reads the same _REPORT_CADENCES
table Beat schedules from, so this and the real schedule can't drift apart."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from app.core.timezones import ET
from app.tasks import next_occurrence_for_cadence


def test_mwf_next_occurrence_is_a_mon_wed_or_fri_at_17_00_et() -> None:
    now = datetime(2026, 9, 3, 10, 0, tzinfo=ET)  # a Thursday
    nxt = next_occurrence_for_cadence("mwf", now)
    assert nxt > now
    assert nxt.weekday() in (0, 2, 4)  # Mon/Wed/Fri
    assert (nxt.hour, nxt.minute) == (17, 0)


def test_weekly_next_occurrence_is_a_saturday_at_19_00_et() -> None:
    now = datetime(2026, 9, 3, 10, 0, tzinfo=ET)
    nxt = next_occurrence_for_cadence("weekly", now)
    assert nxt > now
    assert nxt.weekday() == 5  # Saturday
    assert (nxt.hour, nxt.minute) == (19, 0)


def test_converts_a_non_et_input_to_et_before_computing() -> None:
    from datetime import UTC

    now_utc = datetime(2026, 9, 3, 14, 0, tzinfo=UTC)  # 10:00 ET
    now_et = now_utc.astimezone(ET)
    assert next_occurrence_for_cadence("mwf", now_utc) == next_occurrence_for_cadence("mwf", now_et)


def test_unknown_cadence_raises() -> None:
    with pytest.raises(ValueError, match="unknown report cadence"):
        next_occurrence_for_cadence("daily", datetime(2026, 9, 3, tzinfo=ET))


@pytest.mark.parametrize(
    "cadence,now,expected_date,hour,minute",
    [
        ("mwf", datetime(2026, 10, 31, 12, tzinfo=ET), date(2026, 11, 2), 17, 0),
        ("mwf", datetime(2027, 3, 13, 12, tzinfo=ET), date(2027, 3, 15), 17, 0),
        ("mwf", datetime(2026, 10, 27, 12, tzinfo=ET), date(2026, 10, 28), 17, 0),
        ("weekly", datetime(2026, 10, 28, 12, tzinfo=ET), date(2026, 10, 31), 19, 0),
        ("mwf", datetime(2026, 11, 1, 1, 30, tzinfo=ET), date(2026, 11, 2), 17, 0),
    ],
    ids=[
        "fall-transition",
        "spring-transition",
        "ordinary-mwf",
        "ordinary-weekly",
        "ambiguous-hour",
    ],
)
def test_next_occurrence_et_wall_clock(
    cadence: str, now: datetime, expected_date: date, hour: int, minute: int
) -> None:
    result = next_occurrence_for_cadence(cadence, now)
    assert result.tzinfo == ET
    assert (result.date(), result.hour, result.minute) == (expected_date, hour, minute)
