"""Issue #716 contract using cached fixtures and real Postgres."""

from datetime import date, timedelta
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.user import User
from app.services import jade_replay as replay
from app.services.jade_replay_config import ReplayRange
from app.tests.conftest import TEST_USER_ID
from app.tests.test_jade_replay import (
    E,
    calendar,
    holding,
    series,
    setup,  # noqa: F401 - autouse fixture
)


@pytest.mark.parametrize(
    "key,start",
    [
        ("1M", date(2026, 9, 8)),
        ("3M", date(2026, 7, 8)),
        ("6M", date(2026, 4, 8)),
        ("1Y", date(2025, 10, 8)),
        ("3Y", date(2023, 10, 8)),
        ("5Y", date(2021, 10, 8)),
        ("YTD", date(2025, 12, 31)),
    ],
)
def test_a1_d6_1_window_starts(db_session: Session, key: ReplayRange, start: date) -> None:
    calendar(db_session, [date(2025, 12, 30), date(2025, 12, 31), date(2026, 1, 2), E])
    assert replay.window_start(db_session, E, key) == start


@pytest.mark.parametrize(
    "end,months,start",
    [
        (date(2026, 3, 31), 1, date(2026, 2, 28)),
        (date(2026, 3, 31), 6, date(2025, 9, 30)),
        (date(2028, 3, 31), 1, date(2028, 2, 29)),
    ],
)
def test_a1_d6_2_month_end_clamp(end: date, months: int, start: date) -> None:
    from app.services import jade_replay_config as config

    assert config.months_before(end, months) == start


def test_a2_d6_3_early_january_ytd(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    end = date(2026, 1, 2)
    monkeypatch.setattr(replay, "today_et", lambda: end)
    calendar(db_session, [date(2025, 12, 31), end])
    holding(db_session, asset_type="cash")
    result = replay.compute_replay(db_session, TEST_USER_ID, "USD", "sp500", "YTD")
    assert result.window_start == date(2025, 12, 31) and result.window_end == end
    assert result.status == "insufficient" and result.sample_count == 1
    assert result.metrics.portfolio is None and result.points == []


def test_a3_d6_4_short_span_annualization() -> None:
    values = [(date(2026, 7, 8) + timedelta(days=i), 100 + 5 * i / 92) for i in range(93)]
    assert replay.metrics(values, hide_worst_month=False).annualized_return == "0.213735"


@pytest.mark.parametrize("key,hidden", [("1M", True), ("3M", False)])
def test_a4_d6_5_worst_month(db_session: Session, key: ReplayRange, hidden: bool) -> None:
    days = [E - timedelta(days=i) for i in reversed(range(16))]
    calendar(db_session, days)
    holding(db_session, asset_type="cash")
    series(db_session, "yf:SPY", days, [100 + i for i in range(16)])
    result = replay.compute_replay(db_session, TEST_USER_ID, "USD", "sp500", key)
    assert result.status == "ok"
    for m in (result.metrics.portfolio, result.metrics.benchmark):
        assert m is not None
        assert (m.worst_month is None) == hidden
        assert (m.worst_month_label is None) == hidden


@pytest.mark.parametrize("key,method", [("3M", "own"), ("1Y", "head_proxy")])
def test_a5_d6_6_recent_listing(db_session: Session, key: ReplayRange, method: str) -> None:
    first = date(2026, 6, 12)
    days = [date(2025, 10, 8), *[first + timedelta(days=i) for i in range((E - first).days + 1)]]
    calendar(db_session, days)
    series(db_session, "yf:SPY", days, [100 + i for i in range(len(days))])
    series(db_session, "yf:SPCX", days[1:], [100 + i for i in range(len(days) - 1)])
    holding(db_session, auto=True, ticker="SPCX")
    result = replay.compute_replay(db_session, TEST_USER_ID, "USD", "sp500", key)
    assert result.holdings[0].method == method and result.holdings[0].own_first_date == first


def test_a6_d6_8_api_default_explicit_and_invalid(
    app_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = db_session.get(User, TEST_USER_ID)
    assert user is not None
    user.subscription_status = "active"
    user.subscription_type = "jade"
    calendar(db_session, [E])
    db_session.flush()
    provider = Mock(side_effect=AssertionError("provider call"))
    monkeypatch.setattr("app.services._yfinance.fetch_ohlcv_range_bounded", provider)
    monkeypatch.setattr("app.services.fund_nav_fetcher.fetch_nav_history_pages", provider)
    default = app_client.get("/jade/replay")
    assert default.status_code == 200
    assert default.json()["range"] == "1Y" and default.json()["window_start"] == "2025-10-08"
    five = app_client.get("/jade/replay?range=5Y")
    assert five.status_code == 200
    assert five.json()["range"] == "5Y" and five.json()["window_start"] == "2021-10-08"
    assert app_client.get("/jade/replay?range=2Y").status_code == 422
    provider.assert_not_called()
    assert not db_session.new and not db_session.dirty and not db_session.deleted
