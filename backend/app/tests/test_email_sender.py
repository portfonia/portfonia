"""Tests for email_sender (Stage G).

Strategy:
- _render_html: verify table support, wrapper, no crash.
- send_report_email: mock httpx.Client + settings; cover happy path,
  dedup guard, empty report_md, HTTP error, network exception.
- DB session is mocked (email_sender has no schema-dependent queries).
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from unittest.mock import MagicMock, patch

from app.services.email_sender import _render_html, send_report_email

# ---------------------------------------------------------------------------
# _render_html
# ---------------------------------------------------------------------------


def test_render_html_wraps_body() -> None:
    html = _render_html("# Hello\n\nWorld")
    assert "<h1>Hello</h1>" in html
    assert "<p>World</p>" in html
    assert 'class="wrapper"' in html


def test_render_html_renders_table() -> None:
    md = "| A | B |\n|---|---|\n| 1 | 2 |"
    html = _render_html(md)
    assert "<table>" in html
    assert "<th>A</th>" in html
    assert "<td>1</td>" in html


def test_render_html_empty_string() -> None:
    html = _render_html("")
    assert "<div" in html  # wrapper present even with no content


def test_render_html_escapes_raw_html() -> None:
    """LLM-supplied raw HTML must be escaped, not passed through verbatim."""
    html = _render_html("Hi <script>alert(1)</script> <img src=x onerror=alert(1)>")
    assert "<script>" not in html
    assert "onerror=alert(1)>" not in html
    assert "&lt;script&gt;" in html


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_report(
    *, email_sent_at: datetime | None = None, md: str = "# Report\n\nBody"
) -> MagicMock:
    r = MagicMock()
    r.id = uuid.uuid4()
    r.report_md = md
    r.report_date = date(2026, 6, 6)
    r.status = "success"
    r.email_sent_at = email_sent_at
    return r


def _mock_settings() -> MagicMock:
    s = MagicMock()
    s.RESEND_API_KEY.get_secret_value.return_value = "re_test_key"
    s.EMAIL_FROM = "Portfonia <portfonia@physicalclue.us>"
    s.EMAIL_REPLY_TO = "portfonia@physicalclue.us"
    s.DEV_USER_EMAIL = "test@example.com"
    return s


# ---------------------------------------------------------------------------
# send_report_email
# ---------------------------------------------------------------------------


@patch("app.services.email_sender.get_settings")
@patch("app.services.email_sender.httpx.Client")
def test_send_success(mock_client_cls: MagicMock, mock_settings: MagicMock) -> None:
    mock_settings.return_value = _mock_settings()
    mock_resp = MagicMock()
    mock_resp.raise_for_status.return_value = None
    mock_client_cls.return_value.__enter__.return_value.post.return_value = mock_resp

    report = _make_report()
    session = MagicMock()

    result = send_report_email(report, session)

    assert result is True
    session.commit.assert_called_once()
    assert report.email_sent_at is not None


@patch("app.services.email_sender.get_settings")
@patch("app.services.email_sender.httpx.Client")
def test_send_dedup_skips(mock_client_cls: MagicMock, mock_settings: MagicMock) -> None:
    mock_settings.return_value = _mock_settings()
    already_sent = datetime(2026, 6, 6, 10, 0, tzinfo=UTC)
    report = _make_report(email_sent_at=already_sent)
    session = MagicMock()

    result = send_report_email(report, session)

    assert result is True
    mock_client_cls.assert_not_called()
    session.commit.assert_not_called()


@patch("app.services.email_sender.get_settings")
@patch("app.services.email_sender.httpx.Client")
def test_send_empty_md_returns_false(mock_client_cls: MagicMock, mock_settings: MagicMock) -> None:
    mock_settings.return_value = _mock_settings()
    report = _make_report(md="")
    session = MagicMock()

    result = send_report_email(report, session)

    assert result is False
    mock_client_cls.assert_not_called()


@patch("app.services.email_sender.get_settings")
@patch("app.services.email_sender.httpx.Client")
def test_send_http_error_returns_false(
    mock_client_cls: MagicMock, mock_settings: MagicMock
) -> None:
    import httpx as _httpx

    mock_settings.return_value = _mock_settings()

    mock_resp = MagicMock()
    mock_resp.status_code = 422
    mock_resp.text = "Unprocessable Entity"
    http_exc = _httpx.HTTPStatusError("422", request=MagicMock(), response=mock_resp)
    mock_resp.raise_for_status.side_effect = http_exc
    mock_client_cls.return_value.__enter__.return_value.post.return_value = mock_resp

    report = _make_report()
    session = MagicMock()

    result = send_report_email(report, session)

    assert result is False
    session.commit.assert_not_called()


@patch("app.services.email_sender.get_settings")
@patch("app.services.email_sender.httpx.Client")
def test_send_network_exception_returns_false(
    mock_client_cls: MagicMock, mock_settings: MagicMock
) -> None:
    import httpx as _httpx

    mock_settings.return_value = _mock_settings()
    mock_client_cls.return_value.__enter__.return_value.post.side_effect = _httpx.ConnectError(
        "connection refused"
    )

    report = _make_report()
    session = MagicMock()

    result = send_report_email(report, session)

    assert result is False
    session.commit.assert_not_called()


@patch("app.services.email_sender.get_settings")
@patch("app.services.email_sender.httpx.Client")
def test_send_subject_format(mock_client_cls: MagicMock, mock_settings: MagicMock) -> None:
    mock_settings.return_value = _mock_settings()
    mock_resp = MagicMock()
    mock_resp.raise_for_status.return_value = None
    post_mock = mock_client_cls.return_value.__enter__.return_value.post
    post_mock.return_value = mock_resp

    report = _make_report()
    session = MagicMock()
    send_report_email(report, session)

    call_kwargs = post_mock.call_args
    payload = call_kwargs.kwargs["json"] if "json" in call_kwargs.kwargs else call_kwargs[1]["json"]
    assert payload["subject"] == "Portfonia 财经分析报告 — 2026-06-06"
    assert payload["to"] == ["test@example.com"]


@patch("app.services.email_sender.get_settings")
@patch("app.services.email_sender.httpx.Client")
def test_send_sets_idempotency_key(mock_client_cls: MagicMock, mock_settings: MagicMock) -> None:
    """Idempotency-Key is content-addressed: report id + hash of the rendered body."""
    mock_settings.return_value = _mock_settings()
    mock_resp = MagicMock()
    mock_resp.raise_for_status.return_value = None
    post_mock = mock_client_cls.return_value.__enter__.return_value.post
    post_mock.return_value = mock_resp

    report = _make_report()
    send_report_email(report, MagicMock())

    headers = post_mock.call_args.kwargs["headers"]
    key = headers["Idempotency-Key"]
    assert key.startswith(f"report-{report.id}-")
    assert len(key) == len(f"report-{report.id}-") + 16


@patch("app.services.email_sender.get_settings")
@patch("app.services.email_sender.httpx.Client")
def test_idempotency_key_changes_with_content(
    mock_client_cls: MagicMock, mock_settings: MagicMock
) -> None:
    """A regenerated report with different content gets a different key, so a
    resend after regenerate is not rejected by Resend's stale-body 409 check."""
    mock_settings.return_value = _mock_settings()
    mock_resp = MagicMock()
    mock_resp.raise_for_status.return_value = None
    post_mock = mock_client_cls.return_value.__enter__.return_value.post
    post_mock.return_value = mock_resp

    report_v1 = _make_report(md="# Report\n\nFirst version")
    send_report_email(report_v1, MagicMock())
    key1 = post_mock.call_args.kwargs["headers"]["Idempotency-Key"]

    report_v2 = _make_report(md="# Report\n\nSecond version")
    report_v2.id = report_v1.id
    send_report_email(report_v2, MagicMock())
    key2 = post_mock.call_args.kwargs["headers"]["Idempotency-Key"]

    assert key1 != key2
    assert key1.startswith(f"report-{report_v1.id}-")
    assert key2.startswith(f"report-{report_v1.id}-")
