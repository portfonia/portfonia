"""Tests for email_sender (Stage G).

Strategy:
- _render_html: verify table support, wrapper, no crash.
- send_report_email: mock httpx.Client + settings; cover happy path,
  dedup guard, empty report_md, HTTP error, network exception.
- DB session is mocked (email_sender has no schema-dependent queries).
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from unittest.mock import MagicMock, patch

from bs4 import BeautifulSoup, Tag
from sqlalchemy.orm import Session

from app.models.user import User
from app.services.email_sender import (
    _TAG_STYLES,
    _WRAPPER_TD_STYLE,
    _inline_body_styles,
    _render_html,
    send_report_email,
    send_verification_email,
)
from app.services.unsubscribe_token import verify_token


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
    """Grok review round 2 (PR #117): assert real BS4 elements, not bare
    substrings — a substring check doesn't prove markdown structure (h1/p)
    survived style-inlining, only that the words appear somewhere."""
    html = _render_html("# Hello\n\nWorld")
    soup = BeautifulSoup(html, "html.parser")
    h1 = soup.find("h1")
    assert h1 is not None and h1.get_text() == "Hello"
    p = soup.find("p")
    assert p is not None and p.get_text() == "World"
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


def test_zebra_fill_appended_after_base_cell_style() -> None:
    """Grok review round 2 (PR #117): zebra background-color must be
    appended after the cell's base style, not prepended — CSS resolves
    same-attribute conflicts by last-declaration-wins, so appending is what
    guarantees the zebra fill can't be silently overridden by a future
    `_TAG_STYLES["td"]` background."""
    md = "| A |\n|---|\n| 1 |\n| 2 |"
    html = _render_html(md)

    soup = BeautifulSoup(html, "html.parser")
    tbody = soup.find("tbody")
    assert tbody is not None
    striped_cell = _td(tbody.find_all("tr")[1])
    style = str(striped_cell.get("style", ""))

    base_pos = style.find("border")
    zebra_pos = style.find("background-color")
    assert base_pos != -1 and zebra_pos != -1
    assert zebra_pos > base_pos, f"zebra fill must come after base style: {style!r}"


def test_render_html_cjk_content_survives_bs4_round_trip() -> None:
    """Grok review round 2 (PR #117): production reports render in zh-Hans by
    default (Ring 0) — existing tests only used ASCII, so a BeautifulSoup
    serialization quirk on Chinese text wouldn't be caught. Also checks a
    literal ampersand round-trips as an entity rather than raw."""
    md = "# 财经分析报告\n\n持仓 A & B 的表现"
    html = _render_html(md)

    soup = BeautifulSoup(html, "html.parser")
    h1 = soup.find("h1")
    assert h1 is not None and h1.get_text() == "财经分析报告"
    p = soup.find("p")
    assert p is not None and p.get_text() == "持仓 A & B 的表现"
    assert "&amp;" in html


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


# B3: fixed so _make_report's default user_id and _mock_settings' DEV_USER_ID
# resolve to the same identity — every pre-B3 test exercises the "known
# recipient" path unless it explicitly passes a different user_id.
_DEV_USER_ID_STR = "00000000-0000-0000-0000-000000000001"


def _make_report(
    *,
    email_sent_at: datetime | None = None,
    md: str = "# Report\n\nBody",
    user_id: uuid.UUID | None = None,
) -> MagicMock:
    r = MagicMock()
    r.id = uuid.uuid4()
    r.user_id = user_id if user_id is not None else uuid.UUID(_DEV_USER_ID_STR)
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
    s.DEV_USER_ID = _DEV_USER_ID_STR
    s.DEV_USER_EMAIL = "test@example.com"
    s.OUTPUT_LANG = "zh"  # matches Ring 0 default; subject resolves via this (issue #90 review)
    s.FRONTEND_URL = "https://portfonia.com"
    return s


# ---------------------------------------------------------------------------
# send_report_email
# ---------------------------------------------------------------------------


@patch(
    "app.services.email_sender.recipient_email_with_purpose",
    return_value=("test@example.com", "account_email"),
)
@patch("app.services.email_sender.get_settings")
@patch("app.services.email_sender.httpx.Client")
def test_send_success(
    mock_client_cls: MagicMock, mock_settings: MagicMock, mock_user_dir_settings: MagicMock
) -> None:
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


@patch(
    "app.services.email_sender.recipient_email_with_purpose",
    return_value=("test@example.com", "account_email"),
)
@patch("app.services.email_sender.get_settings")
@patch("app.services.email_sender.httpx.Client")
def test_send_persists_provider_message_id(
    mock_client_cls: MagicMock, mock_settings: MagicMock, mock_user_dir_settings: MagicMock
) -> None:
    """issue #45: Resend's message id is persisted, not just logged."""
    mock_settings.return_value = _mock_settings()

    mock_resp = MagicMock()
    mock_resp.raise_for_status.return_value = None
    mock_resp.json.return_value = {"id": "resend-abc123"}
    mock_client_cls.return_value.__enter__.return_value.post.return_value = mock_resp

    report = _make_report()
    session = MagicMock()
    session.execute.return_value.rowcount = 1

    result = send_report_email(report, session)

    assert result is True
    update_stmt = session.execute.call_args[0][0]
    values_by_name = {col.name: bind.value for col, bind in update_stmt._values.items()}
    assert values_by_name["provider_message_id"] == "resend-abc123"
    assert report.provider_message_id == "resend-abc123"


@patch(
    "app.services.email_sender.recipient_email_with_purpose",
    return_value=("test@example.com", "account_email"),
)
@patch("app.services.email_sender.get_settings")
@patch("app.services.email_sender.httpx.Client")
def test_send_missing_resend_id_persists_none(
    mock_client_cls: MagicMock, mock_settings: MagicMock, mock_user_dir_settings: MagicMock
) -> None:
    """A Resend response without an id must not fall back to the literal string
    "unknown" in the DB — that would be indistinguishable from a real id."""
    mock_settings.return_value = _mock_settings()

    mock_resp = MagicMock()
    mock_resp.raise_for_status.return_value = None
    mock_resp.json.return_value = {}
    mock_client_cls.return_value.__enter__.return_value.post.return_value = mock_resp

    report = _make_report()
    session = MagicMock()
    session.execute.return_value.rowcount = 1

    result = send_report_email(report, session)

    assert result is True
    update_stmt = session.execute.call_args[0][0]
    values_by_name = {col.name: bind.value for col, bind in update_stmt._values.items()}
    assert values_by_name["provider_message_id"] is None
    assert report.provider_message_id is None


@patch(
    "app.services.email_sender.recipient_email_with_purpose",
    return_value=("test@example.com", "account_email"),
)
@patch("app.services.email_sender.get_settings")
@patch("app.services.email_sender.httpx.Client")
def test_send_concurrent_dedup(
    mock_client_cls: MagicMock, mock_settings: MagicMock, mock_user_dir_settings: MagicMock
) -> None:
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


@patch("app.services.email_sender.send_ops_alert")
@patch("app.services.email_sender.recipient_email_with_purpose", return_value=None)
@patch("app.services.email_sender.get_settings")
@patch("app.services.email_sender.httpx.Client")
def test_send_unknown_recipient_fails_closed(
    mock_client_cls: MagicMock,
    mock_settings: MagicMock,
    _recipient: MagicMock,
    mock_alert: MagicMock,
) -> None:
    """A report whose user_id doesn't resolve to a known recipient must
    never fall back to a default inbox. Fail closed: no send, ops alert,
    email_sent_at stays null."""
    mock_settings.return_value = _mock_settings()
    report = _make_report(user_id=uuid.uuid4())
    session = MagicMock()

    result = send_report_email(report, session)

    assert result is False
    mock_client_cls.assert_not_called()
    session.commit.assert_not_called()
    assert report.email_sent_at is None
    mock_alert.assert_called_once()


# ---------------------------------------------------------------------------
# send_report_email — resolved-is-None split (issue #276): the failure path
# re-checks the user row to tell "row missing/inactive" (original subject,
# a real bug signal) from "active user, no verified address" (new subject,
# expected once the verification gate ships). These tests run the REAL
# recipient_email_with_purpose against a real User row via session.get —
# mocking the resolver (as every test above does) cannot reach this branch.
# ---------------------------------------------------------------------------


def _real_user_row(user_id: uuid.UUID, **overrides: object) -> User:
    return User(
        id=user_id,
        auth_provider="supabase",
        auth_subject=f"sub-{user_id}",
        email="acct@example.com",
        status="active",
        locale="zh",
        base_currency="USD",
        report_cadence="mwf",
        **overrides,
    )


@patch("app.services.email_sender.send_ops_alert")
@patch("app.services.email_sender.get_settings")
@patch("app.services.email_sender.httpx.Client")
def test_send_inactive_user_alerts_unresolved_subject(
    mock_client_cls: MagicMock,
    mock_settings: MagicMock,
    mock_alert: MagicMock,
    db_session: Session,
) -> None:
    """Missing/inactive user row keeps the ORIGINAL subject unchanged —
    that case is still a real bug signal, not the routine no-verification
    case."""
    mock_settings.return_value = _mock_settings()
    user_id = uuid.uuid4()
    report = _make_report(user_id=user_id)

    result = send_report_email(report, db_session)

    assert result is False
    mock_client_cls.assert_not_called()
    assert report.email_sent_at is None
    mock_alert.assert_called_once()
    assert (
        mock_alert.call_args.kwargs["subject"]
        == "Portfonia: report recipient could not be resolved"
    )


@patch("app.services.email_sender.send_ops_alert")
@patch("app.services.email_sender.get_settings")
@patch("app.services.email_sender.httpx.Client")
def test_send_active_unverified_user_alerts_no_verified_recipient(
    mock_client_cls: MagicMock,
    mock_settings: MagicMock,
    mock_alert: MagicMock,
    db_session: Session,
) -> None:
    """Active user whose every address is unverified now resolves to None
    (issue #276 Layer 2) and must fire the NEW distinct subject — not the
    row-missing subject, which is a bug signal, and not silence."""
    mock_settings.return_value = _mock_settings()
    user_id = uuid.uuid4()
    db_session.add(_real_user_row(user_id))
    db_session.flush()
    report = _make_report(user_id=user_id)

    result = send_report_email(report, db_session)

    assert result is False
    mock_client_cls.assert_not_called()
    assert report.email_sent_at is None
    mock_alert.assert_called_once()
    assert (
        mock_alert.call_args.kwargs["subject"] == "Portfonia ops: report has no verified recipient"
    )
    # PR #288 review: this branch only runs AFTER a Report row exists and
    # delivery was refused (admin/self-service generate of an unverified
    # user, or the fan-out-verified/send-unverified race) — the body must
    # say "generated, not emailed", never claim generation was skipped.
    body = mock_alert.call_args.kwargs["body"]
    assert "generated" in body
    assert "NOT emailed" in body
    assert "no report was generated" not in body


@patch(
    "app.services.email_sender.recipient_email_with_purpose",
    return_value=("test@example.com", "account_email"),
)
@patch("app.services.email_sender.get_settings")
@patch("app.services.email_sender.httpx.Client")
def test_send_http_error_returns_false(
    mock_client_cls: MagicMock, mock_settings: MagicMock, mock_user_dir_settings: MagicMock
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


@patch(
    "app.services.email_sender.recipient_email_with_purpose",
    return_value=("test@example.com", "account_email"),
)
@patch("app.services.email_sender.get_settings")
@patch("app.services.email_sender.httpx.Client")
def test_send_network_exception_returns_false(
    mock_client_cls: MagicMock, mock_settings: MagicMock, mock_user_dir_settings: MagicMock
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


@patch(
    "app.services.email_sender.recipient_email_with_purpose",
    return_value=("test@example.com", "account_email"),
)
@patch("app.services.email_sender.get_settings")
@patch("app.services.email_sender.httpx.Client")
def test_send_commit_failure_returns_false_and_alerts(
    mock_client_cls: MagicMock, mock_settings: MagicMock, mock_user_dir_settings: MagicMock
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
    # issue #45 review follow-up: manual-repair instructions must cover both
    # halves of the pair, not just email_sent_at — otherwise a manual fix
    # leaves provider_message_id stale/NULL even after confirming delivery.
    body = mock_alert.call_args.kwargs["body"]
    assert "provider_message_id" in body


@patch(
    "app.services.email_sender.recipient_email_with_purpose",
    return_value=("test@example.com", "account_email"),
)
@patch("app.services.email_sender.get_settings")
@patch("app.services.email_sender.httpx.Client")
def test_send_subject_format(
    mock_client_cls: MagicMock, mock_settings: MagicMock, mock_user_dir_settings: MagicMock
) -> None:
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


@patch(
    "app.services.email_sender.recipient_email_with_purpose",
    return_value=("test@example.com", "account_email"),
)
@patch("app.services.email_sender.get_settings")
@patch("app.services.email_sender.httpx.Client")
def test_send_subject_resolves_via_output_lang(
    mock_client_cls: MagicMock, mock_settings: MagicMock, mock_user_dir_settings: MagicMock
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


@patch(
    "app.services.email_sender.recipient_email_with_purpose",
    return_value=("test@example.com", "account_email"),
)
@patch("app.services.email_sender.get_settings")
@patch("app.services.email_sender.httpx.Client")
def test_send_sets_idempotency_key(
    mock_client_cls: MagicMock, mock_settings: MagicMock, mock_user_dir_settings: MagicMock
) -> None:
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


@patch(
    "app.services.email_sender.recipient_email_with_purpose",
    return_value=("test@example.com", "account_email"),
)
@patch("app.services.email_sender.get_settings")
@patch("app.services.email_sender.httpx.Client")
def test_idempotency_key_changes_with_content(
    mock_client_cls: MagicMock, mock_settings: MagicMock, mock_user_dir_settings: MagicMock
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


@patch(
    "app.services.email_sender.recipient_email_with_purpose",
    return_value=("test@example.com", "account_email"),
)
@patch("app.services.email_sender.get_settings")
@patch("app.services.email_sender.httpx.Client")
def test_send_footer_copy_unsubscribe_register_en(
    mock_client_cls: MagicMock, mock_settings: MagicMock, _recipient: MagicMock
) -> None:
    """issue #289 item 1 (en): the footer must explain what the report is,
    that it was delivered per the user's own configuration, and what the
    link does to future delivery — not just 'revoke verification'. The
    register is plain 'unsubscribe', consistent with the /unsubscribe page."""
    settings = _mock_settings()
    settings.OUTPUT_LANG = "en"
    mock_settings.return_value = settings

    mock_resp = MagicMock()
    mock_resp.raise_for_status.return_value = None
    post_mock = mock_client_cls.return_value.__enter__.return_value.post
    post_mock.return_value = mock_resp

    send_report_email(_make_report(), MagicMock())

    payload = post_mock.call_args.kwargs["json"]
    assert "This report was delivered by Portfonia to the address you configured" in payload["html"]
    assert "You can unsubscribe this address to stop receiving reports here" in payload["text"]
    assert "unsubscribe?token=" in payload["html"]


@patch(
    "app.services.email_sender.recipient_email_with_purpose",
    return_value=("test@example.com", "account_email"),
)
@patch("app.services.email_sender.get_settings")
@patch("app.services.email_sender.httpx.Client")
def test_send_footer_copy_unsubscribe_register_zh(
    mock_client_cls: MagicMock, mock_settings: MagicMock, _recipient: MagicMock
) -> None:
    """issue #289 item 1 (zh-Hans branch): same register as the en footer —
    explains delivery and the unsubscribe action, no 'revoke verification'
    phrasing forced in."""
    mock_settings.return_value = _mock_settings()  # OUTPUT_LANG=zh

    mock_resp = MagicMock()
    mock_resp.raise_for_status.return_value = None
    post_mock = mock_client_cls.return_value.__enter__.return_value.post
    post_mock.return_value = mock_resp

    send_report_email(_make_report(), MagicMock())

    payload = post_mock.call_args.kwargs["json"]
    assert "本报告由 Portfonia 根据您提供的信息" in payload["html"]
    assert "您可以退订此邮箱" in payload["text"]
    assert "unsubscribe?token=" in payload["html"]


@patch(
    "app.services.email_sender.recipient_email_with_purpose",
    return_value=("test@example.com", "account_email"),
)
@patch("app.services.email_sender.get_settings")
@patch("app.services.email_sender.httpx.Client")
def test_send_includes_text_alternative_and_unsubscribe_headers(
    mock_client_cls: MagicMock, mock_settings: MagicMock, _recipient: MagicMock
) -> None:
    """issue #257: multipart text body + List-Unsubscribe headers (Resend
    `text` + `headers` fields, confirmed against
    https://resend.com/docs/api-reference/emails/send-email)."""
    mock_settings.return_value = _mock_settings()

    mock_resp = MagicMock()
    mock_resp.raise_for_status.return_value = None
    post_mock = mock_client_cls.return_value.__enter__.return_value.post
    post_mock.return_value = mock_resp

    md = (
        "# Report\n\nBody\n\n"
        "**Disclaimer** This does not constitute investment advice. "
        "Consult a qualified financial advisor."
    )
    report = _make_report(md=md)
    send_report_email(report, MagicMock())

    payload = post_mock.call_args.kwargs["json"]
    assert "html" in payload
    assert isinstance(payload["text"], str)
    assert "Body" in payload["text"]
    assert "does not constitute investment advice" in payload["text"]

    headers = payload["headers"]
    assert headers["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    unsub = headers["List-Unsubscribe"]
    prefix = "<https://portfonia.com/unsubscribe?token="
    assert unsub.startswith(prefix) and unsub.endswith(">")
    token = unsub[len(prefix) : -1]
    claims = verify_token(token)
    assert claims is not None
    assert claims.email == "test@example.com"
    assert claims.purpose == "account_email"
    assert claims.user_id == report.user_id
    assert token in payload["text"]
    assert "unsubscribe?token=" in payload["html"]


@patch(
    "app.services.email_sender.recipient_email_with_purpose",
    return_value=("delivery@example.com", "delivery_email"),
)
@patch("app.services.email_sender.get_settings")
@patch("app.services.email_sender.httpx.Client")
def test_send_unsubscribe_token_uses_delivery_purpose(
    mock_client_cls: MagicMock, mock_settings: MagicMock, _recipient: MagicMock
) -> None:
    mock_settings.return_value = _mock_settings()
    mock_resp = MagicMock()
    mock_resp.raise_for_status.return_value = None
    post_mock = mock_client_cls.return_value.__enter__.return_value.post
    post_mock.return_value = mock_resp

    report = _make_report()
    send_report_email(report, MagicMock())

    payload = post_mock.call_args.kwargs["json"]
    unsub = payload["headers"]["List-Unsubscribe"]
    token = unsub.removeprefix("<https://portfonia.com/unsubscribe?token=").removesuffix(">")
    claims = verify_token(token)
    assert claims is not None
    assert claims.purpose == "delivery_email"
    assert claims.email == "delivery@example.com"


@patch(
    "app.services.email_sender.recipient_email_with_purpose",
    return_value=("test@example.com", "account_email"),
)
@patch("app.services.email_sender.get_settings")
@patch("app.services.email_sender.httpx.Client")
def test_idempotency_key_and_token_stable_one_second_apart(
    mock_client_cls: MagicMock, mock_settings: MagicMock, _recipient: MagicMock
) -> None:
    """PR #279 review: html_body (hashed into Idempotency-Key) must not
    change when `now` ticks one second inside Resend's 24h window."""
    mock_settings.return_value = _mock_settings()
    mock_resp = MagicMock()
    mock_resp.raise_for_status.return_value = None
    post_mock = mock_client_cls.return_value.__enter__.return_value.post
    post_mock.return_value = mock_resp

    report = _make_report(md="# Report\n\nSame content")
    clock = {"t": datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC)}

    def _frozen_now() -> datetime:
        return clock["t"]

    prefix = "<https://portfonia.com/unsubscribe?token="
    with patch("app.services.email_sender._now_utc", _frozen_now):
        send_report_email(report, MagicMock())
        key1 = post_mock.call_args.kwargs["headers"]["Idempotency-Key"]
        token1 = post_mock.call_args.kwargs["json"]["headers"]["List-Unsubscribe"]
        token1 = token1[len(prefix) : -1]
        report.email_sent_at = None
        clock["t"] = clock["t"] + timedelta(seconds=1)
        send_report_email(report, MagicMock())
        key2 = post_mock.call_args.kwargs["headers"]["Idempotency-Key"]
        token2 = post_mock.call_args.kwargs["json"]["headers"]["List-Unsubscribe"]
        token2 = token2[len(prefix) : -1]

    assert key1 == key2
    assert token1 == token2


# ---------------------------------------------------------------------------
# send_verification_email (issue #260)
# ---------------------------------------------------------------------------


@patch("app.services.email_sender.get_settings")
@patch("app.services.email_sender.httpx.Client")
def test_send_verification_email_defaults_to_english(
    mock_client_cls: MagicMock, mock_settings: MagicMock
) -> None:
    """Regression (review, PR #261 round 2): the original version hardcoded
    English regardless of the caller — this repo's mandatory i18n policy
    applies to this email too."""
    mock_settings.return_value = _mock_settings()
    mock_resp = MagicMock()
    mock_resp.raise_for_status.return_value = None
    mock_resp.json.return_value = {"id": "resend-id-1"}
    post_mock = mock_client_cls.return_value.__enter__.return_value.post
    post_mock.return_value = mock_resp

    result = send_verification_email("a@example.com", "tok-1")

    assert result == "resend-id-1"
    payload = post_mock.call_args.kwargs["json"]
    assert payload["subject"] == "Verify your email — Portfonia"
    assert "https://portfonia.com/verify-email?token=tok-1" in payload["text"]


@patch("app.services.email_sender.get_settings")
@patch("app.services.email_sender.httpx.Client")
def test_send_verification_email_uses_zh_copy_for_zh_locale(
    mock_client_cls: MagicMock, mock_settings: MagicMock
) -> None:
    mock_settings.return_value = _mock_settings()
    mock_resp = MagicMock()
    mock_resp.raise_for_status.return_value = None
    mock_resp.json.return_value = {"id": "resend-id-1"}
    post_mock = mock_client_cls.return_value.__enter__.return_value.post
    post_mock.return_value = mock_resp

    send_verification_email("a@example.com", "tok-1", locale="zh")

    payload = post_mock.call_args.kwargs["json"]
    assert payload["subject"] == "验证你的邮箱 — Portfonia"
    assert "https://portfonia.com/verify-email?token=tok-1" in payload["text"]


@patch("app.services.email_sender.get_settings")
@patch("app.services.email_sender.httpx.Client")
def test_send_verification_email_falls_back_to_english_for_unknown_locale(
    mock_client_cls: MagicMock, mock_settings: MagicMock
) -> None:
    """zh-Hant isn't in the dict yet (not exposed to users at the UI layer
    either) — an unrecognized locale must not crash, it degrades to en."""
    mock_settings.return_value = _mock_settings()
    mock_resp = MagicMock()
    mock_resp.raise_for_status.return_value = None
    mock_resp.json.return_value = {"id": "resend-id-1"}
    post_mock = mock_client_cls.return_value.__enter__.return_value.post
    post_mock.return_value = mock_resp

    send_verification_email("a@example.com", "tok-1", locale="zh-Hant")

    payload = post_mock.call_args.kwargs["json"]
    assert payload["subject"] == "Verify your email — Portfonia"


@patch("app.services.email_sender.get_settings")
@patch("app.services.email_sender.httpx.Client")
def test_send_verification_email_returns_none_on_failure(
    mock_client_cls: MagicMock, mock_settings: MagicMock
) -> None:
    mock_settings.return_value = _mock_settings()
    mock_client_cls.return_value.__enter__.return_value.post.side_effect = Exception("boom")

    result = send_verification_email("a@example.com", "tok-1")

    assert result is None
