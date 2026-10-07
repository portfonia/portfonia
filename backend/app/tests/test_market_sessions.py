"""Calendar coverage warnings approved in issue #686 F1/F5."""

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone
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
            {"close": pd.date_range("2026-12-01 07:00", "2026-12-31 07:00", tz="UTC")},
            index=pd.date_range("2026-12-01", "2026-12-31"),
        )
    )
    monkeypatch.setattr(
        market_sessions, "_calendar", lambda market: cal if market == "A-Share" else None
    )
    monkeypatch.setattr(market_sessions, "_warned_keys", set(), raising=False)
    logging.getLogger("app.services.market_sessions").disabled = False


def test_mapped_out_of_range_warns_and_alerts_once(caplog: pytest.LogCaptureFixture) -> None:
    alert = cast(MagicMock, market_sessions.send_ops_alert)
    instant = datetime(2027, 1, 1, tzinfo=UTC)
    with caplog.at_level(logging.WARNING, logger="app.services.market_sessions"):
        assert market_sessions.sessions_closing_in("A-Share", instant, instant) == []
        assert market_sessions.baseline_session("A-Share", instant) is None
        assert market_sessions.previous_sessions("A-Share", date(2027, 1, 1), 1) == []
    alert.assert_called_once()
    assert alert.call_args.kwargs["idempotency_key"] == "calendar-coverage-XSHG-2026-12-31-expired"
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
            "A-Share", datetime(2026, 12, 1, 8, tzinfo=UTC)
        ) == date(2026, 12, 1)
    alert.assert_called_once()
    assert alert.call_args.kwargs["idempotency_key"] == "calendar-coverage-XSHG-2026-12-31"
    market_sessions.baseline_session("A-Share", datetime(2026, 12, 2, 8, tzinfo=UTC))
    alert.assert_called_once()
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


def test_last_five_days_alert_each_utc_date_after_early_warning() -> None:
    alert = cast(MagicMock, market_sessions.send_ops_alert)
    market_sessions.baseline_session("A-Share", datetime(2026, 12, 1, 8, tzinfo=UTC))
    for day in range(26, 31):
        # The local date is the previous day, but the evaluated UTC date is day.
        instant = datetime(2026, 12, day, 0, 30, tzinfo=UTC).astimezone(
            timezone(-timedelta(hours=8))
        )
        market_sessions.baseline_session("A-Share", instant)
        market_sessions.baseline_session("A-Share", instant)
    market_sessions.baseline_session("A-Share", datetime(2027, 1, 1, tzinfo=UTC))
    market_sessions.baseline_session("A-Share", datetime(2027, 1, 2, tzinfo=UTC))
    keys = [call.kwargs["idempotency_key"] for call in alert.call_args_list]
    assert keys == [
        "calendar-coverage-XSHG-2026-12-31",
        *[f"calendar-coverage-XSHG-2026-12-31-2026-12-{day}" for day in range(26, 31)],
        "calendar-coverage-XSHG-2026-12-31-expired",
    ]


def test_alert_body_has_verbatim_maintenance_prompt_and_xshg_capture_note() -> None:
    alert = cast(MagicMock, market_sessions.send_ops_alert)
    market_sessions.baseline_session("A-Share", datetime(2026, 12, 1, 8, tzinfo=UTC))
    body = alert.call_args.kwargs["body"]
    prompt = (
        "In /Users/garyj/Portfonia: check PyPI for the newest exchange_calendars release "
        "and confirm its XSHG calendar covers the next year (last_session after the current "
        "coverage end). If none does yet, report that and stop. Otherwise create a worktree "
        "on a new branch from origin/main, bump exchange_calendars in backend/requirements.txt, "
        "run backend/app/tests/test_market_sessions.py and the china_session_calendar tests, "
        "then the full gate (ruff format, ruff check, mypy, pytest -q), open a PR referencing "
        "the calendar coverage alert, and stop. Merge and production deployment need the "
        "product owner's explicit approval."
    )
    assert prompt in body
    assert "China fund-NAV/ETF lag check" in body
    assert "Sina/Tencent fallback" in body
    assert "china_session_calendar" in body
    assert "stale primary NAV" in body
