"""Daily intelligence-record retention sweep (issue #620/#639; the shared-intel
cache part of this task was removed in issue #640)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.intel import IntelSlotRun
from app.tasks import cache_tasks, celery_app

_EMPTY_COUNTS = {
    "news_deleted": 0,
    "news_surfaced_deleted": 0,
    "intel_articles_deleted": 0,
    "paid_api_usage_deleted": 0,
    "intel_collection_runs_deleted": 0,
    "intel_slot_runs_deleted": 0,
}


def test_cleanup_expired_runs_intel_retention_and_commits(db_session: Session) -> None:
    now = datetime.now(UTC)
    db_session.add_all(
        [
            IntelSlotRun(
                slot="pre_open",
                run_date=(now - timedelta(days=100)).date(),
                started_at=now - timedelta(days=100),
                status="ok",
                details={},
            ),
            IntelSlotRun(
                slot="pre_open",
                run_date=(now - timedelta(days=10)).date(),
                started_at=now - timedelta(days=10),
                status="ok",
                details={},
            ),
        ]
    )
    db_session.flush()

    result = cache_tasks._cleanup_expired(db_session)

    assert result == {**_EMPTY_COUNTS, "intel_slot_runs_deleted": 1}
    remaining = db_session.execute(select(IntelSlotRun)).scalars().all()
    assert len(remaining) == 1


@patch("app.core.database.SessionLocal")
def test_task_commits_and_closes_session(mock_session_cls: MagicMock) -> None:
    mock_session = MagicMock()
    mock_session.execute.return_value.rowcount = 0
    mock_session_cls.return_value = mock_session

    result = cache_tasks.sweep_stale_shared_intel_cache.run()

    assert result == _EMPTY_COUNTS
    mock_session.commit.assert_called_once()
    mock_session.close.assert_called_once()


@patch("app.tasks.cache_tasks.send_ops_alert")
@patch("app.core.database.SessionLocal")
def test_task_retries_and_alerts_on_exhaustion(
    mock_session_cls: MagicMock, mock_alert: MagicMock
) -> None:
    """Round 2 review finding: unlike backup/capture beat tasks, the sweep
    had no try/except/retry/ops-alert at all — a silent sweep outage lets
    the tables grow without bound, the exact failure mode this task exists
    to prevent. Matches backup_database_task's pattern: catch,
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
    assert mock_alert.call_args.kwargs["severity"] == "ALERT"
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
