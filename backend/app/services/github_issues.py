"""Auto-create GitHub issues for operational failures that need developer tracking.

Requires GITHUB_TOKEN (repo scope, issues:write) and GITHUB_REPO in settings.
If either is absent the call is a no-op — production can run without this.

Usage:
    from app.services.github_issues import create_bug_report
    create_bug_report(
        title="capture_prices_task: final retry exhausted for US/close",
        body="...",
        labels=["bug", "ops"],
    )
"""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)

_GITHUB_API = "https://api.github.com"

# GitHub issue bodies 422 above this (documented REST limit). Applied here
# so any caller interpolating a huge exception (issue #195) still files.
_GITHUB_ISSUE_BODY_MAX = 65_536
_TRUNCATION_MARK = "\n...(truncated)"


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    keep = max(0, limit - len(_TRUNCATION_MARK))
    return text[:keep] + _TRUNCATION_MARK


def create_bug_report(
    title: str,
    body: str,
    labels: list[str] | None = None,
) -> str | None:
    """Create a GitHub issue and return its URL, or None if skipped/failed.

    Never raises — failure is logged and swallowed so the caller's main path
    is never interrupted by a GitHub API hiccup.
    """
    from app.core.config import get_settings

    settings = get_settings()
    if settings.GITHUB_TOKEN is None:
        logger.debug("GITHUB_TOKEN not set — skipping bug report: %s", title)
        return None

    token = settings.GITHUB_TOKEN.get_secret_value()
    repo = settings.GITHUB_REPO

    payload: dict[str, object] = {
        "title": title,
        "body": _truncate(body, _GITHUB_ISSUE_BODY_MAX),
    }
    if labels:
        payload["labels"] = labels

    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(
                f"{_GITHUB_API}/repos/{repo}/issues",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                json=payload,
            )
            resp.raise_for_status()
        url: str = resp.json().get("html_url", "")
        logger.info("bug report created: %s", url)
        return url
    except Exception:
        logger.exception("github bug report creation failed: %s", title)
        return None
