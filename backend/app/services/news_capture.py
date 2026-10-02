"""Clean and persist the shared RSS pool; retain leads only in memory."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.intel import InstrumentProfile, IntelCollectionRun
from app.models.news import News
from app.services.headline_cleaning import block_reason, load_cleaning_config
from app.services.instrument_news_sources import CollectedItem
from app.services.instrument_profiles import match_instruments
from app.services.intel_records import link_instrument, store_headline
from app.services.news_fetcher import NewsItem, fetch_news


@dataclass
class PoolCaptureResult:
    inserted: int
    items: list[NewsItem]


def capture_news(
    session: Session,
    window_hours: int = 48,
    *,
    slot_run_id: uuid.UUID | None = None,
    node: str | None = None,
) -> PoolCaptureResult:
    now = datetime.now(UTC)
    run = IntelCollectionRun(
        kind="rss",
        slot_run_id=slot_run_id,
        node=node or "capture-news",
        started_at=now,
        status="running",
        stats={},
        errors=[],
    )
    session.add(run)
    session.flush()
    config = load_cleaning_config()
    fetched = fetch_news(window_hours=window_hours)
    aliases = {p.identifier: p.aliases for p in session.scalars(select(InstrumentProfile))}
    cleaning: dict[str, int] = {}
    samples: dict[str, list[str]] = {}
    kept = []
    inserted = 0
    linked = 0
    for item in fetched.items:
        reason = block_reason(
            CollectedItem(item.title, item.published_at, item.url, item.summary),
            [],
            [],
            config,
            pool=True,
        )
        if reason:
            cleaning[reason] = cleaning.get(reason, 0) + 1
            samples.setdefault(reason, [])
            if len(samples[reason]) < 3:
                samples[reason].append(item.title)
            continue
        existed = session.scalar(select(News.id).where(News.url_hash == item.url_hash))
        nid = store_headline(session, item, "pool", "article", None)
        inserted += int(existed is None)
        for ident in match_instruments(item.title + " " + (item.summary or ""), aliases):
            linked += link_instrument(session, nid, ident)
        kept.append(item)
    errors = [f.error for f in fetched.feeds if f.error]
    run.stats = {
        "rss": {
            "calls": len(fetched.feeds),
            "items": len(fetched.items),
            "inserted": inserted,
            "linked": linked,
            "errors": len(errors),
            "skipped_no_name": 0,
        },
        "feeds": {f.name: {"items": f.items, "errors": f.errors} for f in fetched.feeds},
        "cleaning": cleaning,
        "cleaning_samples": samples,
    }
    run.errors = errors[:50]
    run.finished_at = datetime.now(UTC)
    run.status = "partial" if errors else "ok"
    session.commit()
    return PoolCaptureResult(inserted, kept)
