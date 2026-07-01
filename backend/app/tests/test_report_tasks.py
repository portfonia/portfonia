"""Tests for Celery report tasks (Stage H).

Strategy:
- Task logic (DB session lifecycle, retry on error) is tested by calling the
  underlying function directly — avoids spinning up a real Celery worker.
- Beat schedule structure is verified at import time (no worker needed).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from unittest.mock import MagicMock, patch

import pytest

from app.core.timezones import ET
from app.tasks import celery_app

# ---------------------------------------------------------------------------
# Beat schedule
# ---------------------------------------------------------------------------


def test_beat_schedule_registered() -> None:
    schedule = celery_app.conf.beat_schedule
    assert "report-incremental-mwf" in schedule


def test_beat_schedule_task_name() -> None:
    entry = celery_app.conf.beat_schedule["report-incremental-mwf"]
    assert entry["task"] == "app.tasks.report_tasks.generate_incremental_report"


def test_beat_schedule_passes_report_type_and_session_node() -> None:
    entry = celery_app.conf.beat_schedule["report-incremental-mwf"]
    assert entry["kwargs"] == {
        "report_type": "incremental",
        "session_node": "after_close",
        "trigger_hour": 17,
        "trigger_minute": 0,
    }


def test_beat_schedule_crontab_mwf_1700() -> None:
    from celery.schedules import crontab  # type: ignore[import-untyped]

    entry = celery_app.conf.beat_schedule["report-incremental-mwf"]
    sched = entry["schedule"]
    assert isinstance(sched, crontab)
    # Mon/Wed/Fri = {1, 3, 5} in crontab internals.
    assert {1, 3, 5} <= sched.day_of_week
    assert 17 in sched.hour
    assert 0 in sched.minute


def test_celery_timezone_is_et() -> None:
    assert celery_app.conf.timezone == "America/New_York"


# ---------------------------------------------------------------------------
# generate_incremental_report task logic
# ---------------------------------------------------------------------------


def _make_report(report_id: uuid.UUID | None = None) -> MagicMock:
    r = MagicMock()
    r.id = report_id or uuid.uuid4()
    r.status = "success"
    r.report_date = date(2026, 6, 6)
    return r


@patch("app.tasks.report_tasks.send_ops_alert")
@patch("app.core.database.SessionLocal")
@patch("app.services.report_generator.generate_report")
def test_task_happy_path(
    mock_gen: MagicMock, mock_session_cls: MagicMock, mock_alert: MagicMock
) -> None:
    report_id = uuid.uuid4()
    mock_gen.return_value = _make_report(report_id)
    mock_session = MagicMock()
    mock_session_cls.return_value = mock_session

    from app.tasks.report_tasks import generate_incremental_report

    result = generate_incremental_report.run()  # .run() bypasses Celery routing

    assert result["report_id"] == str(report_id)
    assert result["status"] == "success"
    mock_session.close.assert_called_once()
    mock_alert.assert_not_called()
    assert mock_gen.call_args.kwargs["report_type"] == "incremental"
    assert mock_gen.call_args.kwargs["session_node"] == "after_close"


@patch("app.tasks.report_tasks.datetime")
@patch("app.tasks.report_tasks.send_ops_alert")
@patch("app.core.database.SessionLocal")
@patch("app.services.report_generator.generate_report")
def test_task_skips_stale_beat_catchup(
    mock_gen: MagicMock,
    mock_session_cls: MagicMock,
    mock_alert: MagicMock,
    mock_datetime: MagicMock,
) -> None:
    """Issue #71: if Beat was down and fires the missed 17:00 ET tick hours
    late once it comes back, the task must skip — not silently generate and
    email a report for a run nobody scheduled at that moment."""
    mock_datetime.now.return_value = datetime(2026, 6, 30, 19, 46, tzinfo=ET)

    from app.tasks.report_tasks import generate_incremental_report

    result = generate_incremental_report.run(trigger_hour=17, trigger_minute=0)

    assert result == {"status": "skipped_stale_trigger"}
    mock_gen.assert_not_called()
    mock_alert.assert_called_once()
    assert "SKIPPED" in mock_alert.call_args.kwargs["subject"]


@patch("app.tasks.report_tasks.datetime")
@patch("app.tasks.report_tasks.send_ops_alert")
@patch("app.core.database.SessionLocal")
@patch("app.services.report_generator.generate_report")
def test_task_runs_when_close_to_trigger_time(
    mock_gen: MagicMock,
    mock_session_cls: MagicMock,
    mock_alert: MagicMock,
    mock_datetime: MagicMock,
) -> None:
    """A few minutes of normal jitter around the intended fire time is not
    a stale Beat catch-up and must still generate/email as usual."""
    mock_gen.return_value = _make_report()
    mock_session_cls.return_value = MagicMock()
    mock_datetime.now.return_value = datetime(2026, 6, 30, 17, 4, tzinfo=ET)

    from app.tasks.report_tasks import generate_incremental_report

    result = generate_incremental_report.run(trigger_hour=17, trigger_minute=0)

    assert result["status"] == "success"
    mock_gen.assert_called_once()
    mock_alert.assert_not_called()


@patch("app.tasks.report_tasks.send_ops_alert")
@patch("app.core.database.SessionLocal")
@patch("app.services.report_generator.generate_report")
def test_task_uses_report_type_and_session_node_from_beat_kwargs(
    mock_gen: MagicMock, mock_session_cls: MagicMock, mock_alert: MagicMock
) -> None:
    """A future cadence (e.g. Ring 1 weekly) passes its own report_type/session_node
    via beat kwargs rather than the task hardcoding "incremental"/"after_close"."""
    mock_gen.return_value = _make_report()
    mock_session_cls.return_value = MagicMock()

    from app.tasks.report_tasks import generate_incremental_report

    generate_incremental_report.run(report_type="weekly", session_node="weekly_close")

    assert mock_gen.call_args.kwargs["report_type"] == "weekly"
    assert mock_gen.call_args.kwargs["session_node"] == "weekly_close"


@patch("app.tasks.report_tasks.send_ops_alert")
@patch("app.core.database.SessionLocal")
@patch("app.services.report_generator.generate_report")
def test_task_needs_review_sends_ops_alert(
    mock_gen: MagicMock, mock_session_cls: MagicMock, mock_alert: MagicMock
) -> None:
    report = _make_report()
    report.status = "needs_review"
    mock_gen.return_value = report
    mock_session_cls.return_value = MagicMock()

    from app.tasks.report_tasks import generate_incremental_report

    result = generate_incremental_report.run()

    assert result["status"] == "needs_review"
    mock_alert.assert_called_once()
    subject = mock_alert.call_args.kwargs["subject"]
    assert "BLOCKED" in subject or "needs_review" in subject or "compliance" in subject.lower()


@patch("app.tasks.report_tasks.send_ops_alert")
@patch("app.core.database.SessionLocal")
@patch("app.services.report_generator.generate_report")
def test_task_closes_session_on_success(
    mock_gen: MagicMock, mock_session_cls: MagicMock, mock_alert: MagicMock
) -> None:
    mock_gen.return_value = _make_report()
    mock_session = MagicMock()
    mock_session_cls.return_value = mock_session

    from app.tasks.report_tasks import generate_incremental_report

    generate_incremental_report.run()

    mock_session.close.assert_called_once()


@patch("app.tasks.report_tasks.send_ops_alert")
@patch("app.core.database.SessionLocal")
@patch("app.services.report_generator.generate_report")
def test_task_closes_session_on_failure(
    mock_gen: MagicMock, mock_session_cls: MagicMock, mock_alert: MagicMock
) -> None:
    mock_gen.side_effect = RuntimeError("LLM down")
    mock_session = MagicMock()
    mock_session_cls.return_value = mock_session

    from app.tasks.report_tasks import generate_incremental_report

    with pytest.raises(RuntimeError):
        generate_incremental_report.run()

    mock_session.close.assert_called_once()
