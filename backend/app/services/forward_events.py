"""Forward-calendar data: US macro releases + FOMC + held-company earnings (#1).

Scheduled FACTS only — dates that are already on the official calendar. The report
frames them as "X is scheduled for Y, your Z has exposure, watch W" and never
forecasts an outcome (that would cross the Layer-4 boundary). China forward intel
is out of scope: only US releases and US-listed earnings are collected.

Sources:
- Macro releases — FRED ``release/dates`` for a curated set of releases. FRED
  carries the agencies' published forward schedules, so a suspended/shutdown month
  simply will not appear (and the report adds an RSS-derived delay caveat).
- FOMC statement dates — hardcoded from federalreserve.gov (FRED's FOMC release has
  no registered forward schedule). VERIFY ANNUALLY.
- Earnings — yfinance ``Ticker.calendar`` (no extra dependency, unlike
  ``get_earnings_dates`` which needs lxml).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models.forward_event import ForwardEvent

logger = logging.getLogger(__name__)

_FRED_BASE = "https://api.stlouisfed.org/fred/release/dates"

# FRED release_id -> friendly macro release name (US scheduled data releases).
_FRED_RELEASES: dict[int, str] = {
    10: "Consumer Price Index (CPI)",
    46: "Producer Price Index (PPI)",
    50: "Employment Situation (Nonfarm Payrolls)",
    9: "Retail Sales",
    54: "Personal Income & Outlays (PCE)",
    53: "Gross Domestic Product (GDP)",
    91: "Consumer Sentiment (UMich)",
}

# FOMC statement-release (final-day) dates, verified from federalreserve.gov
# (monetarypolicy/fomccalendars.htm). FRED's "FOMC Press Release" has no registered
# forward schedule, so these are hardcoded. VERIFY ANNUALLY when the Fed publishes
# the next year's calendar.
_FOMC_DATES: tuple[str, ...] = (
    "2026-01-28",
    "2026-03-18",
    "2026-04-29",
    "2026-06-17",
    "2026-07-29",
    "2026-09-16",
    "2026-10-28",
    "2026-12-09",
    "2027-01-27",
    "2027-03-17",
    "2027-04-28",
    "2027-06-09",
    "2027-07-28",
    "2027-09-15",
    "2027-10-27",
    "2027-12-08",
)


@dataclass
class ForwardEventData:
    event_type: str  # macro / earnings
    name: str
    ticker: str  # "" for macro, symbol for earnings
    scheduled_date: date
    source: str  # fred / fomc / yfinance


def fetch_fred_release_dates(
    api_key: str, today: date, horizon_days: int
) -> list[ForwardEventData]:
    """Scheduled macro release dates within [today, today+horizon] from FRED."""
    end = today + timedelta(days=horizon_days)
    out: list[ForwardEventData] = []
    with httpx.Client(timeout=20.0) as client:
        for release_id, name in _FRED_RELEASES.items():
            try:
                resp = client.get(
                    _FRED_BASE,
                    params={
                        "release_id": release_id,
                        "api_key": api_key,
                        "file_type": "json",
                        "include_release_dates_with_no_data": "true",
                        "realtime_start": today.isoformat(),
                        "realtime_end": "9999-12-31",
                        "sort_order": "asc",
                        "limit": 12,
                    },
                )
                resp.raise_for_status()
                for rd in resp.json().get("release_dates", []):
                    d = date.fromisoformat(rd["date"])
                    if today <= d <= end:
                        out.append(ForwardEventData("macro", name, "", d, "fred"))
            except Exception:
                logger.exception("FRED fetch failed for release %s", release_id)
    return out


def fetch_fomc_dates(today: date, horizon_days: int) -> list[ForwardEventData]:
    end = today + timedelta(days=horizon_days)
    return [
        ForwardEventData("macro", "FOMC Statement", "", d, "fomc")
        for s in _FOMC_DATES
        if today <= (d := date.fromisoformat(s)) <= end
    ]


def fetch_earnings_dates(
    tickers: list[str], today: date, horizon_days: int
) -> list[ForwardEventData]:
    """Next scheduled earnings date per ticker within the horizon (yfinance)."""
    import yfinance as yf

    end = today + timedelta(days=horizon_days)
    out: list[ForwardEventData] = []
    for ticker in tickers:
        try:
            cal = yf.Ticker(ticker).calendar or {}
            dates = cal.get("Earnings Date") or []
            if isinstance(dates, date):
                dates = [dates]
            for d in dates:
                if isinstance(d, datetime):
                    d = d.date()
                if isinstance(d, date) and today <= d <= end:
                    out.append(ForwardEventData("earnings", ticker, ticker, d, "yfinance"))
        except Exception:
            logger.exception("earnings fetch failed for %s", ticker)
    return out


def persist_forward_events(session: Session, events: list[ForwardEventData]) -> int:
    """Idempotent upsert on (event_type, name, ticker, scheduled_date)."""
    if not events:
        return 0
    now = datetime.now(tz=UTC)
    rows = [
        {
            "event_type": e.event_type,
            "name": e.name,
            "ticker": e.ticker,
            "scheduled_date": e.scheduled_date,
            "source": e.source,
            "captured_at": now,
        }
        for e in events
    ]
    base = pg_insert(ForwardEvent).values(rows)
    stmt = base.on_conflict_do_update(
        constraint="uq_forward_events_key",
        set_={"source": base.excluded["source"], "captured_at": base.excluded["captured_at"]},
    ).returning(ForwardEvent.id)
    n = len(session.execute(stmt).fetchall())
    session.commit()
    return n


def load_forward_events(session: Session, start: date, end: date) -> list[dict[str, str]]:
    """Forward events scheduled in [start, end], soonest first (for the report).

    `id` is included (issue #128 A3) because the L2 shared macro-event cache
    keys a scheduled event as `fwd:<id>` — the row's own primary key, stable
    across captures since `persist_forward_events` upserts on
    `uq_forward_events_key`. It is carried here rather than fetched by a
    second, separately-written query so the events A3 analyzes and the events
    §2.5 renders can never be two divergent sets. The report renderers read
    by key and ignore it.
    """
    rows = (
        session.execute(
            select(ForwardEvent)
            .where(
                ForwardEvent.scheduled_date >= start,
                ForwardEvent.scheduled_date <= end,
            )
            .order_by(ForwardEvent.scheduled_date.asc())
        )
        .scalars()
        .all()
    )
    return [
        {
            "id": str(r.id),
            "event_type": r.event_type,
            "name": r.name,
            "ticker": r.ticker,
            "scheduled_date": r.scheduled_date.isoformat(),
            "source": r.source,
        }
        for r in rows
    ]
