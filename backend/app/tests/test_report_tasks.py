"""Tests for Celery report tasks (Stage H).

Strategy:
- Task logic (DB session lifecycle, retry on error, multi-user fan-out) is
  tested by calling the underlying function directly — avoids spinning up a
  real Celery worker.
- Beat schedule structure is verified at import time (no worker needed).

Multi-user fan-out (issue #128 A1): generate_incremental_report used to call
generate_report() exactly once, under the fixed DEV_USER_ID. It now fans out
over app.services.user_scope.active_users, generating one report per user
with per-user failure isolation — see the "fan-out" section below. SessionLocal
is mocked wholesale in every test in this file (no real DB); the real-DB,
real-anomaly-detection end-to-end fan-out behavior (no cross-user leakage,
shared moves_cache) is covered separately in test_shared_compute_a1.py.

Issue #308: the fan-out reads `active_users` (full `User` rows, not
`active_user_ids`'s bare ids) so each recipient's own `locale` (report
language) rides along without a second per-user lookup on the same
session — see user_scope.py's `active_users` docstring for why an
interleaved second read on that session hung indefinitely under a
real-session test pattern this file's own mocking doesn't use. Mocked
recipients here are `SimpleNamespace(id=..., locale=...)` stand-ins (see
`_active_user` below), not bare UUIDs.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from types import SimpleNamespace
from typing import Any
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
        "cadence": "mwf",
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


# --- weekly cadence (issue #191) --------------------------------------------


def test_beat_schedule_registered_weekly() -> None:
    schedule = celery_app.conf.beat_schedule
    assert "report-incremental-weekly" in schedule


def test_beat_schedule_weekly_task_name() -> None:
    entry = celery_app.conf.beat_schedule["report-incremental-weekly"]
    assert entry["task"] == "app.tasks.report_tasks.generate_incremental_report"


def test_beat_schedule_weekly_passes_report_type_session_node_and_cadence() -> None:
    entry = celery_app.conf.beat_schedule["report-incremental-weekly"]
    assert entry["kwargs"] == {
        "report_type": "incremental",
        "session_node": "weekend_snapshot",
        "cadence": "weekly",
        "trigger_hour": 19,
        "trigger_minute": 0,
    }


def test_beat_schedule_crontab_weekly_saturday_1900() -> None:
    """No `_node_cron`/nowfun wrapper needed — `celery_app.conf.timezone` is
    already "America/New_York" (see test_celery_timezone_is_et), so a plain
    crontab here is interpreted in ET the same way the mwf row already is,
    DST included."""
    from celery.schedules import crontab

    entry = celery_app.conf.beat_schedule["report-incremental-weekly"]
    sched = entry["schedule"]
    assert isinstance(sched, crontab)
    # Saturday = {6} in crontab internals.
    assert {6} <= sched.day_of_week
    assert 19 in sched.hour
    assert 0 in sched.minute


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


def _active_user(
    user_id: uuid.UUID, locale: str = "zh", base_currency: str = "USD"
) -> SimpleNamespace:
    """Stand-in for the `User` ORM rows `active_users` now returns (issue
    #308; `base_currency` added issue #350 item 1) — `report_tasks.py`
    reads `.id`, `.locale`, and `.base_currency` straight off each object,
    no bare-UUID list and no second per-user lookup."""
    return SimpleNamespace(id=user_id, locale=locale, base_currency=base_currency)


@patch("app.services.user_scope.active_users")
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


@patch("app.services.user_scope.active_users")
@patch("app.tasks.report_tasks.send_ops_alert")
@patch("app.core.database.SessionLocal")
@patch("app.services.report_generator.generate_report")
def test_task_fans_out_over_every_active_user(
    mock_gen: MagicMock,
    mock_session_cls: MagicMock,
    mock_alert: MagicMock,
    mock_active_users: MagicMock,
) -> None:
    mock_active_users.return_value = [_active_user(_U1), _active_user(_U2), _active_user(_U3)]
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


@patch("app.services.user_scope.active_users")
@patch("app.tasks.report_tasks.send_ops_alert")
@patch("app.core.database.SessionLocal")
@patch("app.services.report_generator.generate_report")
def test_task_reads_each_recipients_own_report_language(
    mock_gen: MagicMock,
    mock_session_cls: MagicMock,
    mock_alert: MagicMock,
    mock_active_users: MagicMock,
) -> None:
    """Issue #308: the whole reason this issue exists for scheduled (not
    just self-service) reports — each recipient's OWN users.locale must
    drive their output_lang, not one shared Settings.OUTPUT_LANG default
    for the entire batch. Read straight off the `active_users` row, not a
    second per-user session lookup (see module docstring)."""
    mock_active_users.return_value = [
        _active_user(_U1, locale="en"),
        _active_user(_U2, locale="zh"),
    ]
    mock_session_cls.return_value = MagicMock()
    mock_gen.side_effect = lambda session, **kw: _make_report(kw["user_id"])

    from app.tasks.report_tasks import generate_incremental_report

    generate_incremental_report.run()

    output_langs = {c.kwargs["user_id"]: c.kwargs["output_lang"] for c in mock_gen.call_args_list}
    assert output_langs[_U1] == "en"
    assert output_langs[_U2] == "zh"


@patch("app.services.user_scope.active_users")
@patch("app.tasks.report_tasks.send_ops_alert")
@patch("app.core.database.SessionLocal")
@patch("app.services.report_generator.generate_report")
def test_task_reads_each_recipients_own_report_currency(
    mock_gen: MagicMock,
    mock_session_cls: MagicMock,
    mock_alert: MagicMock,
    mock_active_users: MagicMock,
) -> None:
    """Issue #350 item 1: same reasoning as the report-language test above
    — each recipient's OWN users.base_currency must drive their
    generate_report base_currency, not a shared batch default."""
    mock_active_users.return_value = [
        _active_user(_U1, base_currency="CNY"),
        _active_user(_U2, base_currency="HKD"),
    ]
    mock_session_cls.return_value = MagicMock()
    mock_gen.side_effect = lambda session, **kw: _make_report(kw["user_id"])

    from app.tasks.report_tasks import generate_incremental_report

    generate_incremental_report.run()

    base_currencies = {
        c.kwargs["user_id"]: c.kwargs["base_currency"] for c in mock_gen.call_args_list
    }
    assert base_currencies[_U1] == "CNY"
    assert base_currencies[_U2] == "HKD"


@patch("app.services.user_scope.active_users")
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
    mock_active_users.return_value = [_active_user(_U1), _active_user(_U2)]
    mock_session_cls.return_value = MagicMock()
    mock_gen.side_effect = lambda session, **kw: _make_report(kw["user_id"])

    from app.tasks.report_tasks import generate_incremental_report

    generate_incremental_report.run()

    caches = [c.kwargs["moves_cache"] for c in mock_gen.call_args_list]
    assert caches[0] is caches[1]


@patch("app.services.user_scope.active_users")
@patch("app.tasks.report_tasks.send_ops_alert")
@patch("app.core.database.SessionLocal")
@patch("app.services.report_generator.generate_report")
def test_task_tells_each_user_how_many_remain_in_the_batch(
    mock_gen: MagicMock,
    mock_session_cls: MagicMock,
    mock_alert: MagicMock,
    mock_active_users: MagicMock,
) -> None:
    """Issue #128 A4: the shared daily caps (L1 analyses, L2 inferences) are
    sliced by how many users still have to be served, so the first user in
    the fixed `active_users` order cannot spend the whole day's budget and
    starve the SAME later users every day — see shared_budget.py for why this
    pattern kept recurring across A1/A2/A3.

    Counts the current user too: 3, 2, 1 for a three-user batch, so the last
    one may spend everything still left rather than stranding it.
    """
    mock_active_users.return_value = [_active_user(_U1), _active_user(_U2), _active_user(_U3)]
    mock_session_cls.return_value = MagicMock()
    mock_gen.side_effect = lambda session, **kw: _make_report(kw["user_id"])

    from app.tasks.report_tasks import generate_incremental_report

    generate_incremental_report.run()

    assert [c.kwargs["users_remaining"] for c in mock_gen.call_args_list] == [3, 2, 1]


@patch("app.services.user_scope.active_users")
@patch("app.tasks.report_tasks.send_ops_alert")
@patch("app.core.database.SessionLocal")
@patch("app.services.report_generator.generate_report")
def test_task_countdown_is_unaffected_by_a_failing_user(
    mock_gen: MagicMock,
    mock_session_cls: MagicMock,
    mock_alert: MagicMock,
    mock_active_users: MagicMock,
) -> None:
    """A user whose report raises still consumed its turn — the countdown is
    a position in the batch, not a success counter, so a failure must not
    make the remaining users over- or under-claim their share."""
    mock_active_users.return_value = [_active_user(_U1), _active_user(_U2), _active_user(_U3)]
    mock_session_cls.return_value = MagicMock()

    def _side_effect(session: object, **kw: Any) -> object:
        if kw["user_id"] == _U1:
            raise RuntimeError("boom")
        return _make_report(kw["user_id"])

    mock_gen.side_effect = _side_effect

    from app.tasks.report_tasks import generate_incremental_report

    generate_incremental_report.run()

    assert [c.kwargs["users_remaining"] for c in mock_gen.call_args_list] == [3, 2, 1]


@patch("app.services.user_scope.active_users")
@patch("app.tasks.report_tasks.send_ops_alert")
@patch("app.core.database.SessionLocal")
@patch("app.services.report_generator.generate_report")
def test_task_stamps_one_now_shared_across_the_whole_batch(
    mock_gen: MagicMock,
    mock_session_cls: MagicMock,
    mock_alert: MagicMock,
    mock_active_users: MagicMock,
) -> None:
    """PR #151 review (blocking): moves_cache is keyed on the exact
    (period_start, period_end) window, and generate_report stamps a fresh
    row's period_end from its `now` argument. Passing the same moves_cache
    dict object (test above) is not sufficient by itself — every user's
    call must also receive the SAME `now`, or each independently-computed
    `datetime.now()` would land microseconds apart and the cache key would
    never collide in production. Same dict-object identity check as the
    moves_cache test above, applied to `now`."""
    mock_active_users.return_value = [_active_user(_U1), _active_user(_U2)]
    mock_session_cls.return_value = MagicMock()
    mock_gen.side_effect = lambda session, **kw: _make_report(kw["user_id"])

    from app.tasks.report_tasks import generate_incremental_report

    generate_incremental_report.run()

    nows = [c.kwargs["now"] for c in mock_gen.call_args_list]
    assert nows[0] is nows[1]


@patch("app.services.user_scope.active_users")
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
    mock_active_users.return_value = [_active_user(_U1), _active_user(_U2), _active_user(_U3)]
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


@patch("app.services.user_scope.active_users")
@patch("app.tasks.report_tasks.send_ops_alert")
@patch("app.core.database.SessionLocal")
@patch("app.services.report_generator.generate_report")
def test_task_retries_the_whole_batch_when_every_user_fails(
    mock_gen: MagicMock,
    mock_session_cls: MagicMock,
    mock_alert: MagicMock,
    mock_active_users: MagicMock,
) -> None:
    """PR #151 review (non-blocking, but real for today's single-user
    production): per-user isolation must not silently swallow the task's
    documented 3x / 5-minute Celery retry when EVERY user in the batch
    fails — with one active user (today's production), this is functionally
    the pre-A1 single-user failure path, which used to retry. `.run()`
    bypasses Celery routing, so `self.retry()` re-raises the original
    exception rather than scheduling a real retry."""
    mock_active_users.return_value = [_active_user(_U1)]
    mock_session_cls.return_value = MagicMock()
    mock_gen.side_effect = RuntimeError("LLM down")

    from app.tasks.report_tasks import generate_incremental_report

    # Force the exhaustion branch (mirrors test_task_batch_failure_retries_
    # and_alerts_on_exhaustion) so both the per-user alert AND the batch-level
    # exhaustion alert fire in this one call, proving per-user isolation and
    # the retry escalation both still work together rather than one replacing
    # the other.
    with (
        patch.object(generate_incremental_report, "max_retries", 0),
        pytest.raises(RuntimeError),
    ):
        generate_incremental_report.run()

    mock_gen.assert_called_once()
    assert mock_alert.call_count == 2
    subjects = [c.kwargs["subject"] for c in mock_alert.call_args_list]
    assert any("FAILED for one user" in s for s in subjects)
    assert any("batch FAILED" in s for s in subjects)


@patch("app.services.user_scope.active_users")
@patch("app.tasks.report_tasks.send_ops_alert")
@patch("app.core.database.SessionLocal")
@patch("app.services.report_generator.generate_report")
def test_task_needs_review_sends_ops_alert_per_user(
    mock_gen: MagicMock,
    mock_session_cls: MagicMock,
    mock_alert: MagicMock,
    mock_active_users: MagicMock,
) -> None:
    mock_active_users.return_value = [_active_user(_U1)]
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


@patch("app.services.user_scope.active_users")
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
    mock_active_users.return_value = [_active_user(_U1)]
    mock_session_cls.return_value = MagicMock()
    mock_gen.return_value = _make_report(_U1)

    from app.tasks.report_tasks import generate_incremental_report

    generate_incremental_report.run(report_type="weekly", session_node="weekly_close")

    assert mock_gen.call_args.kwargs["report_type"] == "weekly"
    assert mock_gen.call_args.kwargs["session_node"] == "weekly_close"


@patch("app.services.user_scope.active_users")
@patch("app.tasks.report_tasks.send_ops_alert")
@patch("app.core.database.SessionLocal")
@patch("app.services.report_generator.generate_report")
def test_task_closes_session_on_success(
    mock_gen: MagicMock,
    mock_session_cls: MagicMock,
    mock_alert: MagicMock,
    mock_active_users: MagicMock,
) -> None:
    mock_active_users.return_value = [_active_user(_U1)]
    mock_gen.return_value = _make_report(_U1)
    mock_session = MagicMock()
    mock_session_cls.return_value = mock_session

    from app.tasks.report_tasks import generate_incremental_report

    generate_incremental_report.run()

    mock_session.close.assert_called_once()


@patch("app.services.user_scope.active_users")
@patch("app.tasks.report_tasks.send_ops_alert")
@patch("app.core.database.SessionLocal")
@patch("app.services.report_generator.generate_report")
def test_task_closes_session_after_a_batch_level_failure(
    mock_gen: MagicMock,
    mock_session_cls: MagicMock,
    mock_alert: MagicMock,
    mock_active_users: MagicMock,
) -> None:
    """A failure OUTSIDE the per-user loop (active_users itself) is a
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


@patch("app.services.user_scope.active_users")
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
# it returns before SessionLocal/active_users are ever touched.
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


@patch("app.services.user_scope.active_users")
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
    mock_active_users.return_value = [_active_user(_U1)]
    mock_gen.return_value = _make_report(_U1)
    mock_session_cls.return_value = MagicMock()
    mock_datetime.now.return_value = datetime(2026, 6, 30, 17, 4, tzinfo=ET)

    from app.tasks.report_tasks import generate_incremental_report

    result = generate_incremental_report.run(trigger_hour=17, trigger_minute=0)

    assert result["status"] == "completed"
    mock_gen.assert_called_once()
    mock_alert.assert_not_called()
