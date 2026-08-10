"""Tests for the daily backup Celery task + its Beat schedule entry (issue #106).

send_ops_alert/create_bug_report are mocked globally by the autouse
_no_external_notifications fixture in conftest.py — app.tasks.backup_tasks
is registered there. Tests that assert on call args re-patch within a `with`
block, matching the pattern used by other task test modules.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from app.tasks import celery_app


def test_backup_schedule_entry_exists() -> None:
    sched = celery_app.conf.beat_schedule
    assert "backup-database-daily" in sched
    entry = sched["backup-database-daily"]
    assert entry["task"] == "app.tasks.backup_tasks.backup_database_task"


def test_backup_schedule_is_picklable() -> None:
    import pickle

    entry = celery_app.conf.beat_schedule["backup-database-daily"]
    pickle.dumps(entry["schedule"])


@patch("app.services.db_backup.backup_database", return_value="daily/portfonia_prod-x.dump")
def test_backup_database_task_success(mock_backup: MagicMock) -> None:
    from app.tasks.backup_tasks import backup_database_task

    result = backup_database_task.run()
    assert result == {"object_name": "daily/portfonia_prod-x.dump"}


@patch("app.services.db_backup.backup_database", return_value=None)
def test_backup_database_task_returns_none_when_disabled(mock_backup: MagicMock) -> None:
    from app.tasks.backup_tasks import backup_database_task

    result = backup_database_task.run()
    assert result == {"object_name": None}


@patch("app.services.db_backup.backup_database", side_effect=RuntimeError("pg_dump exploded"))
def test_backup_database_task_retries_then_alerts_on_exhaustion(mock_backup: MagicMock) -> None:
    """.apply() (eager mode) loops through all retries synchronously within
    one call — unlike a real worker, where each retry is a separate message
    with retries persisted across invocations. So a single .apply() here
    already exhausts max_retries=2 and should hit _backup_failed exactly once."""
    from app.tasks.backup_tasks import backup_database_task

    with patch("app.tasks.backup_tasks._backup_failed") as mock_failed:
        result = backup_database_task.apply(throw=False)

    assert result.failed()
    assert "pg_dump exploded" in str(result.result)
    mock_failed.assert_called_once()


def test_backup_failed_sends_ops_alert_and_creates_issue() -> None:
    from app.tasks.backup_tasks import _backup_failed

    with (
        patch("app.tasks.backup_tasks.send_ops_alert") as mock_alert,
        patch("app.tasks.backup_tasks.create_bug_report") as mock_issue,
    ):
        _backup_failed(RuntimeError("disk full"))

    mock_alert.assert_called_once()
    assert "disk full" in mock_alert.call_args.kwargs["body"]
    mock_issue.assert_called_once()
    assert mock_issue.call_args.kwargs["labels"] == ["bug", "ops", "backup"]
