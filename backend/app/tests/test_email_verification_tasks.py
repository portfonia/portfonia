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
import pytest

from app.tasks import celery_app
from app.tasks.email_verification_tasks import poll_email_verification_delivery

_VID = uuid.uuid4()


def test_poll_task_module_is_in_the_celery_app_include_list() -> None:
    """Regression (review, PR #261): the task module existed and
    create_verification() enqueued it, but app.tasks.__init__'s `include`
    list never named it — the API process could enqueue the message (it
    imports the module directly), while a real worker process, which only
    loads tasks from `include` at startup, would treat it as unregistered
    and the delivery poll would silently never run.

    Asserting `celery_app.tasks` instead would NOT catch this: this very
    test file's own `from app.tasks.email_verification_tasks import ...`
    above registers the task via Python's normal import side effect,
    regardless of what `include` says — the same reason "not catchable by
    the other tests here" was true of the original bug. `conf.include` is
    the actual list a worker process consults at startup, so it's the only
    thing worth asserting on."""
    assert "app.tasks.email_verification_tasks" in celery_app.conf.include


def _fake_record(
    *, status: str = "pending", provider_message_id: str | None = "resend-id-1"
) -> MagicMock:
    record = MagicMock()
    record.id = _VID
    record.status = status
    record.provider_message_id = provider_message_id
    return record


@patch("app.tasks.email_verification_tasks.get_settings")
def test_poll_skips_and_alerts_when_no_full_access_key_configured(
    mock_settings: MagicMock,
) -> None:
    """issue #104 requirement #7: a missing key now fires one deduped ops
    alert instead of silently skipping."""
    mock_settings.return_value = MagicMock(RESEND_ALL_ACCESS_API_KEY=None)

    with patch(
        "app.tasks.email_verification_tasks.alert_resend_all_access_key_issue"
    ) as mock_alert:
        result = poll_email_verification_delivery.run(str(_VID))

    assert result == "skipped_no_key"
    mock_alert.assert_called_once_with("missing")


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
    mock_session.execute.return_value = MagicMock(rowcount=1)
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
    # The write is a conditional UPDATE ... WHERE status='pending', not a
    # plain attribute assignment on the loaded `record` (review, PR #261) —
    # assert the UPDATE actually ran, not a mutation on the stale in-memory
    # object, which this test's own mock would happily let pass either way.
    mock_session.execute.assert_called_once()
    mock_session.commit.assert_called_once()


@patch("app.tasks.email_verification_tasks.httpx.Client")
@patch("app.tasks.email_verification_tasks.get_settings")
@patch("app.core.database.SessionLocal")
def test_poll_does_not_overwrite_a_row_that_moved_on_before_the_write(
    mock_session_cls: MagicMock, mock_settings: MagicMock, mock_client_cls: MagicMock
) -> None:
    """Regression (review, PR #261): the initial `session.get()` can read
    `pending` while a concurrent confirm() on a different session verifies
    the row moments later. The conditional UPDATE's rowcount is the only
    thing allowed to decide whether the write actually happened — this test
    simulates that race by having the UPDATE report 0 rows matched even
    though the earlier read said "pending"."""
    mock_settings.return_value = MagicMock(
        RESEND_ALL_ACCESS_API_KEY=MagicMock(get_secret_value=lambda: "full-access-key")
    )
    record = _fake_record()
    mock_session = MagicMock()
    mock_session.get.return_value = record
    mock_session.execute.return_value = MagicMock(rowcount=0)
    mock_session_cls.return_value = mock_session

    resp = MagicMock(status_code=200)
    resp.json.return_value = {"last_event": "bounced"}
    resp.raise_for_status.return_value = None
    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.get.return_value = resp
    mock_client_cls.return_value = mock_client

    result = poll_email_verification_delivery.run(str(_VID))

    assert result == "skipped_no_longer_pending_at_write"


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
    mock_session.execute.assert_not_called()  # no UPDATE issued for a non-bounce event
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
    mock_session.execute.assert_not_called()


@patch("app.tasks.email_verification_tasks.httpx.Client")
@patch("app.tasks.email_verification_tasks.get_settings")
@patch("app.core.database.SessionLocal")
def test_poll_skips_and_alerts_on_401(
    mock_session_cls: MagicMock, mock_settings: MagicMock, mock_client_cls: MagicMock
) -> None:
    """issue #104 requirement #7: an invalid/revoked key now fires one
    deduped ops alert instead of falling into self.retry()."""
    mock_settings.return_value = MagicMock(
        RESEND_ALL_ACCESS_API_KEY=MagicMock(get_secret_value=lambda: "full-access-key")
    )
    record = _fake_record()
    mock_session = MagicMock()
    mock_session.get.return_value = record
    mock_session_cls.return_value = mock_session

    resp = MagicMock(status_code=401)
    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.get.return_value = resp
    mock_client_cls.return_value = mock_client

    with patch(
        "app.tasks.email_verification_tasks.alert_resend_all_access_key_issue"
    ) as mock_alert:
        result = poll_email_verification_delivery.run(str(_VID))

    assert result == "skipped_unauthorized"
    mock_alert.assert_called_once_with("unauthorized")
    mock_session.execute.assert_not_called()


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


# --- signup hook (issue #262, Ring 1-Profile Page.md §8.7 / Email
# Validation.md §4.1) ---

_SIGNUP_UID = uuid.uuid4()


def test_send_account_verification_task_module_is_in_the_celery_app_include_list() -> None:
    """Regression guard for the same failure mode PR #261 round 1 caught on
    the poll task: the task module gets imported directly by this test file
    (and by the signup router), so `celery_app.tasks` would register it via
    normal import side effects and hide a missing `include` entry — only
    `conf.include` is what a real worker process consults at startup."""
    assert "app.tasks.email_verification_tasks" in celery_app.conf.include


@patch("app.core.database.SessionLocal")
@patch("app.services.email_verification.create_verification")
def test_send_account_verification_task_creates_account_email_verification(
    mock_create: MagicMock, mock_session_cls: MagicMock
) -> None:
    from app.tasks.email_verification_tasks import send_account_email_verification_task

    mock_session = MagicMock()
    mock_user = MagicMock()
    mock_user.email = "new-user@example.com"
    mock_user.id = _SIGNUP_UID
    mock_session.get.return_value = mock_user
    mock_session_cls.return_value = mock_session

    send_account_email_verification_task.run(str(_SIGNUP_UID))

    mock_create.assert_called_once_with(
        mock_session, email="new-user@example.com", purpose="account_email", user_id=_SIGNUP_UID
    )
    mock_session.close.assert_called_once()


@patch("app.core.database.SessionLocal")
@patch("app.services.email_verification.create_verification")
def test_send_account_verification_task_reraises_send_failure(
    mock_create: MagicMock, mock_session_cls: MagicMock
) -> None:
    """create_verification's failure modes are handled upstream (send-first
    means VerificationSendFailed leaves zero DB writes; commit failures log
    loudly); the task must not swallow them — they propagate for Celery's
    retry machinery (Profile Page.md §8.7)."""
    from app.services.email_verification import VerificationSendFailed
    from app.tasks.email_verification_tasks import send_account_email_verification_task

    mock_session = MagicMock()
    mock_session.get.return_value = MagicMock()  # user exists; fail inside create_verification
    mock_session_cls.return_value = mock_session
    mock_create.side_effect = VerificationSendFailed

    with pytest.raises(VerificationSendFailed):
        send_account_email_verification_task.run(str(_SIGNUP_UID))


@patch("app.core.database.SessionLocal")
@patch("app.services.email_verification.create_verification")
def test_send_account_verification_task_schedules_celery_retry_on_failure(
    mock_create: MagicMock, mock_session_cls: MagicMock
) -> None:
    """Regression (PR #263 review): max_retries=3 on the decorator does
    nothing by itself — without an explicit self.retry() call, a transient
    Resend failure fails the task once and is never retried, silently
    losing the signup verification email. The retry wiring must be
    asserted directly (patch self.retry), not inferred from .run()'s
    exception propagation."""
    from app.services.email_verification import VerificationSendFailed
    from app.tasks.email_verification_tasks import send_account_email_verification_task

    mock_session = MagicMock()
    mock_session.get.return_value = MagicMock()
    mock_session_cls.return_value = mock_session
    mock_create.side_effect = VerificationSendFailed

    class _StopRetry(Exception):
        pass

    with patch.object(
        send_account_email_verification_task, "retry", side_effect=_StopRetry
    ) as mock_retry:
        try:
            send_account_email_verification_task.run(str(_SIGNUP_UID))
        except _StopRetry:
            pass
        else:
            raise AssertionError("expected the task to call self.retry() on a send failure")
    mock_retry.assert_called_once()
    assert mock_retry.call_args.kwargs.get("exc") is not None


@patch("app.core.database.SessionLocal")
@patch("app.services.email_verification.create_verification")
def test_send_account_verification_task_gives_up_after_max_retries(
    mock_create: MagicMock, mock_session_cls: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    """At retries >= max_retries the task stops asking for another retry and
    logs the real recovery path — the Ops API, since no Profile-page record
    exists to resend from. .run() bypasses Celery's retry machinery, so
    self.retry() re-raises the original exception rather than actually
    scheduling a retry (same trick as test_report_tasks's exhaustion test)."""
    import logging

    from app.services.email_verification import VerificationSendFailed
    from app.tasks.email_verification_tasks import send_account_email_verification_task

    # The session-migrate fileConfig disables already-instantiated loggers
    # (CLAUDE.md Tests note) — re-enable this one for the caplog assertion.
    logging.getLogger("app.tasks.email_verification_tasks").disabled = False

    mock_session = MagicMock()
    mock_session.get.return_value = MagicMock()
    mock_session_cls.return_value = mock_session
    mock_create.side_effect = VerificationSendFailed

    with (
        caplog.at_level(logging.ERROR),
        patch.object(send_account_email_verification_task, "max_retries", 0),
        pytest.raises(VerificationSendFailed),
    ):
        send_account_email_verification_task.run(str(_SIGNUP_UID))

    assert "POST /admin/email-verifications" in caplog.text


# --- alert_resend_all_access_key_issue (issue #104 requirement #7) ---


@patch("app.services.email_sender.send_ops_alert", return_value=True)
def test_alert_resend_all_access_key_issue_sends_and_dedups(mock_alert: MagicMock) -> None:
    """Shared by both poll_email_verification_delivery (this module) and
    poll_report_delivery — a persisting key issue must alert once, not on
    every 10-minute poll (alert_dedup.py's existing anti-spam contract)."""
    from app.tasks.email_verification_tasks import alert_resend_all_access_key_issue

    alert_resend_all_access_key_issue("missing")
    alert_resend_all_access_key_issue("missing")
    alert_resend_all_access_key_issue("missing")

    mock_alert.assert_called_once()


@patch("app.services.email_sender.send_ops_alert", return_value=True)
def test_alert_resend_all_access_key_issue_distinct_reasons_alert_separately(
    mock_alert: MagicMock,
) -> None:
    from app.tasks.email_verification_tasks import alert_resend_all_access_key_issue

    alert_resend_all_access_key_issue("missing")
    alert_resend_all_access_key_issue("unauthorized")

    assert mock_alert.call_count == 2


@patch("app.services.email_sender.send_ops_alert", return_value=False)
def test_alert_resend_all_access_key_issue_does_not_dedup_a_failed_send(
    mock_alert: MagicMock,
) -> None:
    """A failed alert delivery must not be recorded as "already alerted" —
    same fail-open contract as alert_dedup.py's other callers (issue #298
    review)."""
    from app.tasks.email_verification_tasks import alert_resend_all_access_key_issue

    alert_resend_all_access_key_issue("missing")
    alert_resend_all_access_key_issue("missing")

    assert mock_alert.call_count == 2
