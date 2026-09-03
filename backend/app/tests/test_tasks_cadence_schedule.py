"""next_occurrence_for_cadence (issue #202) — reads the same _REPORT_CADENCES
table Beat schedules from, so this and the real schedule can't drift apart."""

from __future__ import annotations

from datetime import datetime

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
