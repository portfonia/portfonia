"""app.tasks.email_verification_tasks.poll_email_verification_delivery
(issue #260, Ring 1-Email Validation design doc §3.3 step 6).

Same SessionLocal-mocking shape as test_report_tasks.py — the task opens its
own session via a lazy `from app.core.database import SessionLocal` import,
so patching `app.core.database.SessionLocal` (not a name in this task
module) is what actually intercepts it.
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

import httpx

from app.tasks.email_verification_tasks import poll_email_verification_delivery

_VID = uuid.uuid4()


def _fake_record(
    *, status: str = "pending", provider_message_id: str | None = "resend-id-1"
) -> MagicMock:
    record = MagicMock()
    record.id = _VID
    record.status = status
    record.provider_message_id = provider_message_id
    return record


@patch("app.tasks.email_verification_tasks.get_settings")
def test_poll_skips_when_no_full_access_key_configured(mock_settings: MagicMock) -> None:
    mock_settings.return_value = MagicMock(RESEND_ALL_ACCESS_API_KEY=None)

    result = poll_email_verification_delivery.run(str(_VID))

    assert result == "skipped_no_key"


@patch("app.tasks.email_verification_tasks.get_settings")
@patch("app.core.database.SessionLocal")
def test_poll_skips_when_record_missing(
    mock_session_cls: MagicMock, mock_settings: MagicMock
) -> None:
    mock_settings.return_value = MagicMock(
        RESEND_ALL_ACCESS_API_KEY=MagicMock(get_secret_value=lambda: "full-access-key")
    )
    mock_session = MagicMock()
    mock_session.get.return_value = None
    mock_session_cls.return_value = mock_session

    result = poll_email_verification_delivery.run(str(_VID))

    assert result == "skipped_not_found"


@patch("app.tasks.email_verification_tasks.get_settings")
@patch("app.core.database.SessionLocal")
def test_poll_skips_when_no_longer_pending(
    mock_session_cls: MagicMock, mock_settings: MagicMock
) -> None:
    mock_settings.return_value = MagicMock(
        RESEND_ALL_ACCESS_API_KEY=MagicMock(get_secret_value=lambda: "full-access-key")
    )
    mock_session = MagicMock()
    mock_session.get.return_value = _fake_record(status="verified")
    mock_session_cls.return_value = mock_session

    result = poll_email_verification_delivery.run(str(_VID))

    assert result == "skipped_status_verified"


@patch("app.tasks.email_verification_tasks.httpx.Client")
@patch("app.tasks.email_verification_tasks.get_settings")
@patch("app.core.database.SessionLocal")
def test_poll_marks_undeliverable_on_bounce(
    mock_session_cls: MagicMock, mock_settings: MagicMock, mock_client_cls: MagicMock
) -> None:
    mock_settings.return_value = MagicMock(
        RESEND_ALL_ACCESS_API_KEY=MagicMock(get_secret_value=lambda: "full-access-key")
    )
    record = _fake_record()
    mock_session = MagicMock()
    mock_session.get.return_value = record
    mock_session_cls.return_value = mock_session

    resp = MagicMock(status_code=200)
    resp.json.return_value = {"last_event": "bounced"}
    resp.raise_for_status.return_value = None
    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.get.return_value = resp
    mock_client_cls.return_value = mock_client

    result = poll_email_verification_delivery.run(str(_VID))

    assert result == "undeliverable_bounced"
    assert record.status == "undeliverable"
    mock_session.commit.assert_called_once()


@patch("app.tasks.email_verification_tasks.httpx.Client")
@patch("app.tasks.email_verification_tasks.get_settings")
@patch("app.core.database.SessionLocal")
def test_poll_leaves_pending_on_delivered(
    mock_session_cls: MagicMock, mock_settings: MagicMock, mock_client_cls: MagicMock
) -> None:
    mock_settings.return_value = MagicMock(
        RESEND_ALL_ACCESS_API_KEY=MagicMock(get_secret_value=lambda: "full-access-key")
    )
    record = _fake_record()
    mock_session = MagicMock()
    mock_session.get.return_value = record
    mock_session_cls.return_value = mock_session

    resp = MagicMock(status_code=200)
    resp.json.return_value = {"last_event": "delivered"}
    resp.raise_for_status.return_value = None
    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.get.return_value = resp
    mock_client_cls.return_value = mock_client

    result = poll_email_verification_delivery.run(str(_VID))

    assert result == "ok_delivered"
    assert record.status == "pending"  # untouched
    mock_session.commit.assert_not_called()


@patch("app.tasks.email_verification_tasks.httpx.Client")
@patch("app.tasks.email_verification_tasks.get_settings")
@patch("app.core.database.SessionLocal")
def test_poll_skips_when_provider_reports_404(
    mock_session_cls: MagicMock, mock_settings: MagicMock, mock_client_cls: MagicMock
) -> None:
    mock_settings.return_value = MagicMock(
        RESEND_ALL_ACCESS_API_KEY=MagicMock(get_secret_value=lambda: "full-access-key")
    )
    record = _fake_record()
    mock_session = MagicMock()
    mock_session.get.return_value = record
    mock_session_cls.return_value = mock_session

    resp = MagicMock(status_code=404)
    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.get.return_value = resp
    mock_client_cls.return_value = mock_client

    result = poll_email_verification_delivery.run(str(_VID))

    assert result == "skipped_not_found_at_provider"
    assert record.status == "pending"


@patch("app.tasks.email_verification_tasks.httpx.Client")
@patch("app.tasks.email_verification_tasks.get_settings")
@patch("app.core.database.SessionLocal")
def test_poll_retries_on_http_error(
    mock_session_cls: MagicMock, mock_settings: MagicMock, mock_client_cls: MagicMock
) -> None:
    mock_settings.return_value = MagicMock(
        RESEND_ALL_ACCESS_API_KEY=MagicMock(get_secret_value=lambda: "full-access-key")
    )
    record = _fake_record()
    mock_session = MagicMock()
    mock_session.get.return_value = record
    mock_session_cls.return_value = mock_session

    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.get.side_effect = httpx.ConnectError("boom")
    mock_client_cls.return_value = mock_client

    class _StopRetry(Exception):
        pass

    with patch.object(poll_email_verification_delivery, "retry", side_effect=_StopRetry):
        try:
            poll_email_verification_delivery.run(str(_VID))
        except _StopRetry:
            pass
        else:
            raise AssertionError("expected the task to call self.retry() on an HTTP error")
