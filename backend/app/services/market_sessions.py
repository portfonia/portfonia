"""Official exchange sessions for report windows and intelligence signals."""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from functools import lru_cache
from typing import Protocol, cast

import exchange_calendars as xcals
import pandas as pd

from app.services.email_sender import send_ops_alert as send_ops_alert

logger = logging.getLogger(__name__)
_warned_calendars: set[str] = set()

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


def _check_coverage(market: str | None, cal: _Calendar, instant: datetime) -> bool:
    covered = _covered(cal.schedule, instant)
    name = _CALENDARS[market or ""]
    last = cast(date, cal.schedule.index[-1].date())
    evaluated = instant.astimezone(UTC).date()
    if (not covered or (last - evaluated).days <= 30) and name not in _warned_calendars:
        _warned_calendars.add(name)
        body = (
            f"The {name} calendar for {market} has loaded coverage ending on {last.isoformat()}. "
            f"The evaluated date is {evaluated.isoformat()}. Calendar coverage is outside "
            "the requested range or ends within 30 days. Outside coverage, window moves, "
            f"anomalies, large-holding price lines and intel price signals for {market} "
            "are unavailable. Bump exchange_calendars once a release covers the next year, "
            "then redeploy. No weekday approximation is used."
        )
        logger.warning("%s", body)
        send_ops_alert(
            subject=f"[Portfonia] {name} calendar coverage ends {last.isoformat()}",
            body=body,
            idempotency_key=f"calendar-coverage-{name}-{last.isoformat()}",
            severity="WARNING",
        )
    return covered


def sessions_closing_in(market: str | None, start: datetime, end: datetime) -> list[date]:
    """Session dates with official closes in (start, end], or unavailable."""
    cal = _calendar(market)
    if (
        cal is None
        or not _check_coverage(market, cal, start)
        or not _check_coverage(market, cal, end)
    ):
        return []
    closes = cal.schedule["close"]
    return [cast(date, label.date()) for label in closes[(closes > start) & (closes <= end)].index]


def baseline_session(market: str | None, start: datetime) -> date | None:
    """Last session closing at/before start; never guess outside coverage."""
    cal = _calendar(market)
    if cal is None or not _check_coverage(market, cal, start):
        return None
    closes = cal.schedule["close"]
    labels = closes[closes <= start].index
    return cast(date, labels[-1].date()) if len(labels) else None


def previous_sessions(market: str | None, session: date, n: int) -> list[date]:
    """Exactly n preceding sessions, oldest first; [] if unavailable."""
    cal = _calendar(market)
    if cal is None:
        return []
    instant = datetime(session.year, session.month, session.day, tzinfo=UTC)
    if not _check_coverage(market, cal, instant) or pd.Timestamp(session) not in cal.schedule.index:
        return []
    labels = cal.schedule.index[cal.schedule.index < pd.Timestamp(session)][-n:] if n else []
    if len(labels) != n:
        return []
    return [cast(date, label.date()) for label in labels]
