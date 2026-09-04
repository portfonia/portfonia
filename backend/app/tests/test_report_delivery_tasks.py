"""app.tasks.report_delivery_tasks.poll_report_delivery (issue #104, Ring
1-Email Validation design doc's 2026-09-03 section).

Same SessionLocal-mocking shape as test_email_verification_tasks.py — the
task opens its own session via a lazy `from app.core.database import
SessionLocal` import, so patching `app.core.database.SessionLocal` (not a
name in this task module) is what actually intercepts it.
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.tasks import celery_app
from app.tasks.report_delivery_tasks import poll_report_delivery

_RID = uuid.uuid4()
_UID = uuid.uuid4()


def test_poll_task_module_is_in_the_celery_app_include_list() -> None:
    """Same regression class PR #261 caught on the sibling verification poll
    task: importing the module here registers it via Python's normal import
    side effect regardless of `include`, so only `conf.include` — what a
    real worker process actually consults at startup — is worth asserting."""
    assert "app.tasks.report_delivery_tasks" in celery_app.conf.include


def _fake_report(
    *,
    provider_message_id: str | None = "resend-id-1",
    recipient_email: str | None = "user@example.com",
    recipient_purpose: str | None = "account_email",
) -> MagicMock:
    report = MagicMock()
    report.id = _RID
    report.user_id = _UID
    report.provider_message_id = provider_message_id
    report.recipient_email = recipient_email
    report.recipient_purpose = recipient_purpose
    return report


@patch("app.tasks.report_delivery_tasks.get_settings")
def test_poll_skips_and_alerts_when_no_full_access_key_configured(
    mock_settings: MagicMock,
) -> None:
    mock_settings.return_value = MagicMock(RESEND_ALL_ACCESS_API_KEY=None)

    with patch("app.tasks.report_delivery_tasks.alert_resend_all_access_key_issue") as mock_alert:
        result = poll_report_delivery.run(str(_RID))

    assert result == "skipped_no_key"
    mock_alert.assert_called_once_with("missing")


@patch("app.tasks.report_delivery_tasks.get_settings")
@patch("app.core.database.SessionLocal")
def test_poll_skips_when_report_missing(
    mock_session_cls: MagicMock, mock_settings: MagicMock
) -> None:
    mock_settings.return_value = MagicMock(
        RESEND_ALL_ACCESS_API_KEY=MagicMock(get_secret_value=lambda: "full-access-key")
    )
    mock_session = MagicMock()
    mock_session.get.return_value = None
    mock_session_cls.return_value = mock_session

    result = poll_report_delivery.run(str(_RID))

    assert result == "skipped_not_found"


@patch("app.tasks.report_delivery_tasks.get_settings")
@patch("app.core.database.SessionLocal")
def test_poll_skips_when_no_provider_message_id(
    mock_session_cls: MagicMock, mock_settings: MagicMock
) -> None:
    mock_settings.return_value = MagicMock(
        RESEND_ALL_ACCESS_API_KEY=MagicMock(get_secret_value=lambda: "full-access-key")
    )
    mock_session = MagicMock()
    mock_session.get.return_value = _fake_report(provider_message_id=None)
    mock_session_cls.return_value = mock_session

    result = poll_report_delivery.run(str(_RID))

    assert result == "skipped_no_provider_id"


@patch("app.tasks.report_delivery_tasks.get_settings")
@patch("app.core.database.SessionLocal")
def test_poll_skips_when_no_recipient_recorded(
    mock_session_cls: MagicMock, mock_settings: MagicMock
) -> None:
    """issue #104 requirement #8 (no backfill): a report sent before this
    feature shipped has no recipient_email/recipient_purpose recorded."""
    mock_settings.return_value = MagicMock(
        RESEND_ALL_ACCESS_API_KEY=MagicMock(get_secret_value=lambda: "full-access-key")
    )
    mock_session = MagicMock()
    mock_session.get.return_value = _fake_report(recipient_email=None, recipient_purpose=None)
    mock_session_cls.return_value = mock_session

    result = poll_report_delivery.run(str(_RID))

    assert result == "skipped_no_recipient_recorded"


@patch("app.tasks.report_delivery_tasks.httpx.Client")
@patch("app.tasks.report_delivery_tasks.get_settings")
@patch("app.core.database.SessionLocal")
def test_poll_leaves_state_untouched_on_delivered(
    mock_session_cls: MagicMock, mock_settings: MagicMock, mock_client_cls: MagicMock
) -> None:
    mock_settings.return_value = MagicMock(
        RESEND_ALL_ACCESS_API_KEY=MagicMock(get_secret_value=lambda: "full-access-key")
    )
    mock_session = MagicMock()
    mock_session.get.return_value = _fake_report()
    mock_session_cls.return_value = mock_session

    resp = MagicMock(status_code=200)
    resp.json.return_value = {"last_event": "delivered"}
    resp.raise_for_status.return_value = None
    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.get.return_value = resp
    mock_client_cls.return_value = mock_client

    result = poll_report_delivery.run(str(_RID))

    assert result == "ok_delivered"
    mock_session.execute.assert_not_called()
    mock_session.commit.assert_not_called()
    mock_session.add.assert_not_called()


@pytest.mark.parametrize("last_event", ["bounced", "failed", "suppressed"])
@patch("app.services.email_sender.send_ops_alert")
@patch("app.tasks.report_delivery_tasks.httpx.Client")
@patch("app.tasks.report_delivery_tasks.get_settings")
@patch("app.core.database.SessionLocal")
def test_poll_bounce_clears_verified_at_and_appends_auto_revoked_row(
    mock_session_cls: MagicMock,
    mock_settings: MagicMock,
    mock_client_cls: MagicMock,
    mock_ops_alert: MagicMock,
    last_event: str,
) -> None:
    """issue #104 requirement #3: bounce/complaint/failed/suppressed all get
    the same treatment — clear the user's *_verified_at, append an
    auto_revoked audit row. Parametrized over the three non-complaint
    events (PR #338 review, blacktomb42: only `bounced` was previously
    exercised directly, leaving `failed`/`suppressed` untested even though
    they share the exact same code path via UNDELIVERABLE_EVENTS) — none of
    these fire an ops alert (that's complaint-only, covered separately by
    test_poll_complaint_fires_one_ops_alert below)."""
    mock_settings.return_value = MagicMock(
        RESEND_ALL_ACCESS_API_KEY=MagicMock(get_secret_value=lambda: "full-access-key")
    )
    report = _fake_report(recipient_purpose="account_email", recipient_email="user@example.com")
    mock_session = MagicMock()
    mock_session.get.return_value = report
    mock_session.execute.return_value = MagicMock(rowcount=1)
    mock_session_cls.return_value = mock_session

    resp = MagicMock(status_code=200)
    resp.json.return_value = {"last_event": last_event}
    resp.raise_for_status.return_value = None
    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.get.return_value = resp
    mock_client_cls.return_value = mock_client

    result = poll_report_delivery.run(str(_RID))

    assert result == f"auto_revoked_{last_event}"
    # The clear is a conditional UPDATE ... WHERE user's address column ==
    # report.recipient_email AND verified_col IS NOT NULL — not a plain
    # attribute assignment (same discipline as poll_email_verification_
    # delivery's own conditional writes).
    update_stmt = mock_session.execute.call_args[0][0]
    values_by_name = {col.name: bind.value for col, bind in update_stmt._values.items()}
    assert values_by_name["email_verified_at"] is None

    mock_session.add.assert_called_once()
    added_row = mock_session.add.call_args[0][0]
    assert added_row.status == "auto_revoked"
    assert added_row.revoke_reason == last_event
    assert added_row.purpose == "account_email"
    assert added_row.email == "user@example.com"
    assert added_row.user_id == _UID

    mock_ops_alert.assert_not_called()


@patch("app.services.email_sender.send_ops_alert")
@patch("app.tasks.report_delivery_tasks.httpx.Client")
@patch("app.tasks.report_delivery_tasks.get_settings")
@patch("app.core.database.SessionLocal")
def test_poll_complaint_fires_one_ops_alert(
    mock_session_cls: MagicMock,
    mock_settings: MagicMock,
    mock_client_cls: MagicMock,
    mock_ops_alert: MagicMock,
) -> None:
    mock_settings.return_value = MagicMock(
        RESEND_ALL_ACCESS_API_KEY=MagicMock(get_secret_value=lambda: "full-access-key")
    )
    report = _fake_report(recipient_purpose="delivery_email", recipient_email="d@example.com")
    mock_session = MagicMock()
    mock_session.get.return_value = report
    mock_session.execute.return_value = MagicMock(rowcount=1)
    mock_session_cls.return_value = mock_session

    resp = MagicMock(status_code=200)
    resp.json.return_value = {"last_event": "complained"}
    resp.raise_for_status.return_value = None
    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.get.return_value = resp
    mock_client_cls.return_value = mock_client

    result = poll_report_delivery.run(str(_RID))

    assert result == "auto_revoked_complained"
    mock_ops_alert.assert_called_once()
    kwargs = mock_ops_alert.call_args.kwargs
    assert "complained" in kwargs["subject"] or "complaint" in kwargs["subject"].lower()
    assert "d@example.com" in kwargs["body"]


@patch("app.tasks.report_delivery_tasks.httpx.Client")
@patch("app.tasks.report_delivery_tasks.get_settings")
@patch("app.core.database.SessionLocal")
def test_poll_bounce_no_op_when_recipient_no_longer_matches(
    mock_session_cls: MagicMock, mock_settings: MagicMock, mock_client_cls: MagicMock
) -> None:
    """issue #104 requirement #2's rationale: the user may have changed
    their delivery address between send and poll — the conditional UPDATE's
    address-match guard means this must not clear the (unrelated) NEW
    address's verified_at, and must not append an audit row for the stale
    address either."""
    mock_settings.return_value = MagicMock(
        RESEND_ALL_ACCESS_API_KEY=MagicMock(get_secret_value=lambda: "full-access-key")
    )
    report = _fake_report(recipient_purpose="account_email", recipient_email="old@example.com")
    mock_session = MagicMock()
    mock_session.get.return_value = report
    mock_session.execute.return_value = MagicMock(rowcount=0)
    mock_session_cls.return_value = mock_session

    resp = MagicMock(status_code=200)
    resp.json.return_value = {"last_event": "bounced"}
    resp.raise_for_status.return_value = None
    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.get.return_value = resp
    mock_client_cls.return_value = mock_client

    result = poll_report_delivery.run(str(_RID))

    assert result == "skipped_stale_recipient_bounced"
    mock_session.add.assert_not_called()


@patch("app.tasks.report_delivery_tasks.httpx.Client")
@patch("app.tasks.report_delivery_tasks.get_settings")
@patch("app.core.database.SessionLocal")
def test_poll_skips_when_provider_reports_404(
    mock_session_cls: MagicMock, mock_settings: MagicMock, mock_client_cls: MagicMock
) -> None:
    mock_settings.return_value = MagicMock(
        RESEND_ALL_ACCESS_API_KEY=MagicMock(get_secret_value=lambda: "full-access-key")
    )
    mock_session = MagicMock()
    mock_session.get.return_value = _fake_report()
    mock_session_cls.return_value = mock_session

    resp = MagicMock(status_code=404)
    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.get.return_value = resp
    mock_client_cls.return_value = mock_client

    result = poll_report_delivery.run(str(_RID))

    assert result == "skipped_not_found_at_provider"
    mock_session.execute.assert_not_called()


@patch("app.tasks.report_delivery_tasks.httpx.Client")
@patch("app.tasks.report_delivery_tasks.get_settings")
@patch("app.core.database.SessionLocal")
def test_poll_skips_and_alerts_on_401(
    mock_session_cls: MagicMock, mock_settings: MagicMock, mock_client_cls: MagicMock
) -> None:
    mock_settings.return_value = MagicMock(
        RESEND_ALL_ACCESS_API_KEY=MagicMock(get_secret_value=lambda: "full-access-key")
    )
    mock_session = MagicMock()
    mock_session.get.return_value = _fake_report()
    mock_session_cls.return_value = mock_session

    resp = MagicMock(status_code=401)
    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.get.return_value = resp
    mock_client_cls.return_value = mock_client

    with patch("app.tasks.report_delivery_tasks.alert_resend_all_access_key_issue") as mock_alert:
        result = poll_report_delivery.run(str(_RID))

    assert result == "skipped_unauthorized"
    mock_alert.assert_called_once_with("unauthorized")
    mock_session.execute.assert_not_called()


@patch("app.tasks.report_delivery_tasks.httpx.Client")
@patch("app.tasks.report_delivery_tasks.get_settings")
@patch("app.core.database.SessionLocal")
def test_poll_retries_on_http_error(
    mock_session_cls: MagicMock, mock_settings: MagicMock, mock_client_cls: MagicMock
) -> None:
    mock_settings.return_value = MagicMock(
        RESEND_ALL_ACCESS_API_KEY=MagicMock(get_secret_value=lambda: "full-access-key")
    )
    mock_session = MagicMock()
    mock_session.get.return_value = _fake_report()
    mock_session_cls.return_value = mock_session

    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.get.side_effect = httpx.ConnectError("boom")
    mock_client_cls.return_value = mock_client

    class _StopRetry(Exception):
        pass

    with patch.object(poll_report_delivery, "retry", side_effect=_StopRetry):
        try:
            poll_report_delivery.run(str(_RID))
        except _StopRetry:
            pass
        else:
            raise AssertionError("expected the task to call self.retry() on an HTTP error")
