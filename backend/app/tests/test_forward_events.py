"""Tests for the forward-calendar data layer (#1)."""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

from sqlalchemy.orm import Session

from app.models.forward_event import ForwardEvent
from app.services import forward_events as fe


def test_fetch_fomc_dates_filters_to_horizon() -> None:
    today = date(2026, 6, 9)
    events = fe.fetch_fomc_dates(today, horizon_days=10)  # next FOMC 2026-06-17
    assert [e.scheduled_date for e in events] == [date(2026, 6, 17)]
    assert events[0].event_type == "macro" and events[0].source == "fomc"


def test_fetch_fomc_dates_empty_when_none_in_window() -> None:
    assert fe.fetch_fomc_dates(date(2026, 6, 18), horizon_days=5) == []


def test_fetch_fred_release_dates_windows_and_labels() -> None:
    """FRED rows outside [today, today+horizon] are dropped; in-window become macro."""

    class _Resp:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, list[dict[str, str]]]:
            return {"release_dates": [{"date": "2026-06-10"}, {"date": "2026-09-01"}]}

    client = MagicMock()
    client.get.return_value = _Resp()
    client.__enter__.return_value = client
    client.__exit__.return_value = False

    with patch("app.services.forward_events.httpx.Client", return_value=client):
        events = fe.fetch_fred_release_dates("KEY", date(2026, 6, 9), horizon_days=10)

    dates = {e.scheduled_date for e in events}
    assert date(2026, 6, 10) in dates  # in window
    assert date(2026, 9, 1) not in dates  # beyond horizon
    assert all(e.event_type == "macro" and e.source == "fred" for e in events)


def test_fetch_earnings_dates_filters_window_and_uses_calendar() -> None:
    ticker_obj = MagicMock()
    ticker_obj.calendar = {"Earnings Date": [date(2026, 6, 15), date(2026, 12, 1)]}
    with patch("yfinance.Ticker", return_value=ticker_obj):
        events = fe.fetch_earnings_dates(["NVDA"], date(2026, 6, 9), horizon_days=10)
    assert [e.scheduled_date for e in events] == [date(2026, 6, 15)]
    assert events[0].event_type == "earnings" and events[0].ticker == "NVDA"


def test_persist_and_load_roundtrip_is_idempotent(db_session: Session) -> None:
    events = [
        fe.ForwardEventData("macro", "FOMC Statement", "", date(2026, 6, 17), "fomc"),
        fe.ForwardEventData("earnings", "NVDA", "NVDA", date(2026, 6, 15), "yfinance"),
    ]
    assert fe.persist_forward_events(db_session, events) == 2
    # Re-persisting the same keys upserts rather than duplicating.
    fe.persist_forward_events(db_session, events)
    assert db_session.query(ForwardEvent).count() == 2

    loaded = fe.load_forward_events(db_session, date(2026, 6, 9), date(2026, 6, 19))
    # Soonest first.
    assert [e["scheduled_date"] for e in loaded] == ["2026-06-15", "2026-06-17"]
    assert loaded[1]["name"] == "FOMC Statement"
