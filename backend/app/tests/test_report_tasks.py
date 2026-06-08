"""Tests for Celery report tasks (Stage H).

Strategy:
- Task logic (DB session lifecycle, retry on error) is tested by calling the
  underlying function directly — avoids spinning up a real Celery worker.
- Beat schedule structure is verified at import time (no worker needed).
"""

from __future__ import annotations

import uuid
from datetime import date
from unittest.mock import MagicMock, patch

import pytest

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


def test_beat_schedule_crontab_mwf_1630() -> None:
    from celery.schedules import crontab  # type: ignore[import-untyped]

    entry = celery_app.conf.beat_schedule["report-incremental-mwf"]
    sched = entry["schedule"]
    assert isinstance(sched, crontab)
    # Mon/Wed/Fri = {1, 3, 5} in crontab internals.
    assert {1, 3, 5} <= sched.day_of_week
    assert 16 in sched.hour
    assert 30 in sched.minute


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


@patch("app.core.database.SessionLocal")
@patch("app.services.report_generator.generate_report")
def test_task_happy_path(mock_gen: MagicMock, mock_session_cls: MagicMock) -> None:
    report_id = uuid.uuid4()
    mock_gen.return_value = _make_report(report_id)
    mock_session = MagicMock()
    mock_session_cls.return_value = mock_session

    from app.tasks.report_tasks import generate_incremental_report

    result = generate_incremental_report.run()  # .run() bypasses Celery routing

    assert result["report_id"] == str(report_id)
    assert result["status"] == "success"
    mock_session.close.assert_called_once()


@patch("app.core.database.SessionLocal")
@patch("app.services.report_generator.generate_report")
def test_task_closes_session_on_success(mock_gen: MagicMock, mock_session_cls: MagicMock) -> None:
    mock_gen.return_value = _make_report()
    mock_session = MagicMock()
    mock_session_cls.return_value = mock_session

    from app.tasks.report_tasks import generate_incremental_report

    generate_incremental_report.run()

    mock_session.close.assert_called_once()


@patch("app.core.database.SessionLocal")
@patch("app.services.report_generator.generate_report")
def test_task_closes_session_on_failure(mock_gen: MagicMock, mock_session_cls: MagicMock) -> None:
    mock_gen.side_effect = RuntimeError("LLM down")
    mock_session = MagicMock()
    mock_session_cls.return_value = mock_session

    from app.tasks.report_tasks import generate_incremental_report

    with pytest.raises(RuntimeError):
        generate_incremental_report.run()

    mock_session.close.assert_called_once()
