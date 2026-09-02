"""Tests for capture tasks + the market-session Beat schedule (ADR-002 step 2c)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

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


@patch("app.core.database.SessionLocal")
@patch("app.services.price_capture.capture_prices", return_value=7)
def test_backfill_ohlcv_task_no_tickers_is_noop(
    mock_cap: MagicMock, mock_session_cls: MagicMock
) -> None:
    from app.tasks.capture_tasks import backfill_ohlcv_task

    result = backfill_ohlcv_task.run()
    assert result == {"written": 0}
    mock_cap.assert_not_called()


@patch("app.core.database.SessionLocal")
@patch("app.services.price_capture.capture_prices", return_value=7)
def test_backfill_ohlcv_task_passes_tickers_to_each_market(
    mock_cap: MagicMock, mock_session_cls: MagicMock
) -> None:
    from app.tasks.capture_tasks import backfill_ohlcv_task

    session = MagicMock()
    mock_session_cls.return_value = session
    result = backfill_ohlcv_task.run(["AAPL"])
    assert result == {"written": 49}
    assert [c.args[1] for c in mock_cap.call_args_list] == [
        "US",
        "HK",
        "A-Share",
        "UK",
        "Europe",
        "Japan",
        "Korea",
    ]
    for call in mock_cap.call_args_list:
        assert call.kwargs["lookback_days"] == 420
        assert call.kwargs["tickers"] == ["AAPL"]
    session.close.assert_called_once()


@patch("app.core.database.SessionLocal")
@patch("app.services.price_capture.capture_prices")
def test_backfill_ohlcv_continues_after_one_market_fails(
    mock_cap: MagicMock, mock_session_cls: MagicMock
) -> None:
    """A US overflow must not skip HK / A-Share in the same run (issue #194)."""
    from app.tasks.capture_tasks import backfill_ohlcv_task

    session = MagicMock()
    mock_session_cls.return_value = session

    def _cap(
        _session: object,
        market: str,
        _node: str,
        lookback_days: int = 7,
        **_kwargs: object,
    ) -> int:
        if market == "US":
            raise RuntimeError("US exploded")
        return 5

    mock_cap.side_effect = _cap

    # .run() is called_directly, so Celery's retry re-raises the combined
    # RuntimeError rather than celery.exceptions.Retry.
    with (
        patch("app.tasks.capture_tasks._capture_failed") as mock_fail,
        pytest.raises(RuntimeError, match="US exploded"),
    ):
        backfill_ohlcv_task.run(["AAPL", "0700.HK", "000001.SS"])

    assert [c.args[1] for c in mock_cap.call_args_list] == [
        "US",
        "HK",
        "A-Share",
        "UK",
        "Europe",
        "Japan",
        "Korea",
    ]
    mock_fail.assert_not_called()
    session.rollback.assert_called()
    session.close.assert_called_once()


@patch("app.core.database.SessionLocal")
@patch("app.services.price_capture.capture_prices")
def test_backfill_combined_failure_keeps_later_markets_in_alert(
    mock_cap: MagicMock, mock_session_cls: MagicMock
) -> None:
    """A huge first-market error must not crowd HK/A-Share out of the alert."""
    from app.tasks.capture_tasks import backfill_ohlcv_task

    session = MagicMock()
    mock_session_cls.return_value = session

    def _cap(
        _session: object,
        market: str,
        _node: str,
        lookback_days: int = 7,
        **_kwargs: object,
    ) -> int:
        if market == "US":
            raise RuntimeError("U" * 8000)
        if market == "HK":
            raise RuntimeError("HK_UNIQUE_TOKEN")
        raise RuntimeError("CN_UNIQUE_TOKEN")

    mock_cap.side_effect = _cap
    backfill_ohlcv_task.push_request(retries=1)
    try:
        with (
            patch("app.tasks.capture_tasks.send_ops_alert") as mock_alert,
            patch("app.tasks.capture_tasks.create_bug_report") as mock_issue,
            pytest.raises(RuntimeError),
        ):
            backfill_ohlcv_task.run(["AAPL"])
    finally:
        backfill_ohlcv_task.pop_request()

    issue_body = mock_issue.call_args.kwargs["body"]
    alert_body = mock_alert.call_args.kwargs["body"]
    assert "HK_UNIQUE_TOKEN" in issue_body
    assert "CN_UNIQUE_TOKEN" in issue_body
    assert "HK_UNIQUE_TOKEN" in alert_body
    assert "CN_UNIQUE_TOKEN" in alert_body


def test_fx_capture_entry_runs_daily_weekdays() -> None:
    entry = celery_app.conf.beat_schedule["capture-fx-daily"]
    assert entry["task"] == "app.tasks.capture_tasks.capture_fx_task"
    cron = entry["schedule"]
    # 17:15 ET, not 16:05 (issue #258): FX has no NYSE-style hard close, so
    # scheduling it 5 minutes after the 16:00 ET equities close captured
    # yesterday's daily bar every single day — confirmed against 5 days of
    # production fx_rates rows, all off by exactly one day. FX's own daily
    # bar rolls over around 17:00 ET; 17:15 leaves a buffer past that.
    assert cron.hour == {17} and cron.minute == {15}
    assert cron.day_of_week == {1, 2, 3, 4, 5}  # mon-fri


@patch("app.core.database.SessionLocal")
@patch("app.services.fx_fetcher.update_fx_rates")
def test_capture_fx_task(mock_update: MagicMock, mock_session_cls: MagicMock) -> None:
    from app.services.fx_fetcher import FxFetchResult
    from app.tasks.capture_tasks import capture_fx_task

    session = MagicMock()
    mock_session_cls.return_value = session
    mock_update.return_value = FxFetchResult(upserted=3, failed=[])
    result = capture_fx_task.run()
    assert result == {"upserted": 3, "failed": []}
    mock_update.assert_called_once_with(session)
    session.commit.assert_called_once()
    session.close.assert_called_once()


@patch("app.core.database.SessionLocal")
@patch("app.services.price_capture.capture_fund_navs", return_value=8)
def test_capture_fund_navs_task_is_full_universe(
    mock_cap: MagicMock, mock_session_cls: MagicMock
) -> None:
    """Daily beat path stays unscoped — no fund_codes filter (#196)."""
    from app.tasks.capture_tasks import capture_fund_navs_task

    session = MagicMock()
    mock_session_cls.return_value = session
    result = capture_fund_navs_task.run()
    assert result == {"written": 8}
    mock_cap.assert_called_once_with(session)
    session.close.assert_called_once()


@patch("app.core.database.SessionLocal")
@patch("app.services.price_capture.capture_fund_navs", return_value=8)
def test_backfill_fund_navs_task_no_codes_is_noop(
    mock_cap: MagicMock, mock_session_cls: MagicMock
) -> None:
    from app.tasks.capture_tasks import backfill_fund_navs_task

    result = backfill_fund_navs_task.run()
    assert result == {"written": 0}
    mock_cap.assert_not_called()


@patch("app.core.database.SessionLocal")
@patch("app.services.price_capture.capture_fund_navs", return_value=8)
def test_backfill_fund_navs_task_passes_codes_and_scheduled_lookback(
    mock_cap: MagicMock, mock_session_cls: MagicMock
) -> None:
    """Confirm-time NAV pull is 30 days, not the ticker path's 420 (#196)."""
    from app.tasks.capture_tasks import backfill_fund_navs_task

    session = MagicMock()
    mock_session_cls.return_value = session
    result = backfill_fund_navs_task.run(["513100"])
    assert result == {"written": 8}
    mock_cap.assert_called_once_with(session, lookback_days=30, fund_codes=["513100"])
    session.close.assert_called_once()


@patch("app.core.database.SessionLocal")
@patch("app.services.price_capture.capture_fund_navs", return_value=0)
def test_backfill_fund_navs_task_zero_writes_is_retryable(
    mock_cap: MagicMock, mock_session_cls: MagicMock
) -> None:
    """lsjz swallowing every error used to SUCCESS with written=0 and no alert."""
    from app.tasks.capture_tasks import backfill_fund_navs_task

    session = MagicMock()
    mock_session_cls.return_value = session
    with pytest.raises(RuntimeError, match="0 bars"):
        backfill_fund_navs_task.run(["513100"])
    mock_cap.assert_called_once()
    session.close.assert_called_once()


def test_backfill_sectors_task_no_ids_is_noop() -> None:
    from app.tasks.capture_tasks import backfill_sectors_task

    with patch("app.core.database.SessionLocal") as mock_session_cls:
        result = backfill_sectors_task.run()
    assert result == {"updated": 0}
    mock_session_cls.assert_not_called()


def test_backfill_sectors_task_ids_without_user_id_is_noop() -> None:
    from app.tasks.capture_tasks import backfill_sectors_task

    with patch("app.core.database.SessionLocal") as mock_session_cls:
        result = backfill_sectors_task.run(["00000000-0000-0000-0000-000000000001"])
    assert result == {"updated": 0}
    mock_session_cls.assert_not_called()
