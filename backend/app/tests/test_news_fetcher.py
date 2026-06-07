"""Unit tests for news_fetcher (E1).

No database required.  All network calls are mocked via httpx.
feedparser is invoked for real against in-memory RSS XML fixtures, which
verifies the full parse + normalisation path without hitting the internet.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.services.news_fetcher import (
    NewsItem,
    _fetch_feed,
    _parse_entry_dt,
    _strip_html,
    _url_hash,
    fetch_news,
)

# ---------------------------------------------------------------------------
# RSS XML fixtures
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 6, 4, 20, 0, 0, tzinfo=UTC)
_OLD = _NOW - timedelta(hours=30)  # outside 24-hour window


def _rss(items: list[tuple[str, str, str]]) -> bytes:
    """
    Build a minimal RSS 2.0 feed.
    `items` = list of (title, link, pubdate_rfc2822).
    """
    entries = "\n".join(
        f"""    <item>
      <title>{title}</title>
      <link>{link}</link>
      <description>Summary of {title}.</description>
      <pubDate>{pub}</pubDate>
    </item>"""
        for title, link, pub in items
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Test Feed</title>
    <link>https://example.com</link>
    {entries}
  </channel>
</rss>""".encode()


def _pubdate(dt: datetime) -> str:
    """Format datetime as RSS pubDate (RFC 2822)."""
    return dt.strftime("%a, %d %b %Y %H:%M:%S +0000")


# ---------------------------------------------------------------------------
# _url_hash
# ---------------------------------------------------------------------------


def test_url_hash_is_deterministic() -> None:
    assert _url_hash("https://example.com/a") == _url_hash("https://example.com/a")


def test_url_hash_differs_for_different_urls() -> None:
    assert _url_hash("https://example.com/a") != _url_hash("https://example.com/b")


def test_url_hash_length() -> None:
    assert len(_url_hash("https://example.com")) == 16


# ---------------------------------------------------------------------------
# _strip_html
# ---------------------------------------------------------------------------


def test_strip_html_removes_tags() -> None:
    assert _strip_html("<p>Hello <b>world</b>.</p>") == "Hello world."


def test_strip_html_plain_text_unchanged() -> None:
    assert _strip_html("No tags here") == "No tags here"


def test_strip_html_empty_string() -> None:
    assert _strip_html("") == ""


# ---------------------------------------------------------------------------
# _parse_entry_dt
# ---------------------------------------------------------------------------


def _struct_from_dt(dt: datetime) -> time.struct_time:
    return time.gmtime(dt.timestamp())


def test_parse_entry_dt_from_published_parsed() -> None:
    entry = SimpleNamespace(published_parsed=_struct_from_dt(_NOW), published=None)
    result = _parse_entry_dt(entry)
    assert result is not None
    assert result.tzinfo is UTC
    assert result.year == _NOW.year
    assert result.month == _NOW.month
    assert result.day == _NOW.day


def test_parse_entry_dt_fallback_to_raw_string() -> None:
    entry = SimpleNamespace(
        published_parsed=None,
        published="Wed, 04 Jun 2026 20:00:00 +0000",
    )
    result = _parse_entry_dt(entry)
    assert result is not None
    assert result.year == 2026
    assert result.month == 6
    assert result.day == 4


def test_parse_entry_dt_returns_none_on_missing_date() -> None:
    entry = SimpleNamespace(published_parsed=None, published=None, updated=None)
    assert _parse_entry_dt(entry) is None


def test_parse_entry_dt_returns_none_on_bad_string() -> None:
    entry = SimpleNamespace(published_parsed=None, published="not a date", updated=None)
    assert _parse_entry_dt(entry) is None


# ---------------------------------------------------------------------------
# _fetch_feed — helpers for httpx mocking
# ---------------------------------------------------------------------------


def _mock_http_response(content: bytes, status_code: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.content = content
    resp.status_code = status_code
    if status_code >= 400:
        import httpx

        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            message=f"HTTP {status_code}",
            request=MagicMock(),
            response=MagicMock(status_code=status_code),
        )
    else:
        resp.raise_for_status = MagicMock()
    return resp


def _patch_httpx(response: MagicMock):  # type: ignore[no-untyped-def]
    """Context manager: patch httpx.Client to return `response` for any .get() call."""
    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.get.return_value = response
    return patch("app.services.news_fetcher.httpx.Client", return_value=mock_client)


# ---------------------------------------------------------------------------
# _fetch_feed — functional tests
# ---------------------------------------------------------------------------


def test_fetch_feed_returns_items_within_window() -> None:
    cutoff = _NOW - timedelta(hours=24)
    feed_bytes = _rss(
        [
            ("Recent Article", "https://example.com/1", _pubdate(_NOW - timedelta(hours=2))),
            ("Old Article", "https://example.com/2", _pubdate(_NOW - timedelta(hours=30))),
        ]
    )
    with _patch_httpx(_mock_http_response(feed_bytes)):
        items = _fetch_feed("TEST", "https://example.com/rss", cutoff)

    assert len(items) == 1
    assert items[0].title == "Recent Article"
    assert items[0].source == "TEST"
    assert items[0].url == "https://example.com/1"
    assert items[0].summary is not None


def test_fetch_feed_returns_empty_on_http_error() -> None:
    cutoff = _NOW - timedelta(hours=24)
    with _patch_httpx(_mock_http_response(b"", status_code=404)):
        items = _fetch_feed("TEST", "https://example.com/rss", cutoff)
    assert items == []


def test_fetch_feed_returns_empty_on_network_error() -> None:
    import httpx

    cutoff = _NOW - timedelta(hours=24)
    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.get.side_effect = httpx.ConnectError("unreachable")

    with patch("app.services.news_fetcher.httpx.Client", return_value=mock_client):
        items = _fetch_feed("TEST", "https://example.com/rss", cutoff)
    assert items == []


def test_fetch_feed_strips_html_from_summary() -> None:
    cutoff = _NOW - timedelta(hours=24)
    # Inject raw HTML into the RSS description via a feedparser-parsed fixture.
    feed_bytes = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<rss version="2.0"><channel><title>T</title>'
        b"<item>"
        b"<title>Art</title>"
        b"<link>https://example.com/x</link>"
        b"<description><![CDATA[<p>Hello <b>world</b>.</p>]]></description>"
        b"<pubDate>" + _pubdate(_NOW - timedelta(hours=1)).encode() + b"</pubDate>"
        b"</item></channel></rss>"
    )
    with _patch_httpx(_mock_http_response(feed_bytes)):
        items = _fetch_feed("TEST", "https://example.com/rss", cutoff)

    assert len(items) == 1
    assert items[0].summary == "Hello world."


def test_fetch_feed_all_items_outside_window_returns_empty() -> None:
    cutoff = _NOW - timedelta(hours=24)
    feed_bytes = _rss(
        [
            ("Old 1", "https://example.com/1", _pubdate(_NOW - timedelta(hours=25))),
            ("Old 2", "https://example.com/2", _pubdate(_NOW - timedelta(hours=48))),
        ]
    )
    with _patch_httpx(_mock_http_response(feed_bytes)):
        items = _fetch_feed("TEST", "https://example.com/rss", cutoff)
    assert items == []


# ---------------------------------------------------------------------------
# fetch_news — integration of multiple sources
# ---------------------------------------------------------------------------


def _make_feed_side_effect(feed_map: dict[str, bytes]):  # type: ignore[no-untyped-def]
    """Return a side_effect callable for _fetch_feed based on source name."""

    def _side_effect(source: str, url: str, cutoff: datetime) -> list[NewsItem]:
        if source not in feed_map:
            return []
        cutoff_inner = cutoff
        feed_bytes = feed_map[source]
        # Use _fetch_feed for real parsing via a patched httpx inside.
        with _patch_httpx(_mock_http_response(feed_bytes)):
            return _fetch_feed(source, url, cutoff_inner)

    return _side_effect


def test_fetch_news_deduplicates_same_url_across_sources() -> None:
    shared_url = "https://example.com/shared"
    pub = _pubdate(datetime.now(tz=UTC) - timedelta(hours=1))
    feed_a = _rss([("Shared", shared_url, pub)])
    feed_b = _rss([("Shared Again", shared_url, pub), ("Unique B", "https://example.com/b", pub)])

    side_effect = _make_feed_side_effect({"A": feed_a, "B": feed_b})

    with (
        patch("app.services.news_fetcher._RSS_SOURCES", [("A", "urlA"), ("B", "urlB")]),
        patch("app.services.news_fetcher._fetch_feed", side_effect=side_effect),
    ):
        items = fetch_news(window_hours=24)

    urls = [i.url for i in items]
    assert urls.count(shared_url) == 1  # deduplicated
    assert "https://example.com/b" in urls


def test_fetch_news_sorted_newest_first() -> None:
    # Use real now so both items stay inside the 24-hour window regardless of
    # when the test runs (_NOW is a hardcoded fixture date, not the real clock).
    real_now = datetime.now(tz=UTC)
    early = _pubdate(real_now - timedelta(hours=5))
    late = _pubdate(real_now - timedelta(hours=1))
    feed = _rss(
        [
            ("Older", "https://example.com/1", early),
            ("Newer", "https://example.com/2", late),
        ]
    )

    def _side_effect(source: str, url: str, cutoff: datetime) -> list[NewsItem]:
        with _patch_httpx(_mock_http_response(feed)):
            return _fetch_feed(source, url, cutoff)

    with (
        patch("app.services.news_fetcher._RSS_SOURCES", [("TEST", "url")]),
        patch("app.services.news_fetcher._fetch_feed", side_effect=_side_effect),
    ):
        items = fetch_news(window_hours=24)

    assert len(items) == 2
    assert items[0].title == "Newer"
    assert items[1].title == "Older"


def test_fetch_news_failed_source_does_not_abort_others() -> None:
    good_feed = _rss([("Good", "https://example.com/g", _pubdate(datetime.now(tz=UTC) - timedelta(hours=1)))])

    def _side_effect(source: str, url: str, cutoff: datetime) -> list[NewsItem]:
        if source == "BAD":
            return []  # simulates network error in _fetch_feed
        with _patch_httpx(_mock_http_response(good_feed)):
            return _fetch_feed(source, url, cutoff)

    with (
        patch("app.services.news_fetcher._RSS_SOURCES", [("BAD", "urlBAD"), ("GOOD", "urlGOOD")]),
        patch("app.services.news_fetcher._fetch_feed", side_effect=_side_effect),
    ):
        items = fetch_news(window_hours=24)

    assert len(items) == 1
    assert items[0].source == "GOOD"


def test_fetch_news_empty_when_all_sources_fail() -> None:
    with (
        patch("app.services.news_fetcher._RSS_SOURCES", [("X", "url")]),
        patch("app.services.news_fetcher._fetch_feed", return_value=[]),
    ):
        items = fetch_news(window_hours=24)
    assert items == []
