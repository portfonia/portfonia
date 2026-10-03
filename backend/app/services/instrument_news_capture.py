"""Bounded parallel instrument collection, with per-instrument classification."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.models.intel import InstrumentProfile, IntelCollectionRun, IntelSlotRun, NewsInstrument
from app.models.news import News
from app.services.headline_cleaning import (
    CleaningConfig,
    block_reason,
    classify_headlines,
    load_cleaning_config,
)
from app.services.instrument_news_sources import (
    CollectedItem,
    fetch_eastmoney_ann,
    fetch_finnhub,
    fetch_google_news,
    fetch_sec_8k,
    fetch_yahoo,
    reset_run_cache,
)
from app.services.instrument_profiles import resolve_profiles
from app.services.instrument_universe import UniverseEntry, intel_universe
from app.services.intel_records import headline_from_row, link_instrument, store_headline

logger = logging.getLogger(__name__)
MARKET_RANK = {
    "pre_open": ["Japan", "Korea", "HK", "A-Share", "US", "UK", "Europe"],
    "post_close": ["US", "UK", "Europe", "HK", "A-Share", "Japan", "Korea"],
}


@dataclass
class InstrumentResult:
    leads: list[CollectedItem] = field(default_factory=list)
    stats: dict[str, dict[str, int]] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    cleaning: dict[str, int] = field(default_factory=dict)
    samples: dict[str, list[str]] = field(default_factory=dict)
    classifier: dict[str, float] = field(
        default_factory=lambda: {"batches": 0, "failed_batches": 0, "items": 0, "cost_usd": 0}
    )


def source_stat() -> dict[str, int]:
    return dict.fromkeys(["calls", "items", "inserted", "linked", "errors", "skipped_no_name"], 0)


def error_text(source: str, exc: Exception) -> str:
    return f"{source}: {type(exc).__name__}" + (
        f" HTTP {exc.response.status_code}" if isinstance(exc, httpx.HTTPStatusError) else ""
    )


def sources_for(
    entry: UniverseEntry, profile: InstrumentProfile | None, since: datetime, until: datetime
) -> list[tuple[str, Callable[[], list[CollectedItem]]]]:
    source = []
    en = profile.name_en if profile else None
    zh = profile.name_zh if profile else None
    if entry.market == "US":
        source.append(("finnhub", lambda: fetch_finnhub(entry.ticker, since, until)))
    if en and entry.market != "A-Share":
        source.append(("google_news", lambda: fetch_google_news(en, "en-US", since, until)))
    if zh and entry.market in ("HK", "A-Share"):
        source.append(
            (
                "google_news",
                lambda: fetch_google_news(
                    zh, "zh-HK" if entry.market == "HK" else "zh-CN", since, until
                ),
            )
        )
    if entry.market != "A-Share" and (entry.market == "US" or en):
        source.append(
            (
                "yahoo",
                lambda: fetch_yahoo(
                    entry.ticker if entry.market == "US" else en or "", entry.ticker, since, until
                ),
            )
        )
    if entry.market == "US":
        source.append(("sec", lambda: fetch_sec_8k(entry.ticker, since, until)))
    if entry.market in ("HK", "A-Share"):
        code = entry.ticker.split(".")[0].zfill(5 if entry.market == "HK" else 6)
        source.append(
            (
                "eastmoney",
                lambda: fetch_eastmoney_ann(
                    code, "H" if entry.market == "HK" else "A", since, until
                ),
            )
        )
    return source


def collect_instrument_news(
    session: Session, entry: UniverseEntry, now: datetime, config: CleaningConfig
) -> InstrumentResult:
    result = InstrumentResult()
    p = session.get(InstrumentProfile, entry.identifier)
    aliases = p.aliases if p else [entry.ticker.split(".")[0]]
    stored = list(
        session.execute(
            select(News, NewsInstrument.created_at)
            .join(NewsInstrument, NewsInstrument.news_id == News.id)
            .where(
                NewsInstrument.identifier == entry.identifier,
                News.published_at >= now - timedelta(hours=config.hours),
                News.published_at <= now,
            )
        )
    )
    earlier = [headline_from_row(row).title for row, linked_at in stored if linked_at < now]
    previous = [headline_from_row(row).title for row, linked_at in stored if linked_at >= now]
    stored_recent = [
        headline_from_row(row).title
        for row, _ in sorted(stored, key=lambda pair: pair[0].published_at, reverse=True)[:100]
    ]
    fetched: list[tuple[str, CollectedItem]] = []
    candidates: list[tuple[str, CollectedItem]] = []
    for name, fetch in sources_for(entry, p, now - timedelta(hours=48), now):
        stat = result.stats.setdefault(name, source_stat())
        stat["calls"] += 1
        try:
            items = fetch()
        except Exception as exc:
            stat["errors"] += 1
            result.errors.append(error_text(name, exc))
            continue
        stat["items"] += len(items)
        for item in items:
            if item.kind != "filing" and not now - timedelta(hours=48) <= item.published_at <= now:
                result.cleaning["out_of_window"] = result.cleaning.get("out_of_window", 0) + 1
                continue
            fetched.append((name, item))
    for name, item in sorted(fetched, key=lambda pair: pair[1].published_at):
        reason = block_reason(item, aliases, previous, config, earlier=earlier)
        if reason:
            result.cleaning[reason] = result.cleaning.get(reason, 0) + 1
            sample = result.samples.setdefault(reason, [])
            if len(sample) < 3:
                sample.append(item.title)
            continue
        previous.append(item.title)
        candidates.append((name, item))
    if not p or not p.name_en:
        if entry.market != "A-Share":
            result.stats.setdefault("google_news", source_stat())["skipped_no_name"] += 1
        if entry.market not in ("US", "A-Share"):
            result.stats.setdefault("yahoo", source_stat())["skipped_no_name"] += 1
    if entry.market == "A-Share" and (not p or not p.name_zh):
        result.stats.setdefault("google_news", source_stat())["skipped_no_name"] += 1
    labels: dict[int, str] = {}
    articles = [(i, item) for i, (_, item) in enumerate(candidates) if item.kind != "filing"]
    size = min(100, max(1, get_settings().INTEL_CLASSIFIER_BATCH))
    kept_titles: list[str] = []
    for start in range(0, len(articles), size):
        chunk = articles[start : start + size]
        batch, cost, failed = classify_headlines(
            [item for _, item in chunk],
            entry.ticker,
            aliases,
            recent_titles=(list(reversed(kept_titles)) + stored_recent)[:100],
        )
        result.classifier["batches"] += 1
        result.classifier["items"] += len(chunk)
        result.classifier["cost_usd"] += cost
        result.classifier["failed_batches"] += int(failed is not None)
        if failed:
            result.errors.append(failed)
        for i, batch_label in batch.items():
            labels[chunk[i][0]] = batch_label
        kept_titles.extend(
            item.title
            for i, (_, item) in enumerate(chunk)
            if batch.get(i) not in ("promo", "unrelated", "duplicate")
        )
    for i, (name, item) in sorted(
        enumerate(candidates), key=lambda candidate: candidate[1][1].headline().url_hash
    ):
        label = labels.get(i)
        if label in ("promo", "unrelated", "duplicate"):
            llm_reason = label + "_llm"
            result.cleaning[llm_reason] = result.cleaning.get(llm_reason, 0) + 1
            sample = result.samples.setdefault(llm_reason, [])
            if len(sample) < 3:
                sample.append(item.title)
            continue
        if item.kind == "filing":
            result.cleaning["filings_stored"] = result.cleaning.get("filings_stored", 0) + 1
        elif label is None:
            result.cleaning["stored_null_label"] = result.cleaning.get("stored_null_label", 0) + 1
        result.cleaning["kept"] = result.cleaning.get("kept", 0) + 1
        nid, inserted = store_headline(
            session, item.headline(), "instrument", item.kind, label, filing_form=item.filing_form
        )
        result.stats[name]["inserted"] += int(inserted)
        result.stats[name]["linked"] += link_instrument(session, nid, entry.identifier)
        item.news_id = nid
        item.identifier = entry.identifier
        item.label = label
        result.leads.append(item)
    if p is None:
        p = InstrumentProfile(
            identifier=entry.identifier, market=entry.market, aliases=[entry.ticker]
        )
        session.add(p)
    p.news_collected_at = now
    p.updated_at = now
    session.flush()
    return result


def create_instrument_run(
    session: Session, slot_run: IntelSlotRun, now: datetime
) -> IntelCollectionRun:
    run = IntelCollectionRun(
        kind="instrument",
        slot_run_id=slot_run.id,
        started_at=now,
        status="running",
        instruments_total=0,
        instruments_processed=0,
        stats={},
        errors=[],
    )
    session.add(run)
    session.flush()
    return run


def collect_slot_news(
    session: Session,
    slot_run: IntelSlotRun,
    now: datetime,
    time_budget_s: int,
    on_instrument_done: Callable[[str, list[CollectedItem]], None],
    *,
    collection_run: IntelCollectionRun | None = None,
    profile_errors: list[str] | None = None,
    priority: list[str] | None = None,
) -> IntelCollectionRun:
    start = time.monotonic()
    run = (
        collection_run
        if collection_run is not None
        else create_instrument_run(session, slot_run, now)
    )
    entries = intel_universe(session)
    run.instruments_total = len(entries)
    errors = (
        resolve_profiles(session, entries, now=now)
        if profile_errors is None
        else list(profile_errors)
    )
    markets: dict[str, dict[str, int]] = {}
    for e in entries:
        markets.setdefault(e.market, {"total": 0, "processed": 0})["total"] += 1
    profiles = {p.identifier: p for p in session.scalars(select(InstrumentProfile))}
    ranks = MARKET_RANK[slot_run.slot]

    def order(entry: UniverseEntry) -> tuple[int, float, str]:
        profile = profiles.get(entry.identifier)
        collected = profile.news_collected_at if profile else None
        return (
            ranks.index(entry.market) if entry.market in ranks else len(ranks),
            collected.timestamp() if collected else float("-inf"),
            entry.identifier,
        )

    priority_ranks = {identifier: index for index, identifier in enumerate(priority or [])}
    entries.sort(
        key=lambda entry: (priority_ranks.get(entry.identifier, len(priority_ranks)), *order(entry))
    )
    stats: dict[str, object] = {"markets": markets}
    source_stats: dict[str, dict[str, int]] = {}
    cleaning: dict[str, int] = {}
    samples: dict[str, list[str]] = {}
    classifier: dict[str, float] = {}
    try:
        config = load_cleaning_config()
    except ValueError:
        run.status = "failed"
        run.errors = ["cleaning: ValueError"]
        run.stats = stats
        run.finished_at = datetime.now(UTC)
        session.commit()
        return run
    session.commit()
    reset_run_cache()
    processed = 0
    next_index = 0
    failed = False

    def job(entry: UniverseEntry) -> InstrumentResult:
        with SessionLocal() as worker:
            result = collect_instrument_news(worker, entry, now, config)
            worker.commit()
            on_instrument_done(entry.identifier, result.leads)
            return result

    pending: dict[Future[InstrumentResult], UniverseEntry] = {}
    with ThreadPoolExecutor(max_workers=get_settings().INTEL_COLLECT_WORKERS) as pool:
        while next_index < len(entries) or pending:
            while next_index < len(entries) and len(pending) < get_settings().INTEL_COLLECT_WORKERS:
                if time.monotonic() - start >= time_budget_s:
                    break
                entry = entries[next_index]
                pending[pool.submit(job, entry)] = entry
                next_index += 1
            if not pending:
                break
            done, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in done:
                entry = pending.pop(future)
                try:
                    result = future.result()
                except Exception as exc:
                    errors.append(error_text("collection", exc))
                    failed = True
                    continue
                processed += 1
                markets[entry.market]["processed"] += 1
                errors.extend(result.errors)
                for name, values in result.stats.items():
                    target = source_stats.setdefault(name, source_stat())
                    for key, value in values.items():
                        target[key] += value
                for key, value in result.cleaning.items():
                    cleaning[key] = cleaning.get(key, 0) + value
                for key, cost_value in result.classifier.items():
                    classifier[key] = classifier.get(key, 0) + cost_value
                for key, titles in result.samples.items():
                    samples[key] = (samples.get(key, []) + titles)[:3]
    if next_index < len(entries):
        unreached: dict[str, list[str]] = {}
        for e in entries[next_index:]:
            unreached.setdefault(e.market, []).append(e.identifier)
        stats["unreached"] = unreached
        logger.warning(
            "intel collection budget exhausted: slot=%s run_date=%s unreached=%d %s",
            slot_run.slot,
            slot_run.run_date,
            len(entries) - next_index,
            "; ".join(f"{market}: {','.join(ids)}" for market, ids in unreached.items()),
        )
    # An absent optional key produces one source error per run, not per instrument.
    if not get_settings().FINNHUB_API_KEY and "finnhub" in source_stats:
        source_stats["finnhub"]["errors"] = 1
        errors = [e for e in errors if not e.startswith("finnhub:")] + ["finnhub: key not set"]
    stats.update(source_stats)
    stats.update({"cleaning": cleaning, "cleaning_samples": samples, "classifier": classifier})
    run.stats = stats
    run.errors = list(dict.fromkeys(errors))[:50]
    run.instruments_processed = processed
    run.finished_at = datetime.now(UTC)
    run.status = "failed" if failed else "partial" if errors or next_index < len(entries) else "ok"
    session.commit()
    return run
