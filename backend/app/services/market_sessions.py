"""Official exchange sessions for report windows and intelligence signals."""

from __future__ import annotations

from datetime import date, datetime
from functools import lru_cache
from typing import Protocol, cast

import exchange_calendars as xcals
import pandas as pd

_CALENDARS = {
    "US": "XNYS",
    "HK": "XHKG",
    "A-Share": "XSHG",
    "UK": "XLON",
    "Europe": "XETR",
    "Japan": "XTKS",
    "Korea": "XKRX",
}


class _Calendar(Protocol):
    schedule: pd.DataFrame


@lru_cache(maxsize=7)
def _calendar(market: str | None) -> _Calendar | None:
    name = _CALENDARS.get(market or "")
    return cast(_Calendar, xcals.get_calendar(name)) if name else None


def _covered(schedule: pd.DataFrame, instant: datetime) -> bool:
    day = pd.Timestamp(instant).tz_convert("UTC").tz_localize(None).normalize()
    return bool(schedule.index[0] <= day <= schedule.index[-1])


def sessions_closing_in(market: str | None, start: datetime, end: datetime) -> list[date]:
    """Session dates with official closes in (start, end], or unavailable."""
    cal = _calendar(market)
    if cal is None or not _covered(cal.schedule, start) or not _covered(cal.schedule, end):
        return []
    closes = cal.schedule["close"]
    return [cast(date, label.date()) for label in closes[(closes > start) & (closes <= end)].index]


def baseline_session(market: str | None, start: datetime) -> date | None:
    """Last session closing at/before start; never guess outside coverage."""
    cal = _calendar(market)
    if cal is None or not _covered(cal.schedule, start):
        return None
    closes = cal.schedule["close"]
    labels = closes[closes <= start].index
    return cast(date, labels[-1].date()) if len(labels) else None


def previous_sessions(market: str | None, session: date, n: int) -> list[date]:
    """Exactly n preceding sessions, oldest first; [] if unavailable."""
    cal = _calendar(market)
    if cal is None or pd.Timestamp(session) not in cal.schedule.index:
        return []
    labels = cal.schedule.index[cal.schedule.index < pd.Timestamp(session)][-n:] if n else []
    if len(labels) != n:
        return []
    return [cast(date, label.date()) for label in labels]
