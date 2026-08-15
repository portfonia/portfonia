"""90-day retention sweep for the L1 shared-intel caches (issue #128 A2 —
design doc §4.4)."""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import MagicMock, patch

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.search_cache import SearchCache
from app.models.ticker_intel import TickerIntel
from app.tasks import cache_tasks, celery_app

_TODAY = date(2026, 8, 15)
_OLD = _TODAY - timedelta(days=91)
_RECENT = _TODAY - timedelta(days=10)


def _seed(db_session: Session) -> None:
    db_session.add_all(
        [
            TickerIntel(
                identifier="NVDA",
                trade_date=_OLD,
                prompt_version="l1-v1",
                model="x",
                analysis="old",
                facts={},
            ),
            TickerIntel(
                identifier="NVDA",
                trade_date=_RECENT,
                prompt_version="l1-v1",
                model="x",
                analysis="recent",
                facts={},
            ),
            SearchCache(query_hash="h1", query="q1", trade_date=_OLD, results=[]),
            SearchCache(query_hash="h2", query="q2", trade_date=_RECENT, results=[]),
        ]
    )
    db_session.flush()


def test_cleanup_expired_deletes_only_rows_older_than_90_days(db_session: Session) -> None:
    _seed(db_session)
    cutoff = _TODAY - timedelta(days=90)

    result = cache_tasks._cleanup_expired(db_session, cutoff)

    assert result == {"ticker_intel_deleted": 1, "search_cache_deleted": 1}
    remaining_ti = db_session.execute(select(TickerIntel)).scalars().all()
    remaining_sc = db_session.execute(select(SearchCache)).scalars().all()
    assert [r.trade_date for r in remaining_ti] == [_RECENT]
    assert [r.trade_date for r in remaining_sc] == [_RECENT]


def test_cleanup_expired_noop_when_nothing_is_stale(db_session: Session) -> None:
    db_session.add(
        TickerIntel(
            identifier="NVDA",
            trade_date=_RECENT,
            prompt_version="l1-v1",
            model="x",
            analysis="recent",
            facts={},
        )
    )
    db_session.flush()
    cutoff = _TODAY - timedelta(days=90)

    result = cache_tasks._cleanup_expired(db_session, cutoff)

    assert result == {"ticker_intel_deleted": 0, "search_cache_deleted": 0}


@patch("app.core.database.SessionLocal")
def test_task_computes_cutoff_and_closes_session(mock_session_cls: MagicMock) -> None:
    mock_session = MagicMock()
    mock_session.execute.return_value.rowcount = 0
    mock_session_cls.return_value = mock_session

    result = cache_tasks.sweep_stale_shared_intel_cache.run()

    assert result == {"ticker_intel_deleted": 0, "search_cache_deleted": 0}
    mock_session.commit.assert_called_once()
    mock_session.close.assert_called_once()


@patch("app.tasks.cache_tasks.send_ops_alert")
@patch("app.core.database.SessionLocal")
def test_task_retries_and_alerts_on_exhaustion(
    mock_session_cls: MagicMock, mock_alert: MagicMock
) -> None:
    """Round 2 review finding: unlike backup/capture beat tasks, the sweep
    had no try/except/retry/ops-alert at all — a silent sweep outage let
    both cache tables grow without bound, the exact failure mode this task
    exists to prevent. Matches backup_database_task's pattern: catch,
    retry, ops-alert on exhaustion, still close the session."""
    mock_session = MagicMock()
    mock_session.execute.side_effect = RuntimeError("DB down")
    mock_session_cls.return_value = mock_session

    import pytest

    with (
        patch.object(cache_tasks.sweep_stale_shared_intel_cache, "max_retries", 0),
        pytest.raises(RuntimeError),
    ):
        cache_tasks.sweep_stale_shared_intel_cache.run()

    mock_alert.assert_called_once()
    assert "sweep" in mock_alert.call_args.kwargs["subject"].lower()
    mock_session.close.assert_called_once()


# ---------------------------------------------------------------------------
# Beat schedule registration
# ---------------------------------------------------------------------------


def test_schedule_entry_exists() -> None:
    sched = celery_app.conf.beat_schedule
    assert "sweep-stale-shared-intel-cache-daily" in sched
    entry = sched["sweep-stale-shared-intel-cache-daily"]
    assert entry["task"] == "app.tasks.cache_tasks.sweep_stale_shared_intel_cache"


def test_schedule_is_picklable() -> None:
    import pickle

    entry = celery_app.conf.beat_schedule["sweep-stale-shared-intel-cache-daily"]
    pickle.dumps(entry["schedule"])


def test_schedule_fires_daily_at_0400_no_weekday_restriction() -> None:
    entry = celery_app.conf.beat_schedule["sweep-stale-shared-intel-cache-daily"]
    schedule = entry["schedule"]
    assert schedule.hour == {4}
    assert schedule.minute == {0}
    assert schedule.day_of_week == set(range(7))
