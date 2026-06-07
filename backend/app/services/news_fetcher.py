"""Fetch and normalise RSS news from configured sources (Ring 0 — Stage E1).

Design decisions:
- httpx fetches feed bytes; feedparser parses them.  Splitting network and
  parse concerns gives us timeout control and proper HTTP error handling that
  feedparser's internal fetch does not provide.
- 24-hour window is the default; callers may override for testing or one-off
  backfill.
- Deduplication is by URL hash (MD5-16) within a single fetch_news() call.
  A URL appearing in multiple feeds is included once (first source wins).
- summary is capped at 500 chars after HTML tag stripping.  Full article text
  is not fetched — summaries feed LLM Pass 1 (Stage F) search intent only.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any

import feedparser
import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Source configuration
# ---------------------------------------------------------------------------

# (display_name, rss_url) — update URLs here when feeds move.
_RSS_SOURCES: list[tuple[str, str]] = [
    ("NYT", "https://rss.nytimes.com/services/xml/rss/nyt/Business.xml"),
    ("FT", "https://www.ft.com/?format=rss"),
    # Reuters direct feed retired ~2020; routed via Google News RSS (DI-validated, ~30 items).
    (
        "Reuters",
        "https://news.google.com/rss/search?q=site:reuters.com+business&hl=en-US&gl=US&ceid=US:en",
    ),
]

_REQUEST_TIMEOUT = 15  # seconds per feed
_SUMMARY_MAX_LEN = 500  # chars after HTML stripping
_USER_AGENT = "Portfonia/0.1 (Ring0 RSS reader; contact portfonia@physicalclue.us)"

_TAG_RE = re.compile(r"<[^>]+>")

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NewsItem:
    """One normalised news article from any RSS/Atom source."""

    url_hash: str  # MD5-16 of url — primary dedup key
    title: str
    url: str
    source: str  # e.g. "NYT"
    published_at: datetime  # UTC, timezone-aware
    summary: str | None  # plain text, capped at _SUMMARY_MAX_LEN chars


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _url_hash(url: str) -> str:
    """Return the first 16 hex chars of MD5(url).  Not security-sensitive."""
    return hashlib.md5(url.encode(), usedforsecurity=False).hexdigest()[:16]


def _strip_html(text: str) -> str:
    """Best-effort HTML tag removal via regex.  No external dependency."""
    return _TAG_RE.sub("", text).strip()


def _parse_entry_dt(entry: Any) -> datetime | None:
    """
    Extract the published datetime from a feedparser entry object.

    feedparser normalises to a UTC time.struct_time in `published_parsed`.
    Falls back to the raw `published` / `updated` string (RFC 2822) if the
    struct is missing.  Returns None if no parseable date is found.
    """
    struct = getattr(entry, "published_parsed", None)
    if isinstance(struct, time.struct_time):
        try:
            return datetime(
                struct.tm_year,
                struct.tm_mon,
                struct.tm_mday,
                struct.tm_hour,
                struct.tm_min,
                struct.tm_sec,
                tzinfo=UTC,
            )
        except ValueError:
            pass

    raw: object = getattr(entry, "published", None) or getattr(entry, "updated", None)
    if isinstance(raw, str):
        try:
            return parsedate_to_datetime(raw).astimezone(UTC)
        except Exception:
            pass

    return None


def _fetch_feed(source: str, url: str, cutoff: datetime) -> list[NewsItem]:
    """
    Fetch one RSS/Atom feed and return items published at or after cutoff.

    Returns [] on any network or parse error (logged at WARNING level so a
    single broken feed does not fail the whole news run).
    """
    try:
        with httpx.Client(timeout=_REQUEST_TIMEOUT) as client:
            resp = client.get(
                url,
                headers={
                    "User-Agent": _USER_AGENT,
                    "Accept": "application/rss+xml, application/atom+xml, text/xml, */*",
                },
                follow_redirects=True,
            )
            resp.raise_for_status()
            raw_bytes = resp.content
    except httpx.HTTPStatusError as exc:
        logger.warning("HTTP %s fetching %s feed: %s", exc.response.status_code, source, url)
        return []
    except httpx.HTTPError:
        logger.warning("Network error fetching %s feed: %s", source, url)
        return []
    except Exception:
        logger.exception("Unexpected error fetching %s feed: %s", source, url)
        return []

    feed: Any = feedparser.parse(raw_bytes)

    # bozo=True means feedparser detected a structural issue.  Some feeds set
    # it for minor issues (encoding declarations etc.) while still yielding
    # valid entries.  Only bail out when bozo AND no entries were parsed.
    if getattr(feed, "bozo", False) and not list(feed.entries):
        logger.warning(
            "feedparser bozo flag for %s (%s): %s",
            source,
            url,
            getattr(feed, "bozo_exception", "unknown"),
        )
        return []

    items: list[NewsItem] = []
    for entry in feed.entries:
        pub = _parse_entry_dt(entry)
        if pub is None or pub < cutoff:
            continue

        link: object = getattr(entry, "link", "") or ""
        if not isinstance(link, str) or not link:
            continue

        title: object = getattr(entry, "title", "") or ""
        title_str = title if isinstance(title, str) else ""

        raw_summary: object = getattr(entry, "summary", None) or getattr(entry, "description", None)
        summary: str | None = None
        if isinstance(raw_summary, str) and raw_summary:
            stripped = _strip_html(raw_summary)
            summary = stripped[:_SUMMARY_MAX_LEN] if stripped else None

        items.append(
            NewsItem(
                url_hash=_url_hash(link),
                title=title_str.strip(),
                url=link,
                source=source,
                published_at=pub,
                summary=summary,
            )
        )

    logger.info(
        "%s: %d items in window (total entries in feed: %d)",
        source,
        len(items),
        len(list(feed.entries)),
    )
    return items


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def fetch_news(window_hours: int = 24) -> list[NewsItem]:
    """
    Fetch news from all configured RSS sources.

    Returns items published within the last `window_hours`, deduplicated by
    URL hash (first source wins when the same URL appears in multiple feeds),
    sorted newest-first.  Feed failures are logged and silently skipped.
    """
    cutoff = datetime.now(tz=UTC) - timedelta(hours=window_hours)
    seen: set[str] = set()
    all_items: list[NewsItem] = []

    for source, url in _RSS_SOURCES:
        for item in _fetch_feed(source, url, cutoff):
            if item.url_hash not in seen:
                seen.add(item.url_hash)
                all_items.append(item)

    all_items.sort(key=lambda x: x.published_at, reverse=True)
    logger.info(
        "fetch_news: %d unique items from %d source(s)",
        len(all_items),
        len(_RSS_SOURCES),
    )
    return all_items
