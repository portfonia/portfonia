"""Email delivery service (Stage G).

Sends generated reports via Resend REST API (httpx direct, no SDK).
On success, writes email_sent_at to the reports row.
On failure, logs and returns False — report persistence must not be rolled back
by a delivery failure.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime

import httpx
from bs4 import BeautifulSoup
from markdown_it import MarkdownIt
from sqlalchemy import update as sa_update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.report import Report
from app.services.i18n_glossary import load_i18n_glossary, locale_for_output_lang

logger = logging.getLogger(__name__)

_RESEND_SEND_URL = "https://api.resend.com/emails"

# Outlook's Word rendering engine does not reliably apply <head><style> rules
# (issue #24) — this block is kept only as an enhancement for clients that DO
# support it (Gmail web/app, Apple Mail, ...). The load-bearing copy of every
# rule below lives inline via _TAG_STYLES / _ZEBRA_EVEN_ROW_STYLE /
# _WRAPPER_TD_STYLE, applied by _inline_body_styles. Outer braces are doubled
# to survive .format().
_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<style>
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto,
                 'Helvetica Neue', Arial, sans-serif;
    font-size: 15px;
    line-height: 1.65;
    color: #1a1a1a;
    background: #ffffff;
    margin: 0;
    padding: 0;
  }}
  h1 {{
    font-size: 1.45em;
    color: #111;
    border-bottom: 2px solid #e8e8e8;
    padding-bottom: 8px;
  }}
  h2 {{
    font-size: 1.15em;
    color: #222;
    margin-top: 2em;
    border-bottom: 1px solid #ebebeb;
    padding-bottom: 4px;
  }}
  h3 {{ font-size: 1.0em; color: #333; margin-top: 1.4em; }}
  table {{
    border-collapse: collapse;
    table-layout: fixed;
    width: 100%;
    margin: 1em 0;
    font-size: 0.88em;
  }}
  th, td {{
    border: 1px solid #d8d8d8;
    padding: 6px 10px;
    text-align: left;
    vertical-align: top;
  }}
  th {{ background: #f5f5f5; font-weight: 600; }}
  tr:nth-child(even) {{ background: #fafafa; }}
  blockquote {{
    border-left: 3px solid #ccc;
    margin: 1em 0;
    padding: 0.4em 1em;
    color: #555;
  }}
  code {{
    background: #f3f3f3;
    padding: 1px 4px;
    border-radius: 3px;
    font-size: 0.88em;
  }}
  pre {{ background: #f3f3f3; padding: 12px; border-radius: 4px; overflow-x: auto; }}
  p {{ margin: 0.7em 0; }}
  ul, ol {{ padding-left: 1.5em; }}
  hr {{ border: none; border-top: 1px solid #e0e0e0; margin: 2em 0; }}
</style>
</head>
<body style="margin:0;padding:0;background:#ffffff;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif;font-size:15px;line-height:1.65;color:#1a1a1a;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="width:100%;background:#ffffff;">
<tr>
<td align="center" style="padding:0;">
<table role="presentation" width="720" cellpadding="0" cellspacing="0" border="0" style="width:720px;max-width:720px;">
<tr>
<td style="padding:32px 24px;">
{body}
</td>
</tr>
</table>
</td>
</tr>
</table>
</body>
</html>
"""

# Render report Markdown to HTML.
# - html=False escapes any raw HTML in the LLM output (defense against
#   <script>/<img onerror=...> injection if the report is ever viewed in a
#   browser or admin UI; email clients usually strip it but we do not rely on that).
# - enable("table") restores GFM tables, which the commonmark baseline disables.
_md = MarkdownIt("commonmark", {"html": False}).enable("table")

# Inline copy of the <head><style> rules above, keyed by tag name — Outlook's
# Word engine ignores <style> blocks, so every client-critical rule must also
# be present as an inline `style="..."` attribute (issue #24).
_TAG_STYLES: dict[str, str] = {
    "h1": "font-size:1.45em;color:#111;border-bottom:2px solid #e8e8e8;padding-bottom:8px;margin:0 0 0.5em;",
    "h2": "font-size:1.15em;color:#222;margin-top:2em;border-bottom:1px solid #ebebeb;padding-bottom:4px;",
    "h3": "font-size:1.0em;color:#333;margin-top:1.4em;",
    "table": "border-collapse:collapse;table-layout:fixed;width:100%;margin:1em 0;font-size:0.88em;",
    "th": "border:1px solid #d8d8d8;padding:6px 10px;text-align:left;vertical-align:top;background:#f5f5f5;font-weight:600;",
    "td": "border:1px solid #d8d8d8;padding:6px 10px;text-align:left;vertical-align:top;",
    "blockquote": "border-left:3px solid #ccc;margin:1em 0;padding:0.4em 1em;color:#555;",
    "code": "background:#f3f3f3;padding:1px 4px;font-size:0.88em;",
    "pre": "background:#f3f3f3;padding:12px;overflow-x:auto;",
    "p": "margin:0.7em 0;",
    "ul": "padding-left:1.5em;margin:0.7em 0;",
    "ol": "padding-left:1.5em;margin:0.7em 0;",
    "hr": "border:none;border-top:1px solid #e0e0e0;margin:2em 0;",
}

# tr:nth-child(even) is not reliably honored by Outlook — applied per-row
# instead, scoped per thead/tbody to match nth-child's own per-parent counting.
_ZEBRA_EVEN_ROW_STYLE = "background:#fafafa;"


def _inline_body_styles(body_html: str) -> str:
    """Duplicate the <head><style> rules onto each tag as inline `style=`
    attributes, and inline table-row zebra striping in place of
    `tr:nth-child(even)` — both required for Outlook's Word rendering engine,
    which does not reliably apply <style> blocks or nth-child selectors
    (issue #24)."""
    soup = BeautifulSoup(body_html, "html.parser")

    for tag_name, style in _TAG_STYLES.items():
        for tag in soup.find_all(tag_name):
            existing = tag.get("style", "")
            tag["style"] = f"{style}{existing}"

    for table in soup.find_all("table"):
        for row_group in table.find_all(["thead", "tbody"], recursive=False):
            for i, row in enumerate(row_group.find_all("tr", recursive=False)):
                if i % 2 == 1:
                    existing = row.get("style", "")
                    row["style"] = f"{_ZEBRA_EVEN_ROW_STYLE}{existing}"

    return str(soup)


def _render_html(markdown: str) -> str:
    body = _inline_body_styles(_md.render(markdown))
    return _HTML_TEMPLATE.format(body=body)


def send_report_email(report: Report, session: Session) -> bool:
    """Send *report* by email via Resend.

    Returns True on success (including already-sent).
    Returns False on delivery error — never raises.
    """
    # G3: dedup guard
    if report.email_sent_at is not None:
        logger.info(
            "report %s: already sent at %s — skipping",
            report.id,
            report.email_sent_at,
        )
        return True

    if not report.report_md:
        logger.warning("report %s: report_md is empty, cannot send", report.id)
        return False

    settings = get_settings()
    api_key = settings.RESEND_API_KEY.get_secret_value()
    recipient = settings.DEV_USER_EMAIL

    report_date_str = (
        report.report_date.strftime("%Y-%m-%d")
        if report.report_date
        else datetime.now(tz=UTC).strftime("%Y-%m-%d")
    )
    # Resolved via OUTPUT_LANG, matching the locale report_generator._translate_md
    # renders the body in (PR #91 review — was hardcoded to zh-Hans regardless of
    # OUTPUT_LANG, which happened to match Ring 0's only supported value but would
    # have been the first place stuck on zh-Hans once a second locale ships).
    # `Report` has no stored per-row output_lang, so this reads the *current*
    # Settings value rather than whatever the report was actually rendered with —
    # acceptable at Ring 0 (OUTPUT_LANG does not change between generation and send
    # in practice), revisit if that stops holding.
    report_title_key = "Portfonia Financial Analysis Report"
    glossary = load_i18n_glossary()
    locale = locale_for_output_lang(settings.OUTPUT_LANG)
    report_title = (
        glossary.report_glossary[report_title_key][locale]
        if locale in glossary.supported_locales
        else report_title_key
    )
    subject = f"{report_title} — {report_date_str}"
    html_body = _render_html(report.report_md)

    payload: dict[str, object] = {
        "from": settings.EMAIL_FROM,
        "to": [recipient],
        "reply_to": settings.EMAIL_REPLY_TO,
        "subject": subject,
        "html": html_body,
    }

    # Content-addressed idempotency key: a redelivered Celery task or a
    # near-simultaneous manual send for the SAME content reuses this key, so
    # Resend's own dedup suppresses the duplicate. A regenerated report with
    # DIFFERENT content gets a different key, so it can still be delivered —
    # reusing report.id alone made Resend reject the regenerated send with
    # "request body was modified" (409), silently leaving the corrected
    # content unsent while the stale first version sat in the inbox.
    content_hash = hashlib.sha256(html_body.encode("utf-8")).hexdigest()[:16]
    idempotency_key = f"report-{report.id}-{content_hash}"

    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(
                _RESEND_SEND_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "Idempotency-Key": idempotency_key,
                },
                json=payload,
            )
            resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        logger.error(
            "report %s: Resend HTTP %s — %s",
            report.id,
            exc.response.status_code,
            exc.response.text[:400],
        )
        return False
    except Exception:
        logger.exception("report %s: email delivery failed", report.id)
        return False

    resend_id = resp.json().get("id", "unknown")
    sent_at = datetime.now(tz=UTC)

    # Atomic conditional UPDATE: only one concurrent sender can win the
    # WHERE email_sent_at IS NULL predicate. rowcount == 0 means another
    # worker already committed email_sent_at — Resend's Idempotency-Key
    # suppressed the duplicate delivery, so we can safely return True.
    try:
        # session.execute on a DML statement returns CursorResult at runtime;
        # SQLAlchemy stubs type it as Result[Any] and don't narrow for DML.
        result: CursorResult[tuple[()]] = session.execute(  # type: ignore[assignment]
            sa_update(Report)
            .where(Report.id == report.id, Report.email_sent_at.is_(None))
            .values(email_sent_at=sent_at, report_html=html_body)
        )
        session.commit()
    except Exception:
        logger.exception(
            "report %s: email delivered (resend_id=%s) but failed to persist email_sent_at",
            report.id,
            resend_id,
        )
        session.rollback()
        send_ops_alert(
            subject=f"[Portfonia] email sent but state unconfirmed — report {report.id}",
            body=(
                f"Report {report.id} ({report.report_date}) was delivered by Resend "
                f"(resend_id={resend_id}) but the follow-up DB commit failed, so "
                f"email_sent_at remains NULL.\n\n"
                f"The dedup guard will NOT fire on the next retry for this report. "
                f"If the report content is regenerated before the next run, a second "
                f"delivery is possible.\n\n"
                f"Action: verify delivery in the Resend dashboard, then manually set "
                f"email_sent_at on this report row if confirmed."
            ),
        )
        return False

    if result.rowcount == 0:
        logger.info(
            "report %s: concurrent send dedup — email_sent_at already committed by another sender",
            report.id,
        )
        return True

    # Reflect the committed state back onto the in-memory object so the G3
    # check fires correctly for any subsequent call in the same process.
    report.email_sent_at = sent_at
    report.report_html = html_body

    logger.info(
        "report %s: email delivered to %s (subject: %s, resend_id: %s)",
        report.id,
        recipient,
        subject,
        resend_id,
    )
    return True


def send_ops_alert(subject: str, body: str, idempotency_key: str | None = None) -> None:
    """Send a plain-text ops alert to the admin email via Resend.

    Used for failure/needs_review notifications. Never raises — logs on error.

    Pass idempotency_key to suppress duplicate alerts across Celery retries of
    the same task. Resend will accept the first delivery and discard subsequent
    requests with the same key within 24 hours.
    """
    settings = get_settings()
    api_key = settings.RESEND_API_KEY.get_secret_value()
    payload: dict[str, object] = {
        "from": settings.EMAIL_FROM,
        "to": [settings.ADMIN_EMAIL],
        "subject": subject,
        "text": body,
    }
    headers: dict[str, str] = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(
                _RESEND_SEND_URL,
                headers=headers,
                json=payload,
            )
            resp.raise_for_status()
        logger.info("ops alert sent to %s: %s", settings.ADMIN_EMAIL, subject)
    except Exception:
        logger.exception("ops alert delivery failed: %s", subject)
