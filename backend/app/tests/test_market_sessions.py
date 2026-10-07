"""Calendar coverage warnings approved in issue #686 F1."""

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import cast
from unittest.mock import MagicMock

import pandas as pd
import pytest

from app.services import market_sessions


@dataclass
class _Calendar:
    schedule: pd.DataFrame


@pytest.fixture(autouse=True)
def _bounded_calendar(monkeypatch: pytest.MonkeyPatch) -> None:
    cal = _Calendar(
        pd.DataFrame(
            {
                "close": pd.to_datetime(
                    ["2026-12-29 07:00Z", "2026-12-30 07:00Z", "2026-12-31 07:00Z"]
                )
            },
            index=pd.to_datetime(["2026-12-29", "2026-12-30", "2026-12-31"]),
        )
    )
    monkeypatch.setattr(
        market_sessions, "_calendar", lambda market: cal if market == "A-Share" else None
    )
    monkeypatch.setattr(market_sessions, "_warned_calendars", set(), raising=False)
    logging.getLogger("app.services.market_sessions").disabled = False


def test_mapped_out_of_range_warns_and_alerts_once(caplog: pytest.LogCaptureFixture) -> None:
    alert = cast(MagicMock, market_sessions.send_ops_alert)
    instant = datetime(2027, 1, 1, tzinfo=UTC)
    with caplog.at_level(logging.WARNING, logger="app.services.market_sessions"):
        assert (
            market_sessions.sessions_closing_in(
                "A-Share", datetime(2026, 12, 30, tzinfo=UTC), instant
            )
            == []
        )
        assert market_sessions.baseline_session("A-Share", instant) is None
        assert market_sessions.previous_sessions("A-Share", date(2027, 1, 1), 1) == []
    alert.assert_called_once()
    assert alert.call_args.kwargs["idempotency_key"] == "calendar-coverage-XSHG-2026-12-31"
    assert alert.call_args.kwargs["severity"] == "WARNING"
    body = alert.call_args.kwargs["body"]
    for text in [
        "A-Share",
        "2026-12-31",
        "window moves",
        "anomalies",
        "large-holding price lines",
        "intel price signals",
        "exchange_calendars",
        "redeploy",
    ]:
        assert text in body
    assert len(caplog.records) == 1
    assert caplog.records[0].levelno == logging.WARNING
    assert "XSHG" in caplog.text
    with caplog.at_level(logging.WARNING, logger="app.services.market_sessions"):
        market_sessions.baseline_session("A-Share", instant)
        market_sessions.sessions_closing_in("A-Share", instant, instant)
    assert len(caplog.records) == 1
    alert.assert_called_once()


def test_near_coverage_end_warns_before_unavailability(caplog: pytest.LogCaptureFixture) -> None:
    alert = cast(MagicMock, market_sessions.send_ops_alert)
    with caplog.at_level(logging.WARNING, logger="app.services.market_sessions"):
        assert market_sessions.baseline_session(
            "A-Share", datetime(2026, 12, 30, 8, tzinfo=UTC)
        ) == date(2026, 12, 30)
    alert.assert_called_once()
    assert alert.call_args.kwargs["idempotency_key"] == "calendar-coverage-XSHG-2026-12-31"
    assert len(caplog.records) == 1


def test_unmapped_market_is_silent(caplog: pytest.LogCaptureFixture) -> None:
    alert = cast(MagicMock, market_sessions.send_ops_alert)
    instant = datetime(2027, 1, 1, tzinfo=UTC)
    with caplog.at_level(logging.WARNING, logger="app.services.market_sessions"):
        for market in ["Other", None]:
            assert market_sessions.sessions_closing_in(market, instant, instant) == []
            assert market_sessions.baseline_session(market, instant) is None
            assert market_sessions.previous_sessions(market, date(2027, 1, 1), 1) == []
    alert.assert_not_called()
    assert not caplog.records
