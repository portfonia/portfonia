"""Tests for the async holdings-upload Celery task (issue #77 / PR #82 review).

Strategy mirrors test_report_tasks.py: task logic is tested by mocking
SessionLocal and calling the task's underlying function directly (`.run()`
bypasses Celery routing) rather than spinning up a real worker. SessionLocal
is lazy (issue #27) and refuses the dev DB under pytest; these tests still
mock it because they exercise control flow, not SQL.
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

from app.schemas.holdings import UploadPreview


def _make_job(
    job_id: uuid.UUID, raw_text: str | None = "some holdings text", status: str = "pending"
) -> MagicMock:
    job = MagicMock()
    job.id = job_id
    job.raw_text = raw_text
    job.status = status
    return job


@patch("app.services.holding_parser.parse")
@patch("app.core.database.SessionLocal")
def test_parse_holdings_upload_success(mock_session_cls: MagicMock, mock_parse: MagicMock) -> None:
    job_id = uuid.uuid4()
    job = _make_job(job_id)
    mock_session = MagicMock()
    mock_session.get.return_value = job
    mock_session_cls.return_value = mock_session
    preview = UploadPreview(valid_rows=[], issue_rows=[])
    mock_parse.return_value = preview

    from app.tasks.holdings_tasks import parse_holdings_upload

    result = parse_holdings_upload.run(str(job_id))  # bypasses Celery routing

    assert result == {"job_id": str(job_id), "status": "success"}
    assert job.status == "success"
    assert job.preview == preview.model_dump(mode="json")
    mock_parse.assert_called_once_with("some holdings text")
    # Cleared once the parse attempt is done with it, regardless of outcome —
    # a holdings file's content shouldn't linger on the row (PR #82 review).
    assert job.raw_text is None
    mock_session.commit.assert_called_once()
    mock_session.close.assert_called_once()


@patch("app.services.holding_parser.parse")
@patch("app.core.database.SessionLocal")
def test_parse_holdings_upload_records_runtime_error(
    mock_session_cls: MagicMock, mock_parse: MagicMock
) -> None:
    """holding_parser.parse() exhausting both attempts raises RuntimeError —
    the task must record it as a failed job, not let it propagate (there's
    no Celery-level retry for this interactive, user-facing task)."""
    job_id = uuid.uuid4()
    job = _make_job(job_id)
    mock_session = MagicMock()
    mock_session.get.return_value = job
    mock_session_cls.return_value = mock_session
    mock_parse.side_effect = RuntimeError("LLM call failed: boom")

    from app.tasks.holdings_tasks import parse_holdings_upload

    result = parse_holdings_upload.run(str(job_id))

    assert result == {"job_id": str(job_id), "status": "failed"}
    assert job.status == "failed"
    assert job.error == "LLM call failed: boom"
    assert job.raw_text is None
    mock_session.commit.assert_called_once()
    mock_session.close.assert_called_once()


@patch("app.services.holding_parser.parse")
@patch("app.core.database.SessionLocal")
def test_parse_holdings_upload_records_unexpected_error(
    mock_session_cls: MagicMock, mock_parse: MagicMock
) -> None:
    job_id = uuid.uuid4()
    job = _make_job(job_id)
    mock_session = MagicMock()
    mock_session.get.return_value = job
    mock_session_cls.return_value = mock_session
    mock_parse.side_effect = ValueError("unexpected")

    from app.tasks.holdings_tasks import parse_holdings_upload

    result = parse_holdings_upload.run(str(job_id))

    assert result == {"job_id": str(job_id), "status": "failed"}
    assert job.status == "failed"
    assert "ValueError" in job.error
    assert job.raw_text is None
    mock_session.commit.assert_called_once()


@patch("app.services.holding_parser.parse")
@patch("app.core.database.SessionLocal")
def test_parse_holdings_upload_missing_raw_text_records_failure(
    mock_session_cls: MagicMock, mock_parse: MagicMock
) -> None:
    """Defensive case: a job row with no raw_text (shouldn't happen — the
    router always sets it before enqueueing) must not call parse(None) or
    crash the task; it should just fail the job cleanly."""
    job_id = uuid.uuid4()
    job = _make_job(job_id, raw_text=None)
    mock_session = MagicMock()
    mock_session.get.return_value = job
    mock_session_cls.return_value = mock_session

    from app.tasks.holdings_tasks import parse_holdings_upload

    result = parse_holdings_upload.run(str(job_id))

    assert result == {"job_id": str(job_id), "status": "failed"}
    assert job.status == "failed"
    mock_parse.assert_not_called()
    mock_session.commit.assert_called_once()


@patch("app.services.holding_parser.parse")
@patch("app.core.database.SessionLocal")
def test_parse_holdings_upload_redelivery_after_success_is_idempotent(
    mock_session_cls: MagicMock, mock_parse: MagicMock
) -> None:
    """PR #82 second review: task_acks_late=True means a worker that dies
    after this task's own commit but before it acks the message gets the
    same message redelivered. By the time that happens, raw_text has
    already been cleared by the first (successful) run — a naive second run
    would see "no text" and overwrite the real success with a false
    failure, losing the preview. A job already in a terminal state must be
    left untouched, not re-enter the parse/error paths."""
    job_id = uuid.uuid4()
    preview_dump: dict[str, object] = {"valid_rows": [], "issue_rows": [], "broker_groups": []}
    job = _make_job(job_id, raw_text=None, status="success")
    job.preview = preview_dump
    mock_session = MagicMock()
    mock_session.get.return_value = job
    mock_session_cls.return_value = mock_session

    from app.tasks.holdings_tasks import parse_holdings_upload

    result = parse_holdings_upload.run(str(job_id))

    assert result == {"job_id": str(job_id), "status": "success"}
    assert job.status == "success"
    assert job.preview == preview_dump  # untouched — not overwritten with a failure
    mock_parse.assert_not_called()
    mock_session.commit.assert_not_called()


@patch("app.services.holding_parser.parse")
@patch("app.core.database.SessionLocal")
def test_parse_holdings_upload_redelivery_after_failure_is_idempotent(
    mock_session_cls: MagicMock, mock_parse: MagicMock
) -> None:
    job_id = uuid.uuid4()
    job = _make_job(job_id, raw_text=None, status="failed")
    job.error = "LLM call failed: boom"
    mock_session = MagicMock()
    mock_session.get.return_value = job
    mock_session_cls.return_value = mock_session

    from app.tasks.holdings_tasks import parse_holdings_upload

    result = parse_holdings_upload.run(str(job_id))

    assert result == {"job_id": str(job_id), "status": "failed"}
    assert job.status == "failed"
    assert job.error == "LLM call failed: boom"
    mock_parse.assert_not_called()
    mock_session.commit.assert_not_called()


@patch("app.core.database.SessionLocal")
def test_parse_holdings_upload_missing_job_returns_early(mock_session_cls: MagicMock) -> None:
    mock_session = MagicMock()
    mock_session.get.return_value = None
    mock_session_cls.return_value = mock_session

    from app.tasks.holdings_tasks import parse_holdings_upload

    result = parse_holdings_upload.run(str(uuid.uuid4()))

    assert result == {"status": "job_not_found"}
    mock_session.commit.assert_not_called()
    mock_session.close.assert_called_once()


def test_parse_holdings_upload_has_time_limits() -> None:
    """Issue #85 / PR #88 review: time_limit is pinned to the 45s product
    SLA (down from the 90s ceiling PR #82 set for the old, much slower
    model); soft_time_limit leaves only a 2s gap to the hard kill (not 10s)
    so it doesn't cut off a legitimate two-attempt run (2 x 20s = ~40s)
    before either attempt gets to fail on its own terms."""
    from app.tasks.holdings_tasks import _SLA_SECONDS, parse_holdings_upload

    assert _SLA_SECONDS == 45
    assert parse_holdings_upload.time_limit == 45
    assert parse_holdings_upload.soft_time_limit == 43


def test_parse_holdings_upload_uses_upload_job_request() -> None:
    """PR #88 review: Task.Request is Celery's documented per-task
    customization point (resolved by celery/worker/strategy.py via
    symbol_by_name(task.Request)) — this must actually be wired for
    _UploadJobRequest.on_timeout to run for a real hard-timeout kill, not
    just be a class that exists but is never used."""
    from app.tasks.holdings_tasks import _UploadJobRequest, parse_holdings_upload

    assert parse_holdings_upload.Request is _UploadJobRequest


@patch("app.core.database.SessionLocal")
@patch("celery.worker.request.Request.on_timeout")
def test_upload_job_request_on_timeout_hard_limit_resolves_job(
    mock_super_on_timeout: MagicMock, mock_session_cls: MagicMock
) -> None:
    """PR #88 review finding: task_revoked never fires for an automatic
    hard time_limit kill on the installed celery==5.6.3 (verified against
    its source — Request.on_timeout(soft=False) is the actual hook Celery
    calls, and it never sends task_revoked). This override is what must
    resolve the job for issue #85's real SIGKILL scenario — the
    task_revoked handler below only covers an explicit admin revoke."""
    job_id = uuid.uuid4()
    job = _make_job(job_id)
    mock_session = MagicMock()
    mock_session.get.return_value = job
    mock_session_cls.return_value = mock_session

    from app.tasks.holdings_tasks import _UploadJobRequest

    req = object.__new__(_UploadJobRequest)
    req._args = (str(job_id),)
    req._kwargs = {}

    _UploadJobRequest.on_timeout(req, soft=False, timeout=45)

    # Original Celery behavior (backend result marking, ack) must still run.
    mock_super_on_timeout.assert_called_once_with(False, 45)
    assert job.status == "failed"
    assert job.raw_text is None
    assert "45" in job.error


@patch("app.core.database.SessionLocal")
@patch("celery.worker.request.Request.on_timeout")
def test_upload_job_request_on_timeout_soft_limit_leaves_job_alone(
    mock_super_on_timeout: MagicMock, mock_session_cls: MagicMock
) -> None:
    """Soft limit is handled by the task's own except/finally when it gets
    a chance to run (SoftTimeLimitExceeded is catchable, unlike SIGKILL) —
    this hook only needs to act on the hard (soft=False) path."""
    from app.tasks.holdings_tasks import _UploadJobRequest

    req = object.__new__(_UploadJobRequest)
    req._args = (str(uuid.uuid4()),)
    req._kwargs = {}

    _UploadJobRequest.on_timeout(req, soft=True, timeout=43)

    mock_super_on_timeout.assert_called_once_with(True, 43)
    mock_session_cls.assert_not_called()


@patch("app.core.database.SessionLocal")
@patch("celery.worker.request.Request.on_timeout")
def test_upload_job_request_on_timeout_missing_job_id_is_safe(
    mock_super_on_timeout: MagicMock, mock_session_cls: MagicMock
) -> None:
    from app.tasks.holdings_tasks import _UploadJobRequest

    req = object.__new__(_UploadJobRequest)
    req._args = ()
    req._kwargs = {}

    _UploadJobRequest.on_timeout(req, soft=False, timeout=45)

    mock_super_on_timeout.assert_called_once_with(False, 45)
    mock_session_cls.assert_not_called()


@patch("app.core.database.SessionLocal")
def test_task_revoked_handler_marks_job_failed_on_terminated(mock_session_cls: MagicMock) -> None:
    """This handler covers an explicit admin revoke(terminate=True), not
    the automatic hard time_limit path (that's _UploadJobRequest.on_timeout,
    covered above — task_revoked never fires for it, per the PR #88 review
    finding). Tested independently here since the handler's own logic
    (extract job_id, resolve, guard terminated=False) is correct and worth
    keeping regardless of which trigger reaches it."""
    job_id = uuid.uuid4()
    job = _make_job(job_id)
    mock_session = MagicMock()
    mock_session.get.return_value = job
    mock_session_cls.return_value = mock_session

    from app.tasks.holdings_tasks import _mark_revoked_job_failed

    request = MagicMock()
    request.args = (str(job_id),)
    request.kwargs = {}

    _mark_revoked_job_failed(request=request, terminated=True, signum=9)

    assert job.status == "failed"
    assert job.raw_text is None
    assert "signal 9" in job.error
    mock_session.commit.assert_called_once()
    mock_session.close.assert_called_once()


@patch("app.core.database.SessionLocal")
def test_task_revoked_handler_reads_job_id_from_kwargs(mock_session_cls: MagicMock) -> None:
    """.delay(job_id) passes it positionally today, but the handler must not
    assume that — reading from request.kwargs as a fallback keeps it correct
    if the call site ever switches to a keyword argument."""
    job_id = uuid.uuid4()
    job = _make_job(job_id)
    mock_session = MagicMock()
    mock_session.get.return_value = job
    mock_session_cls.return_value = mock_session

    from app.tasks.holdings_tasks import _mark_revoked_job_failed

    request = MagicMock()
    request.args = ()
    request.kwargs = {"job_id": str(job_id)}

    _mark_revoked_job_failed(request=request, terminated=True, signum=9)

    assert job.status == "failed"


@patch("app.core.database.SessionLocal")
def test_task_revoked_handler_ignores_non_terminated_revocation(
    mock_session_cls: MagicMock,
) -> None:
    """terminated=False means the task was pulled off the queue before it
    ever ran — nothing was written, so there's nothing to clean up."""
    from app.tasks.holdings_tasks import _mark_revoked_job_failed

    _mark_revoked_job_failed(request=MagicMock(), terminated=False, signum=None)

    mock_session_cls.assert_not_called()


@patch("app.core.database.SessionLocal")
def test_task_revoked_handler_ignores_missing_request(mock_session_cls: MagicMock) -> None:
    from app.tasks.holdings_tasks import _mark_revoked_job_failed

    _mark_revoked_job_failed(request=None, terminated=True, signum=9)

    mock_session_cls.assert_not_called()


@patch("app.core.database.SessionLocal")
def test_task_revoked_signal_dispatches_only_for_parse_holdings_upload(
    mock_session_cls: MagicMock,
) -> None:
    """The handler is registered with sender=parse_holdings_upload (issue
    #85) so it must not fire for another task's revocation sharing the same
    Celery app — asserted via the real signal dispatch, not by calling the
    handler function directly, since that's the part a sender-scoping bug
    would actually break."""
    from celery.signals import task_revoked  # type: ignore[import-untyped]

    from app.tasks.holdings_tasks import parse_holdings_upload
    from app.tasks.report_tasks import generate_incremental_report

    request = MagicMock()
    request.args = (str(uuid.uuid4()),)
    request.kwargs = {}

    task_revoked.send(
        sender=generate_incremental_report, request=request, terminated=True, signum=9
    )
    mock_session_cls.assert_not_called()

    task_revoked.send(sender=parse_holdings_upload, request=request, terminated=True, signum=9)
    mock_session_cls.assert_called_once()


@patch("app.core.database.SessionLocal")
def test_sweep_stale_upload_jobs_marks_stuck_pending_rows_failed(
    mock_session_cls: MagicMock,
) -> None:
    """Backstop for cases _UploadJobRequest.on_timeout itself misses (issue
    #85): a stale-pending row past the sweep threshold gets resolved the
    same way (failed + raw_text cleared)."""
    stale_id = uuid.uuid4()
    stale_row = MagicMock()
    stale_row.id = stale_id
    mock_session = MagicMock()
    mock_session.query.return_value.filter.return_value.all.return_value = [stale_row]
    stale_job = _make_job(stale_id, raw_text="leftover holdings text", status="pending")
    mock_session.get.return_value = stale_job
    mock_session_cls.return_value = mock_session

    from app.tasks.holdings_tasks import sweep_stale_upload_jobs

    result = sweep_stale_upload_jobs.run()

    assert result == {"swept": 1}
    assert stale_job.status == "failed"
    assert stale_job.raw_text is None
    assert stale_job.error is not None


@patch("app.core.database.SessionLocal")
def test_sweep_stale_upload_jobs_no_stale_rows_is_a_noop(mock_session_cls: MagicMock) -> None:
    mock_session = MagicMock()
    mock_session.query.return_value.filter.return_value.all.return_value = []
    mock_session_cls.return_value = mock_session

    from app.tasks.holdings_tasks import sweep_stale_upload_jobs

    result = sweep_stale_upload_jobs.run()

    assert result == {"swept": 0}
    mock_session.commit.assert_not_called()


@patch("app.core.database.SessionLocal")
def test_sweep_stale_upload_jobs_skips_row_already_resolved(mock_session_cls: MagicMock) -> None:
    """Race guard: the sweeper's own query and its per-row resolve open
    separate sessions, so a row on_timeout already resolved in between must
    be left untouched rather than overwritten."""
    resolved_id = uuid.uuid4()
    resolved_row = MagicMock()
    resolved_row.id = resolved_id
    mock_session = MagicMock()
    mock_session.query.return_value.filter.return_value.all.return_value = [resolved_row]
    already_resolved_job = _make_job(resolved_id, raw_text=None, status="success")
    mock_session.get.return_value = already_resolved_job
    mock_session_cls.return_value = mock_session

    from app.tasks.holdings_tasks import sweep_stale_upload_jobs

    result = sweep_stale_upload_jobs.run()

    assert result == {"swept": 0}
    mock_session.commit.assert_not_called()


@patch("app.core.database.SessionLocal")
def test_resolve_pending_job_as_failed_swallows_db_errors(mock_session_cls: MagicMock) -> None:
    """PR #88 review nit: a transient DB error mid-resolve must not
    propagate — this runs from Celery's own timeout-handling machinery, a
    signal receiver, and a per-row loop in the sweeper, none of which
    should crash on one bad row/call."""
    mock_session = MagicMock()
    mock_session.get.side_effect = RuntimeError("connection reset")
    mock_session_cls.return_value = mock_session

    from app.tasks.holdings_tasks import _resolve_pending_job_as_failed

    result = _resolve_pending_job_as_failed(
        job_id=str(uuid.uuid4()), error="boom", log_context="test"
    )

    assert result is False
    mock_session.close.assert_called_once()


@patch("app.core.database.SessionLocal")
def test_resolve_pending_job_as_failed_swallows_session_open_errors(
    mock_session_cls: MagicMock,
) -> None:
    mock_session_cls.side_effect = RuntimeError("db unreachable")

    from app.tasks.holdings_tasks import _resolve_pending_job_as_failed

    result = _resolve_pending_job_as_failed(
        job_id=str(uuid.uuid4()), error="boom", log_context="test"
    )

    assert result is False


def test_sweep_stale_after_seconds_has_queue_wait_headroom() -> None:
    """PR #88 review: created_at is set at enqueue time, not when a worker
    picks the task up, and holdings uploads share the default queue/worker
    pool with capture tasks — the sweep threshold needs slack beyond the
    45s parse SLA so a job that's still legitimately queued isn't false-
    failed."""
    from app.tasks.holdings_tasks import _SLA_SECONDS, _SWEEP_STALE_AFTER_SECONDS

    assert _SWEEP_STALE_AFTER_SECONDS == _SLA_SECONDS + 45
