"""Tests for Celery report tasks (Stage H).

Strategy:
- Task logic (DB session lifecycle, retry on error, multi-user fan-out) is
  tested by calling the underlying function directly — avoids spinning up a
  real Celery worker.
- Beat schedule structure is verified at import time (no worker needed).

Multi-user fan-out (issue #128 A1): generate_incremental_report used to call
generate_report() exactly once, under the fixed DEV_USER_ID. It now fans out
over app.services.user_scope.active_user_ids, generating one report per user
with per-user failure isolation — see the "fan-out" section below. SessionLocal
is mocked wholesale in every test in this file (no real DB); the real-DB,
real-anomaly-detection end-to-end fan-out behavior (no cross-user leakage,
shared moves_cache) is covered separately in test_shared_compute_a1.py.
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

_U1 = uuid.UUID("00000000-0000-0000-0000-0000000000c1")
_U2 = uuid.UUID("00000000-0000-0000-0000-0000000000c2")
_U3 = uuid.UUID("00000000-0000-0000-0000-0000000000c3")


def _make_report(user_id: uuid.UUID, report_id: uuid.UUID | None = None) -> MagicMock:
    r = MagicMock()
    r.id = report_id or uuid.uuid4()
    r.user_id = user_id
    r.status = "success"
    r.report_date = date(2026, 6, 6)
    return r


@patch("app.services.user_scope.active_user_ids")
@patch("app.tasks.report_tasks.send_ops_alert")
@patch("app.core.database.SessionLocal")
@patch("app.services.report_generator.generate_report")
def test_task_no_active_users(
    mock_gen: MagicMock,
    mock_session_cls: MagicMock,
    mock_alert: MagicMock,
    mock_active_users: MagicMock,
) -> None:
    mock_active_users.return_value = []
    mock_session_cls.return_value = MagicMock()

    from app.tasks.report_tasks import generate_incremental_report

    result = generate_incremental_report.run()

    assert result == {"status": "no_active_users", "results": []}
    mock_gen.assert_not_called()
    mock_alert.assert_not_called()


@patch("app.services.user_scope.active_user_ids")
@patch("app.tasks.report_tasks.send_ops_alert")
@patch("app.core.database.SessionLocal")
@patch("app.services.report_generator.generate_report")
def test_task_fans_out_over_every_active_user(
    mock_gen: MagicMock,
    mock_session_cls: MagicMock,
    mock_alert: MagicMock,
    mock_active_users: MagicMock,
) -> None:
    mock_active_users.return_value = [_U1, _U2, _U3]
    mock_session_cls.return_value = MagicMock()
    mock_gen.side_effect = lambda session, **kw: _make_report(kw["user_id"])

    from app.tasks.report_tasks import generate_incremental_report

    result = generate_incremental_report.run()

    assert result["status"] == "completed"
    assert {r["user_id"] for r in result["results"]} == {str(_U1), str(_U2), str(_U3)}
    assert all(r["status"] == "success" for r in result["results"])
    assert mock_gen.call_count == 3
    called_user_ids = {c.kwargs["user_id"] for c in mock_gen.call_args_list}
    assert called_user_ids == {_U1, _U2, _U3}
    mock_alert.assert_not_called()


@patch("app.services.user_scope.active_user_ids")
@patch("app.tasks.report_tasks.send_ops_alert")
@patch("app.core.database.SessionLocal")
@patch("app.services.report_generator.generate_report")
def test_task_shares_one_moves_cache_across_the_whole_batch(
    mock_gen: MagicMock,
    mock_session_cls: MagicMock,
    mock_alert: MagicMock,
    mock_active_users: MagicMock,
) -> None:
    """The same moves_cache dict object must be passed to every user's
    generate_report call — this is the plumbing UAT-2 (design doc §7.2)
    depends on for compute_global_moves() to run once per window."""
    mock_active_users.return_value = [_U1, _U2]
    mock_session_cls.return_value = MagicMock()
    mock_gen.side_effect = lambda session, **kw: _make_report(kw["user_id"])

    from app.tasks.report_tasks import generate_incremental_report

    generate_incremental_report.run()

    caches = [c.kwargs["moves_cache"] for c in mock_gen.call_args_list]
    assert caches[0] is caches[1]


@patch("app.services.user_scope.active_user_ids")
@patch("app.tasks.report_tasks.send_ops_alert")
@patch("app.core.database.SessionLocal")
@patch("app.services.report_generator.generate_report")
def test_task_single_user_failure_isolated_from_batch(
    mock_gen: MagicMock,
    mock_session_cls: MagicMock,
    mock_alert: MagicMock,
    mock_active_users: MagicMock,
) -> None:
    """UAT-3 (design doc §7.2): one user's generation failure must not stop
    or retry the rest of the batch — the other users still get reports, and
    exactly one ops alert fires for the failed user."""
    mock_active_users.return_value = [_U1, _U2, _U3]
    mock_session = MagicMock()
    mock_session_cls.return_value = mock_session

    def _gen(session: object, **kw: object) -> MagicMock:
        if kw["user_id"] == _U2:
            raise RuntimeError("LLM down for U2")
        return _make_report(kw["user_id"])  # type: ignore[arg-type]

    mock_gen.side_effect = _gen

    from app.tasks.report_tasks import generate_incremental_report

    result = generate_incremental_report.run()

    assert result["status"] == "completed"
    by_user = {r["user_id"]: r["status"] for r in result["results"]}
    assert by_user[str(_U1)] == "success"
    assert by_user[str(_U2)] == "failed"
    assert by_user[str(_U3)] == "success"
    assert mock_gen.call_count == 3  # U3 still attempted despite U2's failure
    mock_alert.assert_called_once()
    assert "FAILED for one user" in mock_alert.call_args.kwargs["subject"]
    mock_session.rollback.assert_called_once()


@patch("app.services.user_scope.active_user_ids")
@patch("app.tasks.report_tasks.send_ops_alert")
@patch("app.core.database.SessionLocal")
@patch("app.services.report_generator.generate_report")
def test_task_needs_review_sends_ops_alert_per_user(
    mock_gen: MagicMock,
    mock_session_cls: MagicMock,
    mock_alert: MagicMock,
    mock_active_users: MagicMock,
) -> None:
    mock_active_users.return_value = [_U1]
    mock_session_cls.return_value = MagicMock()
    report = _make_report(_U1)
    report.status = "needs_review"
    mock_gen.return_value = report

    from app.tasks.report_tasks import generate_incremental_report

    result = generate_incremental_report.run()

    assert result["results"][0]["status"] == "needs_review"
    mock_alert.assert_called_once()
    subject = mock_alert.call_args.kwargs["subject"]
    assert "BLOCKED" in subject or "needs_review" in subject or "compliance" in subject.lower()


@patch("app.services.user_scope.active_user_ids")
@patch("app.tasks.report_tasks.send_ops_alert")
@patch("app.core.database.SessionLocal")
@patch("app.services.report_generator.generate_report")
def test_task_uses_report_type_and_session_node_from_beat_kwargs(
    mock_gen: MagicMock,
    mock_session_cls: MagicMock,
    mock_alert: MagicMock,
    mock_active_users: MagicMock,
) -> None:
    """A future cadence (e.g. Ring 1 weekly) passes its own report_type/session_node
    via beat kwargs rather than the task hardcoding "incremental"/"after_close"."""
    mock_active_users.return_value = [_U1]
    mock_session_cls.return_value = MagicMock()
    mock_gen.return_value = _make_report(_U1)

    from app.tasks.report_tasks import generate_incremental_report

    generate_incremental_report.run(report_type="weekly", session_node="weekly_close")

    assert mock_gen.call_args.kwargs["report_type"] == "weekly"
    assert mock_gen.call_args.kwargs["session_node"] == "weekly_close"


@patch("app.services.user_scope.active_user_ids")
@patch("app.tasks.report_tasks.send_ops_alert")
@patch("app.core.database.SessionLocal")
@patch("app.services.report_generator.generate_report")
def test_task_closes_session_on_success(
    mock_gen: MagicMock,
    mock_session_cls: MagicMock,
    mock_alert: MagicMock,
    mock_active_users: MagicMock,
) -> None:
    mock_active_users.return_value = [_U1]
    mock_gen.return_value = _make_report(_U1)
    mock_session = MagicMock()
    mock_session_cls.return_value = mock_session

    from app.tasks.report_tasks import generate_incremental_report

    generate_incremental_report.run()

    mock_session.close.assert_called_once()


@patch("app.services.user_scope.active_user_ids")
@patch("app.tasks.report_tasks.send_ops_alert")
@patch("app.core.database.SessionLocal")
@patch("app.services.report_generator.generate_report")
def test_task_closes_session_after_a_batch_level_failure(
    mock_gen: MagicMock,
    mock_session_cls: MagicMock,
    mock_alert: MagicMock,
    mock_active_users: MagicMock,
) -> None:
    """A failure OUTSIDE the per-user loop (active_user_ids itself) is a
    batch-level failure — still closes the session and still retries via
    self.retry, matching the pre-A1 single-user behavior for this class of
    error."""
    mock_active_users.side_effect = RuntimeError("DB down")
    mock_session = MagicMock()
    mock_session_cls.return_value = mock_session

    from app.tasks.report_tasks import generate_incremental_report

    with pytest.raises(RuntimeError):
        generate_incremental_report.run()

    mock_gen.assert_not_called()
    mock_session.close.assert_called_once()


@patch("app.services.user_scope.active_user_ids")
@patch("app.tasks.report_tasks.create_bug_report")
@patch("app.tasks.report_tasks.send_ops_alert")
@patch("app.core.database.SessionLocal")
@patch("app.services.report_generator.generate_report")
def test_task_batch_failure_retries_and_alerts_on_exhaustion(
    mock_gen: MagicMock,
    mock_session_cls: MagicMock,
    mock_alert: MagicMock,
    mock_bug_report: MagicMock,
    mock_active_users: MagicMock,
) -> None:
    mock_active_users.side_effect = RuntimeError("DB down")
    mock_session_cls.return_value = MagicMock()

    from app.tasks.report_tasks import generate_incremental_report

    # .run() bypasses Celery's retry machinery, so self.retry() re-raises the
    # original exception rather than actually scheduling a retry.
    with patch.object(generate_incremental_report, "max_retries", 0), pytest.raises(RuntimeError):
        generate_incremental_report.run()

    mock_alert.assert_called_once()
    assert "batch FAILED" in mock_alert.call_args.kwargs["subject"]
    mock_bug_report.assert_called_once()


# ---------------------------------------------------------------------------
# Stale Beat catch-up guard (issue #71) — unaffected by the fan-out change,
# it returns before SessionLocal/active_user_ids are ever touched.
# ---------------------------------------------------------------------------


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


@patch("app.services.user_scope.active_user_ids")
@patch("app.tasks.report_tasks.datetime")
@patch("app.tasks.report_tasks.send_ops_alert")
@patch("app.core.database.SessionLocal")
@patch("app.services.report_generator.generate_report")
def test_task_runs_when_close_to_trigger_time(
    mock_gen: MagicMock,
    mock_session_cls: MagicMock,
    mock_alert: MagicMock,
    mock_datetime: MagicMock,
    mock_active_users: MagicMock,
) -> None:
    """A few minutes of normal jitter around the intended fire time is not
    a stale Beat catch-up and must still generate/email as usual."""
    mock_active_users.return_value = [_U1]
    mock_gen.return_value = _make_report(_U1)
    mock_session_cls.return_value = MagicMock()
    mock_datetime.now.return_value = datetime(2026, 6, 30, 17, 4, tzinfo=ET)

    from app.tasks.report_tasks import generate_incremental_report

    result = generate_incremental_report.run(trigger_hour=17, trigger_minute=0)

    assert result["status"] == "completed"
    mock_gen.assert_called_once()
    mock_alert.assert_not_called()
