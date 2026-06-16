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
from markdown_it import MarkdownIt
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.report import Report

logger = logging.getLogger(__name__)

_RESEND_SEND_URL = "https://api.resend.com/emails"

# Minimal inline-CSS email shell.  Outer braces are doubled to survive .format().
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
  .wrapper {{ max-width: 720px; margin: 0 auto; padding: 32px 24px; }}
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
<body>
<div class="wrapper">
{body}
</div>
</body>
</html>
"""

# Render report Markdown to HTML.
# - html=False escapes any raw HTML in the LLM output (defense against
#   <script>/<img onerror=...> injection if the report is ever viewed in a
#   browser or admin UI; email clients usually strip it but we do not rely on that).
# - enable("table") restores GFM tables, which the commonmark baseline disables.
_md = MarkdownIt("commonmark", {"html": False}).enable("table")


def _render_html(markdown: str) -> str:
    body = _md.render(markdown)
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
    subject = f"Portfonia 财经分析报告 — {report_date_str}"
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

    report.email_sent_at = datetime.now(tz=UTC)
    try:
        session.commit()
    except Exception:
        logger.exception("report %s: failed to persist email_sent_at", report.id)
        session.rollback()

    logger.info(
        "report %s: email delivered to %s (subject: %s)",
        report.id,
        recipient,
        subject,
    )
    return True


def send_ops_alert(subject: str, body: str) -> None:
    """Send a plain-text ops alert to the admin email via Resend.

    Used for failure/needs_review notifications. Never raises — logs on error.
    """
    settings = get_settings()
    api_key = settings.RESEND_API_KEY.get_secret_value()
    payload: dict[str, object] = {
        "from": settings.EMAIL_FROM,
        "to": [settings.ADMIN_EMAIL],
        "subject": subject,
        "text": body,
    }
    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(
                _RESEND_SEND_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            resp.raise_for_status()
        logger.info("ops alert sent to %s: %s", settings.ADMIN_EMAIL, subject)
    except Exception:
        logger.exception("ops alert delivery failed: %s", subject)
