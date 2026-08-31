"""Email delivery service (Stage G).

Sends generated reports via Resend REST API (httpx direct, no SDK).
On success, writes email_sent_at to the reports row.
On failure, logs and returns False — report persistence must not be rolled back
by a delivery failure.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime, timedelta

import httpx
from bs4 import BeautifulSoup, Tag
from markdown_it import MarkdownIt
from sqlalchemy import update as sa_update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.report import Report
from app.services.i18n_glossary import load_i18n_glossary, locale_for_output_lang
from app.services.unsubscribe_token import create_token as create_unsubscribe_token
from app.services.user_directory import recipient_email_with_purpose

logger = logging.getLogger(__name__)

_RESEND_SEND_URL = "https://api.resend.com/emails"
# Resend Idempotency-Key TTL is 24 hours. The unsubscribe token is embedded
# in html_body, which is hashed into that key, so `now` used to mint the
# token must be constant inside this window (PR #279 review).
_RESEND_IDEMPOTENCY_WINDOW = timedelta(hours=24)


def _now_utc() -> datetime:
    return datetime.now(tz=UTC)


def _stable_unsubscribe_now(now: datetime) -> datetime:
    """End of the current 24h UTC bucket.

    `create_token` adds TOKEN_TTL (7 days) to this instant, so remaining
    life is always in [7d, 8d) from the real send time, and the token
    (hence html_body / Idempotency-Key) is identical for any two sends
    in the same bucket.
    """
    window = int(_RESEND_IDEMPOTENCY_WINDOW.total_seconds())
    epoch = int(now.timestamp())
    bucket_start = epoch - (epoch % window)
    return datetime.fromtimestamp(bucket_start + window, tz=UTC)


# Render report Markdown to HTML.
# - html=False escapes any raw HTML in the LLM output (defense against
#   <script>/<img onerror=...> injection if the report is ever viewed in a
#   browser or admin UI; email clients usually strip it but we do not rely on that).
# - enable("table") restores GFM tables, which the commonmark baseline disables.
_md = MarkdownIt("commonmark", {"html": False}).enable("table")

# Single source of truth for per-tag CSS, keyed by tag name — used BOTH to
# generate the <head><style> block below AND to stamp inline `style="..."`
# attributes via _inline_body_styles. Previously these were two hand-copied
# CSS strings that had already drifted (PR #117 Grok review) — generating the
# <style> block from this dict makes drift impossible.
_TAG_STYLES: dict[str, str] = {
    "h1": "font-size:1.45em;color:#111;border-bottom:2px solid #e8e8e8;padding-bottom:8px;margin:0 0 0.5em;",
    "h2": "font-size:1.15em;color:#222;margin-top:2em;border-bottom:1px solid #ebebeb;padding-bottom:4px;",
    "h3": "font-size:1.0em;color:#333;margin-top:1.4em;",
    "table": "border-collapse:collapse;width:100%;margin:1em 0;font-size:0.88em;",
    "th": "border:1px solid #d8d8d8;padding:6px 10px;text-align:left;vertical-align:top;background:#f5f5f5;font-weight:600;",
    "td": "border:1px solid #d8d8d8;padding:6px 10px;text-align:left;vertical-align:top;",
    "blockquote": "border-left:3px solid #ccc;margin:1em 0;padding:0.4em 1em;color:#555;",
    "code": "background:#f3f3f3;padding:1px 4px;border-radius:3px;font-size:0.88em;",
    "pre": "background:#f3f3f3;padding:12px;border-radius:4px;overflow-x:auto;",
    "p": "margin:0.7em 0;",
    "ul": "padding-left:1.5em;margin:0.7em 0;",
    "ol": "padding-left:1.5em;margin:0.7em 0;",
    "hr": "border:none;border-top:1px solid #e0e0e0;margin:2em 0;",
}

_BODY_STYLE = (
    "margin:0;padding:0;background:#ffffff;font-family:-apple-system,BlinkMacSystemFont,"
    "'Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif;font-size:15px;line-height:1.65;"
    "color:#1a1a1a;"
)

# Even-row zebra fill, painted on td/th cells (not the <tr>) — Word-based
# Outlook often ignores `background` set directly on a table row (PR #117
# Grok review finding). background-color (longhand) plus a bgcolor attribute
# cover older Word builds too. tr:nth-child(even) in the <style> block below
# is kept only as a harmless enhancement for clients that do honor it.
_ZEBRA_CELL_STYLE = "background-color:#fafafa;"
_ZEBRA_CELL_BGCOLOR = "#fafafa"

# The bulletproof wrapper's inner content cell padding. Load-bearing because
# _HTML_TEMPLATE interpolates it directly and tests assert it appears in the
# rendered output — not just a name mentioned in a comment.
_WRAPPER_TD_STYLE = "padding:32px 24px;"


def _build_head_style_rules() -> str:
    """Generate the <head><style> block's per-tag rules from _TAG_STYLES,
    so it cannot silently diverge from the inline styles _inline_body_styles
    applies (PR #117 Grok review — the two were previously hand-duplicated
    and had already drifted)."""
    return "\n".join(f"  {tag} {{ {style} }}" for tag, style in _TAG_STYLES.items())


# Outlook's Word rendering engine does not reliably apply <head><style> rules
# (issue #24) — this block is kept only as an enhancement for clients that DO
# support it (Gmail web/app, Apple Mail, ...); its per-tag rules are generated
# from _TAG_STYLES (see _build_head_style_rules) so it can't drift from the
# inline copy applied by _inline_body_styles, which is what's load-bearing.
_HTML_TEMPLATE = f"""\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<style>
  body {{ {_BODY_STYLE} }}
{_build_head_style_rules()}
  tr:nth-child(even) {{ background: #fafafa; }}
</style>
</head>
<body style="{_BODY_STYLE}">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="width:100%;background:#ffffff;">
<tr>
<td align="center" style="padding:0;">
<table role="presentation" width="720" cellpadding="0" cellspacing="0" border="0" style="width:720px;max-width:720px;">
<tr>
<td style="{_WRAPPER_TD_STYLE}">
__REPORT_BODY__
</td>
</tr>
</table>
</td>
</tr>
</table>
</body>
</html>
"""


def _stripe_rows(rows: list[Tag]) -> None:
    """Apply even-row zebra fill to each row's td/th cells (not the row
    itself — see _ZEBRA_CELL_STYLE). Appended AFTER the cell's existing
    style, not prepended — CSS resolves same-attribute conflicts by
    last-declaration-wins, so appending is what guarantees the zebra fill
    can't be silently overridden if _TAG_STYLES["td"] ever gains its own
    background (Grok PR #117 round-2 review)."""
    for i, row in enumerate(rows):
        if i % 2 == 1:
            for cell in row.find_all(["td", "th"], recursive=False):
                existing = str(cell.get("style", ""))
                if existing and not existing.endswith(";"):
                    existing += ";"
                cell["style"] = f"{existing}{_ZEBRA_CELL_STYLE}"
                cell["bgcolor"] = _ZEBRA_CELL_BGCOLOR


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
        row_groups = table.find_all(["thead", "tbody"], recursive=False)
        if row_groups:
            for row_group in row_groups:
                _stripe_rows(row_group.find_all("tr", recursive=False))
        else:
            # No thead/tbody wrapper (e.g. hand-built HTML) — stripe the
            # table's direct <tr> children instead of silently no-op'ing
            # (PR #117 Grok review).
            _stripe_rows(table.find_all("tr", recursive=False))

    return str(soup)


def _render_html(markdown: str) -> str:
    body = _inline_body_styles(_md.render(markdown))
    return _HTML_TEMPLATE.replace("__REPORT_BODY__", body)


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
    resolved = recipient_email_with_purpose(session, report.user_id)
    if resolved is None:
        # Fail closed, never fall back to ADMIN_EMAIL or any other default:
        # a report we can't resolve a recipient for still belongs to a real
        # user, and sending it anywhere else — even to an address we trust —
        # is still a leak, and one that would read as "delivered" in the
        # logs, permanently masking the identity-resolution bug (Ring 1-B
        # design doc §5.3).
        logger.error(
            "report %s: could not resolve a recipient for user_id=%s — refusing to send",
            report.id,
            report.user_id,
        )
        send_ops_alert(
            subject="Portfonia: report recipient could not be resolved",
            body=(
                f"report_id={report.id} user_id={report.user_id} — "
                "recipient_email_with_purpose() returned None. Report was NOT sent. "
                "email_sent_at left null; can be resent manually once the "
                "user's identity resolves."
            ),
        )
        return False

    recipient, purpose = resolved
    api_key = settings.RESEND_API_KEY.get_secret_value()

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
    unsub_token = create_unsubscribe_token(
        user_id=report.user_id,
        purpose=purpose,
        email=recipient,
        now=_stable_unsubscribe_now(_now_utc()),
    )
    unsub_url = f"{settings.FRONTEND_URL}/unsubscribe?token={unsub_token}"
    footer_copy = _UNSUBSCRIBE_FOOTER_COPY.get(
        settings.OUTPUT_LANG, _UNSUBSCRIBE_FOOTER_COPY[_DEFAULT_UNSUBSCRIBE_FOOTER_LOCALE]
    )
    html_body = _render_html(
        report.report_md + "\n\n" + footer_copy["html_md"].format(url=unsub_url)
    )
    text_body = report.report_md + "\n\n" + footer_copy["text"].format(url=unsub_url)

    payload: dict[str, object] = {
        "from": settings.EMAIL_FROM,
        "to": [recipient],
        "reply_to": settings.EMAIL_REPLY_TO,
        "subject": subject,
        "html": html_body,
        "text": text_body,
        "headers": {
            "List-Unsubscribe": f"<{unsub_url}>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        },
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

    # None (not "unknown") when Resend's response is missing an id — issue #45
    # persists this to provider_message_id, and a placeholder string would be
    # indistinguishable from a real id when cross-referencing Resend's dashboard.
    resend_id: str | None = resp.json().get("id")
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
            .values(
                email_sent_at=sent_at,
                report_html=html_body,
                provider_message_id=resend_id,
            )
        )
        session.commit()
    except Exception:
        logger.exception(
            "report %s: email delivered (resend_id=%s) but failed to persist email_sent_at",
            report.id,
            resend_id or "unknown",
        )
        session.rollback()
        send_ops_alert(
            subject=f"[Portfonia] email sent but state unconfirmed — report {report.id}",
            body=(
                f"Report {report.id} ({report.report_date}) was delivered by Resend "
                f"(resend_id={resend_id or 'unknown'}) but the follow-up DB commit failed, so "
                f"email_sent_at remains NULL.\n\n"
                f"The dedup guard will NOT fire on the next retry for this report. "
                f"If the report content is regenerated before the next run, a second "
                f"delivery is possible.\n\n"
                f"Action: verify delivery in the Resend dashboard, then manually set "
                f"email_sent_at and provider_message_id (to {resend_id or 'unknown'}) "
                f"on this report row if confirmed."
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
    report.provider_message_id = resend_id

    logger.info(
        "report %s: email delivered to %s (subject: %s, resend_id: %s)",
        report.id,
        recipient,
        subject,
        resend_id or "unknown",
    )
    return True


# Locale-keyed footer for the report-email unsubscribe link (issue #257).
# Same bare `en`/`zh` keys as `_VERIFICATION_EMAIL_COPY` below — OUTPUT_LANG,
# not the frontend BCP-47 tags. The HTML variant is markdown so `_render_html`
# turns it into a real <a href> in the footer; the text variant is the same
# URL as a plain line. Disclaimer/glossary copy already lives in
# `report.report_md` (assembled by report_sections._build_footer) and is
# therefore present in the text alternative without a second injection.
_UNSUBSCRIBE_FOOTER_COPY: dict[str, dict[str, str]] = {
    "en": {
        "html_md": "[Revoke verification for this address]({url})",
        "text": "To revoke verification for this address, visit:\n{url}",
    },
    "zh": {
        "html_md": "[撤销此邮箱的验证]({url})",
        "text": "如需撤销此邮箱的验证,请访问:\n{url}",
    },
}
_DEFAULT_UNSUBSCRIBE_FOOTER_LOCALE = "en"


# Locale-keyed copy for the verification email (issue #260, review round 2)
# — the original version hardcoded English, violating this repo's mandatory
# "in-product strings are i18n-keyed" policy). Deliberately NOT the frontend
# next-intl catalog (browser-only, unreachable from this backend module) and
# NOT i18n_glossary.yml (built for large LLM-generated report bodies, not a
# two-line transactional email). Keys are bare locale codes matching
# `users.locale`/`OUTPUT_LANG`'s existing convention (`en`/`zh`), not the
# frontend UI catalog's BCP-47 `zh-Hans` tag. zh-Hant isn't covered — it
# isn't exposed to users at the UI layer yet either (frontend/src/locales/
# README.md's "zh-Hant review status"), so there's no reason to get ahead
# of that here.
_VERIFICATION_EMAIL_COPY: dict[str, dict[str, str]] = {
    "en": {
        "subject": "Verify your email — Portfonia",
        "body": (
            "Click the link below to verify this email address for Portfonia:\n\n"
            "{url}\n\n"
            "If you didn't request this, you can ignore this email."
        ),
    },
    "zh": {
        "subject": "验证你的邮箱 — Portfonia",
        "body": (
            "点击下面的链接,验证这个邮箱地址是否可以用于 Portfonia:\n\n"
            "{url}\n\n"
            "如果这不是你本人的操作,可以忽略这封邮件。"
        ),
    },
}
_DEFAULT_VERIFICATION_EMAIL_LOCALE = "en"


def send_verification_email(email: str, token: str, *, locale: str = "en") -> str | None:
    """Send an email-verification link (issue #260). Returns Resend's
    delivery id on success (stored as EmailVerification.provider_message_id,
    the poll target for design doc §3.3 step 6), or None on failure — never
    raises; the caller (create_verification) does not touch the DB until
    this returns a real id (review, PR #261 round 2 — see that function's
    docstring for why the ordering changed from an earlier version).
    """
    settings = get_settings()
    verify_url = f"{settings.FRONTEND_URL}/verify-email?token={token}"
    copy = _VERIFICATION_EMAIL_COPY.get(
        locale, _VERIFICATION_EMAIL_COPY[_DEFAULT_VERIFICATION_EMAIL_LOCALE]
    )
    payload: dict[str, object] = {
        "from": settings.EMAIL_FROM,
        "to": [email],
        "subject": copy["subject"],
        "text": copy["body"].format(url=verify_url),
    }
    headers: dict[str, str] = {
        "Authorization": f"Bearer {settings.RESEND_API_KEY.get_secret_value()}",
        "Content-Type": "application/json",
    }
    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(_RESEND_SEND_URL, headers=headers, json=payload)
            resp.raise_for_status()
        resend_id = resp.json().get("id")
        if not isinstance(resend_id, str):
            # 2xx from Resend but no usable id — the email is almost
            # certainly in flight (Resend accepted the request), yet the
            # caller (create_verification) will treat this the same as a
            # genuine send failure: 502, "no local data was touched, retry".
            # A naive retry then sends a SECOND email. Log loudly so an
            # investigating operator sees the same warning the sibling
            # commit-failure path gives (round-4 review finding) — no
            # status-code change, this is diagnostic only.
            logger.error(
                "verification email to %s: Resend returned 2xx with no usable id (%r) — "
                "email likely sent, but the link cannot be tracked or a retry may double-send",
                email,
                resend_id,
            )
            return None
        logger.info("verification email sent to %s (resend_id=%s)", email, resend_id)
        return resend_id
    except Exception:
        logger.exception("verification email delivery failed for %s", email)
        return None


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
