"""Tests for the async holdings-upload Celery task (issue #77).

Strategy mirrors test_report_tasks.py: SessionLocal is bound to the dev DB at
import time (app.core.database), so task logic is tested by mocking it and
calling the task's underlying function directly (`.run()` bypasses Celery
routing) rather than spinning up a real worker or the test Postgres DB.
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

from app.schemas.holdings import UploadPreview


def _make_job(job_id: uuid.UUID) -> MagicMock:
    job = MagicMock()
    job.id = job_id
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

    result = parse_holdings_upload.run(str(job_id), "some holdings text")  # bypasses Celery routing

    assert result == {"job_id": str(job_id), "status": "success"}
    assert job.status == "success"
    assert job.preview == preview.model_dump(mode="json")
    mock_parse.assert_called_once_with("some holdings text")
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

    result = parse_holdings_upload.run(str(job_id), "some holdings text")

    assert result == {"job_id": str(job_id), "status": "failed"}
    assert job.status == "failed"
    assert job.error == "LLM call failed: boom"
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

    result = parse_holdings_upload.run(str(job_id), "some holdings text")

    assert result == {"job_id": str(job_id), "status": "failed"}
    assert job.status == "failed"
    assert "ValueError" in job.error
    mock_session.commit.assert_called_once()


@patch("app.core.database.SessionLocal")
def test_parse_holdings_upload_missing_job_returns_early(mock_session_cls: MagicMock) -> None:
    mock_session = MagicMock()
    mock_session.get.return_value = None
    mock_session_cls.return_value = mock_session

    from app.tasks.holdings_tasks import parse_holdings_upload

    result = parse_holdings_upload.run(str(uuid.uuid4()), "text")

    assert result == {"status": "job_not_found"}
    mock_session.commit.assert_not_called()
    mock_session.close.assert_called_once()
