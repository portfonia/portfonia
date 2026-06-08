"""Tests for the news capture service (ADR-002 step 2)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.news import News
from app.services.news_capture import capture_news
from app.services.news_fetcher import NewsItem, _url_hash


def _item(title: str) -> NewsItem:
    url = f"https://example.com/{title.replace(' ', '-').lower()}"
    return NewsItem(
        url_hash=_url_hash(url),
        title=title,
        url=url,
        source="TEST",
        published_at=datetime(2026, 6, 6, 12, 0, tzinfo=UTC),
        summary=f"Summary of {title}",
    )


def test_capture_news_inserts_and_dedups(db_session: Session) -> None:
    items = [_item("Fed holds"), _item("Chips rally")]
    with patch("app.services.news_capture.fetch_news", return_value=items):
        first = capture_news(db_session)
    assert first == 2

    # Re-running with overlapping items inserts only the new one.
    with patch(
        "app.services.news_capture.fetch_news",
        return_value=[_item("Fed holds"), _item("New story")],
    ):
        second = capture_news(db_session)
    assert second == 1

    total = db_session.execute(select(func.count()).select_from(News)).scalar_one()
    assert total == 3


def test_capture_news_empty(db_session: Session) -> None:
    with patch("app.services.news_capture.fetch_news", return_value=[]):
        assert capture_news(db_session) == 0
