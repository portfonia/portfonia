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

from bs4 import BeautifulSoup, Tag

from app.services.email_sender import (
    _TAG_STYLES,
    _WRAPPER_TD_STYLE,
    _inline_body_styles,
    _render_html,
    send_report_email,
)

def _td(row: Tag) -> Tag:
    """Find a row's first td, asserting it exists (mypy-strict-friendly
    wrapper around BeautifulSoup's Tag | None find())."""
    cell = row.find("td")
    assert isinstance(cell, Tag), f"expected a <td> in {row!r}"
    return cell


# ---------------------------------------------------------------------------
# _render_html
# ---------------------------------------------------------------------------


def test_render_html_wraps_body() -> None:
    html = _render_html("# Hello\n\nWorld")
    assert "Hello" in html
    assert "World" in html
    # Bulletproof-table wrapper (issue #24), not a div.wrapper — Outlook's
    # Word engine centers via a fixed-width table, not CSS margin/max-width.
    assert 'width="720"' in html


def test_render_html_renders_table() -> None:
    md = "| A | B |\n|---|---|\n| 1 | 2 |"
    html = _render_html(md)
    assert "<table" in html
    assert "A</th>" in html
    assert "1</td>" in html


def test_render_html_empty_string() -> None:
    html = _render_html("")
    assert "<table" in html  # bulletproof wrapper present even with no content


def test_render_html_escapes_raw_html() -> None:
    """LLM-supplied raw HTML must be escaped, not passed through verbatim."""
    html = _render_html("Hi <script>alert(1)</script> <img src=x onerror=alert(1)>")
    assert "<script>" not in html
    assert "onerror=alert(1)>" not in html
    assert "&lt;script&gt;" in html


# ---------------------------------------------------------------------------
# _render_html — issue #24: Outlook/Gmail client-compat inlining
# ---------------------------------------------------------------------------


def test_render_html_inlines_heading_and_table_styles() -> None:
    """Critical layout styling must be inline `style="..."`, not solely in
    <head><style> — Outlook's Word rendering engine does not reliably apply
    <style> block rules."""
    html = _render_html("# Title\n\n| A | B |\n|---|---|\n| 1 | 2 |")

    soup = BeautifulSoup(html, "html.parser")
    h1 = soup.find("h1")
    assert h1 is not None
    assert h1.get("style"), "h1 must carry an inline style attribute"
    assert "color" in h1["style"]

    # The two outer bulletproof-layout tables carry role="presentation";
    # the markdown-rendered table does not — that distinguishes it.
    content_table = next(t for t in soup.find_all("table") if not t.has_attr("role"))
    td = content_table.find("td")
    assert td is not None
    assert td.get("style"), "td must carry an inline style attribute"
    assert "border" in td["style"]
    assert "padding" in td["style"]


def test_render_html_zebra_striping_is_inlined_not_nth_child() -> None:
    """tr:nth-child(even) is not reliably honored by Outlook — each row must
    carry its own explicit inline style rather than relying on the selector
    (the <style> block may still keep nth-child as a harmless enhancement for
    clients that do support it)."""
    md = "| A |\n|---|\n| 1 |\n| 2 |\n| 3 |"
    html = _render_html(md)

    soup = BeautifulSoup(html, "html.parser")
    tbody = soup.find("tbody")
    assert tbody is not None
    rows = tbody.find_all("tr")
    assert len(rows) == 3
    assert "background-color" not in str(_td(rows[0]).get("style", ""))
    assert "background-color" in str(_td(rows[1]).get("style", ""))  # second: striped
    assert "background-color" not in str(_td(rows[2]).get("style", ""))


def test_render_html_wrapper_uses_explicit_width_attribute() -> None:
    """The centering wrapper must use an explicit `width` table attribute,
    not rely solely on CSS max-width, which Outlook does not honor reliably."""
    html = _render_html("Body")
    soup = BeautifulSoup(html, "html.parser")
    inner_table = soup.find("table", attrs={"width": "720"})
    assert inner_table is not None


def test_render_html_preserves_content_inside_inlined_markup() -> None:
    """Inlining styles onto the markdown-rendered body must not lose or
    reorder content."""
    md = "# Title\n\nSome *emphasis* and a [link](https://example.com)."
    html = _render_html(md)
    assert "Title" in html
    assert "<em>emphasis</em>" in html
    assert 'href="https://example.com"' in html


# ---------------------------------------------------------------------------
# _render_html — PR #117 Grok review fixes
# ---------------------------------------------------------------------------


def test_zebra_striping_paints_cells_not_only_row() -> None:
    """Grok review (PR #117): Word-based Outlook often ignores `background`
    set on a <tr>. The even-row fill must also land on each td/th cell
    (background-color longhand + bgcolor attribute), not only the row."""
    md = "| A |\n|---|\n| 1 |\n| 2 |\n| 3 |"
    html = _render_html(md)

    soup = BeautifulSoup(html, "html.parser")
    tbody = soup.find("tbody")
    assert tbody is not None
    rows = tbody.find_all("tr")

    unstriped_cell = _td(rows[0])
    assert "background-color" not in str(unstriped_cell.get("style", ""))

    striped_cell = _td(rows[1])
    assert "background-color" in str(striped_cell.get("style", ""))
    assert striped_cell.get("bgcolor"), "striped cell must also carry a bgcolor attribute"


def test_zebra_striping_falls_back_to_bare_tr_children() -> None:
    """Grok review (PR #117): markdown-it always emits thead/tbody, but
    _inline_body_styles should not silently no-op on a bare <table><tr>...
    structure (e.g. hand-built HTML) with no thead/tbody wrapper."""
    bare_table_html = "<table><tr><td>1</td></tr><tr><td>2</td></tr><tr><td>3</td></tr></table>"
    result = _inline_body_styles(bare_table_html)

    soup = BeautifulSoup(result, "html.parser")
    table = soup.find("table")
    assert isinstance(table, Tag)
    rows = table.find_all("tr", recursive=False)
    assert len(rows) == 3
    assert "background-color" not in str(_td(rows[0]).get("style", ""))
    assert "background-color" in str(_td(rows[1]).get("style", ""))
    assert "background-color" not in str(_td(rows[2]).get("style", ""))


def test_head_style_block_matches_tag_styles_single_source() -> None:
    """Grok review (PR #117): the <head><style> per-tag rules and _TAG_STYLES
    (used for inline injection) must not silently drift — both must be
    generated from the same source, so every _TAG_STYLES declaration is also
    present in the <style> block for the same tag."""
    html = _render_html("# T\n\nx")
    head = html.split("<style>", 1)[1].split("</style>", 1)[0]

    for tag, style in _TAG_STYLES.items():
        rule = head.split(f"{tag} {{", 1)
        assert len(rule) == 2, f"<style> block has no rule for {tag!r}"
        rule_body = rule[1].split("}", 1)[0]
        assert style.rstrip(";").replace(";", "; ") in rule_body or all(
            decl.strip() in rule_body for decl in style.split(";") if decl.strip()
        ), f"{tag!r} inline style {style!r} not reflected in <style> block: {rule_body!r}"


def test_wrapper_td_style_constant_is_real_and_used() -> None:
    """Grok review nit (PR #117): the module comment claims a load-bearing
    `_WRAPPER_TD_STYLE` constant — it must actually exist and be used in the
    rendered wrapper, not just be a name in a comment."""
    html = _render_html("Body")
    assert _WRAPPER_TD_STYLE in html


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
    s.OUTPUT_LANG = "zh"  # matches Ring 0 default; subject resolves via this (issue #90 review)
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
    session.execute.return_value.rowcount = 1

    result = send_report_email(report, session)

    assert result is True
    session.execute.assert_called_once()
    session.commit.assert_called_once()
    assert report.email_sent_at is not None


@patch("app.services.email_sender.get_settings")
@patch("app.services.email_sender.httpx.Client")
def test_send_concurrent_dedup(mock_client_cls: MagicMock, mock_settings: MagicMock) -> None:
    """rowcount == 0 means another sender already committed email_sent_at — return True."""
    mock_settings.return_value = _mock_settings()
    mock_resp = MagicMock()
    mock_resp.raise_for_status.return_value = None
    mock_client_cls.return_value.__enter__.return_value.post.return_value = mock_resp

    report = _make_report()
    session = MagicMock()
    session.execute.return_value.rowcount = 0

    result = send_report_email(report, session)

    assert result is True
    session.commit.assert_called_once()
    # in-memory object NOT updated — the other sender owns the state
    assert report.email_sent_at is None


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
def test_send_commit_failure_returns_false_and_alerts(
    mock_client_cls: MagicMock, mock_settings: MagicMock
) -> None:
    """Email delivered by Resend but commit fails → False + ops alert sent."""
    mock_settings.return_value = _mock_settings()
    mock_resp = MagicMock()
    mock_resp.raise_for_status.return_value = None
    mock_resp.json.return_value = {"id": "resend-abc"}
    mock_client_cls.return_value.__enter__.return_value.post.return_value = mock_resp

    report = _make_report()
    session = MagicMock()
    session.commit.side_effect = Exception("DB connection lost")

    with patch("app.services.email_sender.send_ops_alert") as mock_alert:
        result = send_report_email(report, session)

    assert result is False
    session.commit.assert_called_once()
    session.rollback.assert_called_once()
    mock_alert.assert_called_once()
    subject = mock_alert.call_args.kwargs["subject"]
    assert "unconfirmed" in subject


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
def test_send_subject_resolves_via_output_lang(
    mock_client_cls: MagicMock, mock_settings: MagicMock
) -> None:
    """Subject follows OUTPUT_LANG, not a hardcoded zh-Hans literal (issue #90 review)."""
    settings = _mock_settings()
    settings.OUTPUT_LANG = "en"
    mock_settings.return_value = settings
    mock_resp = MagicMock()
    mock_resp.raise_for_status.return_value = None
    post_mock = mock_client_cls.return_value.__enter__.return_value.post
    post_mock.return_value = mock_resp

    report = _make_report()
    session = MagicMock()
    send_report_email(report, session)

    call_kwargs = post_mock.call_args
    payload = call_kwargs.kwargs["json"] if "json" in call_kwargs.kwargs else call_kwargs[1]["json"]
    assert payload["subject"] == "Portfonia Financial Analysis Report — 2026-06-06"


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
