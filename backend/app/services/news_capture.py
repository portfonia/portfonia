"""Persist fetched news into the long-term `news` table (ADR-002 capture layer).

Credit-free: RSS only, no LLM. Idempotent via ON CONFLICT (url_hash) DO NOTHING,
so overlapping windows and catch-up re-runs never duplicate. RSS feeds only carry
~1-2 days, so a longer window is a best-effort catch-up, not a guarantee.
"""

from __future__ import annotations

import logging

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models.news import News
from app.services.news_fetcher import fetch_news

logger = logging.getLogger(__name__)


def capture_news(session: Session, window_hours: int = 48) -> int:
    """Fetch recent news and upsert into `news`. Returns rows newly inserted.

    A 48h default gives a small catch-up cushion over the per-run interval;
    duplicates are dropped by the url_hash conflict, so the window can overlap
    freely.
    """
    items = fetch_news(window_hours=window_hours)
    if not items:
        logger.info("capture_news: no items fetched")
        return 0

    rows = [
        {
            "url_hash": it.url_hash,
            "title": it.title,
            "source": it.source,
            "url": it.url,
            "summary": it.summary,
            "published_at": it.published_at,
        }
        for it in items
    ]
    # RETURNING yields only the rows actually inserted (conflicts are skipped),
    # which is reliable where cursor.rowcount is not for a multi-row upsert.
    stmt = (
        pg_insert(News)
        .values(rows)
        .on_conflict_do_nothing(constraint="uq_news_url_hash")
        .returning(News.id)
    )
    inserted = len(session.execute(stmt).fetchall())
    session.commit()
    logger.info("capture_news: fetched %d, inserted %d new", len(items), inserted)
    return inserted
