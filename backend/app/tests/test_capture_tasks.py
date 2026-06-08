"""Tests for capture tasks + the market-session Beat schedule (ADR-002 step 2c)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from app.core.timezones import ET, HKT
from app.tasks import celery_app

# ---------------------------------------------------------------------------
# Beat schedule: one prices + one news entry per market node
# ---------------------------------------------------------------------------


def test_capture_schedule_has_all_us_nodes() -> None:
    sched = celery_app.conf.beat_schedule
    for node in ("pre_open", "open", "close", "after_close"):
        assert f"capture-prices-US-{node}" in sched
        assert f"capture-news-US-{node}" in sched


def test_capture_schedule_hk_cn_have_two_nodes_each() -> None:
    sched = celery_app.conf.beat_schedule
    for market in ("HK", "A-Share"):
        assert f"capture-prices-{market}-open" in sched
        assert f"capture-prices-{market}-close" in sched
        assert f"capture-prices-{market}-after_close" not in sched  # no after-hours


def test_capture_prices_entry_carries_market_and_node_args() -> None:
    entry = celery_app.conf.beat_schedule["capture-prices-US-close"]
    assert entry["task"] == "app.tasks.capture_tasks.capture_prices_task"
    assert entry["args"] == ("US", "close")


def test_beat_schedule_is_picklable() -> None:
    """PersistentScheduler shelves the schedule — every entry must pickle
    (a lambda nowfun would crash beat at startup)."""
    import pickle

    for name, entry in celery_app.conf.beat_schedule.items():
        pickle.dumps(entry["schedule"]), name  # raises if not picklable


def test_node_cron_uses_market_local_timezone() -> None:
    us = celery_app.conf.beat_schedule["capture-prices-US-open"]["schedule"]
    hk = celery_app.conf.beat_schedule["capture-prices-HK-open"]["schedule"]
    # nowfun pins each entry to its market clock — DST-correct for US, fixed for HK.
    assert us.nowfun().tzinfo == ET
    assert hk.nowfun().tzinfo == HKT
    assert 9 in us.hour and 30 in us.minute


# ---------------------------------------------------------------------------
# Task bodies (call .run() to bypass Celery routing)
# ---------------------------------------------------------------------------


@patch("app.core.database.SessionLocal")
@patch("app.services.news_capture.capture_news", return_value=4)
def test_capture_news_task(mock_cap: MagicMock, mock_session_cls: MagicMock) -> None:
    from app.tasks.capture_tasks import capture_news_task

    session = MagicMock()
    mock_session_cls.return_value = session
    result = capture_news_task.run()
    assert result == {"inserted": 4}
    session.close.assert_called_once()


@patch("app.core.database.SessionLocal")
@patch("app.services.price_capture.capture_prices", return_value=7)
def test_capture_prices_task(mock_cap: MagicMock, mock_session_cls: MagicMock) -> None:
    from app.tasks.capture_tasks import capture_prices_task

    session = MagicMock()
    mock_session_cls.return_value = session
    result = capture_prices_task.run("US", "close")
    assert result == {"market": "US", "session_node": "close", "written": 7}
    mock_cap.assert_called_once_with(session, "US", "close")
    session.close.assert_called_once()
