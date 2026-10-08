"""Clean and persist the shared RSS pool; retain leads only in memory."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.intel import InstrumentProfile, IntelCollectionRun
from app.models.news import News
from app.services.headline_cleaning import (
    STAGE_STATS,
    block_reason,
    classify_macro,
    load_cleaning_config,
    macro_label,
)
from app.services.instrument_news_sources import CollectedItem
from app.services.instrument_profiles import match_instruments
from app.services.intel_records import link_instrument, store_headline
from app.services.macro_detector import detect_macro_signals
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
    news_ids: dict[str, uuid.UUID] = {}
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
        nid, is_inserted = store_headline(session, item, "pool", "article", None)
        news_ids[item.url_hash] = nid
        inserted += int(is_inserted)
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
    if slot_run_id is not None:
        stats: dict[str, int | float] = {
            "candidates": 0,
            "labeled": 0,
            "calls": 0,
            "failed_calls": 0,
            "cost_usd": 0.0,
            "development": 0,
            "commentary": 0,
            "off_topic": 0,
        }
        stage_stats: dict[str, float] = dict.fromkeys(STAGE_STATS, 0.0)
        seen: set[str] = set()
        for hit in detect_macro_signals(kept, max_articles_per_theme=len(kept) or 1).hits:
            candidates = []
            for item in hit.articles:
                if item.url_hash in seen:
                    continue
                seen.add(item.url_hash)
                row = session.get(News, news_ids[item.url_hash])
                assert row is not None
                if macro_label(row.record) is None:
                    candidates.append((item, row))
            stats["candidates"] += len(candidates)
            for start in range(0, len(candidates), 30):
                batch = candidates[start : start + 30]
                label_call = uuid.uuid4().hex[:12]
                labels, cost, error = classify_macro(
                    [
                        CollectedItem(item.title, item.published_at, item.url, item.summary)
                        for item, _ in batch
                    ],
                    stats=stage_stats,
                )
                stats["calls"] += 1
                stats["cost_usd"] += cost
                if error:
                    errors.append(error)
                if not labels:
                    stats["failed_calls"] += 1
                    continue
                for index, (_, row) in enumerate(batch):
                    label = macro_label(dict(labels.get(index, {})))
                    if label is not None:
                        row.record = {**row.record, **label, "label_call": label_call}
                        stats["labeled"] += 1
                        stats[label["type"]] += 1
        run.stats = {**run.stats, "macro_classification": {**stats, **stage_stats}}
    run.errors = errors[:50]
    run.finished_at = datetime.now(UTC)
    run.status = "partial" if errors else "ok"
    session.commit()
    return PoolCaptureResult(inserted, kept)
