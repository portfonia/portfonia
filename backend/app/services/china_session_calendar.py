"""Scoped XSHG session calendar for China fund-NAV lag and A-Share ETF gaps.

Used only by the #389 capture decision. Does not replace global scheduling
or valuation calendars. Coverage outside the loaded XSHG range is
``calendar_unknown`` — never a weekday guess.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import lru_cache
from typing import Literal, Protocol, cast

import exchange_calendars as xcals
import pandas as pd

from app.core.timezones import CST

_XSHG = "XSHG"
CalendarStatus = Literal["ok", "calendar_unknown"]


class _SessionCalendar(Protocol):
    first_session: object
    last_session: object

    def session_close(self, session: object) -> object: ...
    def previous_session(self, session: object) -> object: ...
    def sessions_in_range(self, start: object, end: object) -> Iterable[object]: ...


@dataclass(frozen=True)
class ChinaSessionWindow:
    as_of_utc: datetime
    window_start: date
    window_end: date
    latest_session: date | None
    cutoff: date | None
    calendar_status: CalendarStatus


@lru_cache(maxsize=1)
def _calendar() -> _SessionCalendar:
    return xcals.get_calendar(_XSHG)  # type: ignore[no-any-return]


def _as_date(value: object) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    ts = pd.Timestamp(value)
    return cast(date, ts.date())


def _session_close_utc(cal: _SessionCalendar, session: object) -> datetime:
    close = cal.session_close(session)
    ts = pd.Timestamp(close)
    ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
    return cast(datetime, ts.to_pydatetime())


def _coverage_ok(cal: _SessionCalendar, start: date, end: date) -> bool:
    first = _as_date(cal.first_session)
    last = _as_date(cal.last_session)
    return start >= first and end <= last and start <= end


def freeze_capture_window(
    as_of_utc: datetime, lookback_days: int, max_lag_sessions: int
) -> ChinaSessionWindow:
    """Freeze the capture date window and NAV cutoff at the first attempt."""
    if as_of_utc.tzinfo is None:
        raise ValueError("as_of_utc must be timezone-aware")
    if max_lag_sessions < 0:
        raise ValueError("max_lag_sessions must be >= 0")
    as_of = as_of_utc.astimezone(CST)
    window_end = as_of.date()
    window_start = window_end - timedelta(days=lookback_days)
    unknown = ChinaSessionWindow(
        as_of_utc=as_of_utc,
        window_start=window_start,
        window_end=window_end,
        latest_session=None,
        cutoff=None,
        calendar_status="calendar_unknown",
    )
    try:
        cal = _calendar()
        if not _coverage_ok(cal, window_start, window_end):
            return unknown
        latest = _latest_completed_session(cal, as_of_utc, window_end)
        if latest is None:
            return unknown
        cutoff_ts: object = latest
        for _ in range(max_lag_sessions):
            cutoff_ts = cal.previous_session(cutoff_ts)
        cutoff = _as_date(cutoff_ts)
        if not _coverage_ok(cal, cutoff, _as_date(latest)):
            return unknown
        return ChinaSessionWindow(
            as_of_utc=as_of_utc,
            window_start=window_start,
            window_end=window_end,
            latest_session=_as_date(latest),
            cutoff=cutoff,
            calendar_status="ok",
        )
    except Exception:
        return unknown


def _latest_completed_session(
    cal: _SessionCalendar, as_of_utc: datetime, window_end: date
) -> object | None:
    probe_start = window_end - timedelta(days=30)
    if not _coverage_ok(cal, probe_start, window_end):
        first = _as_date(cal.first_session)
        probe_start = first
        if not _coverage_ok(cal, probe_start, window_end):
            return None
    sessions = cal.sessions_in_range(pd.Timestamp(probe_start), pd.Timestamp(window_end))
    latest: object | None = None
    for session in sessions:
        if _session_close_utc(cal, session) <= as_of_utc:
            latest = session
    return latest


def completed_sessions(as_of_utc: datetime, start: date, end: date) -> tuple[date, ...] | None:
    """Completed XSHG sessions in ``[start, end]`` as of ``as_of_utc``.

    Returns None when the range is outside calendar coverage.
    """
    if as_of_utc.tzinfo is None:
        raise ValueError("as_of_utc must be timezone-aware")
    try:
        cal = _calendar()
        if not _coverage_ok(cal, start, end):
            return None
        sessions = cal.sessions_in_range(pd.Timestamp(start), pd.Timestamp(end))
        out: list[date] = []
        for session in sessions:
            if _session_close_utc(cal, session) <= as_of_utc:
                out.append(_as_date(session))
        return tuple(out)
    except Exception:
        return None
