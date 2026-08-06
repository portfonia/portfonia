"""Tests for the async holdings-upload Celery task (issue #77 / PR #82 review).

Strategy mirrors test_report_tasks.py: SessionLocal is bound to the dev DB at
import time (app.core.database), so task logic is tested by mocking it and
calling the task's underlying function directly (`.run()` bypasses Celery
routing) rather than spinning up a real worker or the test Postgres DB.
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
    """holding_parser.parse() exhausting all 3 attempts raises RuntimeError —
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
    """PR #82 review: an explicit ceiling above the ~60s worst case (3
    attempts x 20s client timeout) so a hang outside that path can't hold a
    prefork worker slot indefinitely."""
    from app.tasks.holdings_tasks import parse_holdings_upload

    assert parse_holdings_upload.soft_time_limit == 75
    assert parse_holdings_upload.time_limit == 90
