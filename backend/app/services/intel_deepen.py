"""Two in-memory deepening waves, concurrent paid calls and URL-free evidence."""

import re
import threading
from collections import defaultdict
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, replace
from datetime import datetime, timedelta
from decimal import Decimal
from math import ceil
from typing import TypedDict, cast

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.core.timezones import ET
from app.models.intel import InstrumentProfile, IntelSlotRun
from app.models.news import News
from app.models.paid_intel import IntelArticle, IntelArticleLink
from app.services.headline_cleaning import (
    CleaningConfig,
    block_reason,
    classify_headlines,
    load_cleaning_config,
)
from app.services.instrument_news_sources import CollectedItem
from app.services.instrument_profiles import match_instruments
from app.services.instrument_universe import UniverseEntry
from app.services.intel_body import body_verdict, clean_body, strip_outlet_suffix, without_urls
from app.services.intel_deepen_config import DeepenConfig
from app.services.intel_leads import Lead, accepted_recently, excluded, select_leads, url_key
from app.services.intel_records import build_article_record
from app.services.intel_selection import WorkUnit, assign_providers, select_units
from app.services.intel_signals import Signal, slot_history
from app.services.macro_detector import detect_macro_signals
from app.services.news_fetcher import NewsItem
from app.services.paid_search import PaidResult, ParallelClient, TavilyClient
from app.services.paid_usage import PaidUsage


def extract_batches(
    groups: list[list[tuple[WorkUnit, Lead]]], maximum: int
) -> list[list[tuple[WorkUnit, Lead]]]:
    batches = []
    small: list[tuple[WorkUnit, Lead]] = []
    for group in groups:
        if len(group) >= 5:
            if small:
                batches.append(small)
                small = []
            batches.extend(group[i : i + maximum] for i in range(0, len(group), maximum))
        else:
            if len(small) + len(group) > 5:
                batches.append(small)
                small = []
            small.extend(group)
    if small:
        batches.append(small)
    return batches


class ProviderMetrics(TypedDict):
    units: int
    searches: int
    urls_attempted: int
    accepted: int
    rejected: dict[str, int]
    cost_usd: float
    budget: int
    search_filtered: dict[str, int]
    search_classifier_cost_usd: float


class DeepenRun:
    def __init__(
        self,
        session: Session,
        run: IntelSlotRun,
        cfg: DeepenConfig,
        weekend: bool,
        now: datetime,
        previous: datetime,
        universe: list[UniverseEntry],
        signals: dict[str, Signal],
    ) -> None:
        self.run_id = run.id
        self.run_date = run.run_date
        self.slot = run.slot
        self.cfg = cfg
        self.weekend = weekend
        self.now = now
        self.previous = previous
        self.settings = get_settings()
        self.cleaning: CleaningConfig | None
        try:
            self.cleaning = load_cleaning_config()
        except ValueError:
            self.cleaning = None
        self.usage = PaidUsage(session, run, self.settings, weekend, now)
        self.pool = ThreadPoolExecutor(max_workers=self.settings.INTEL_PAID_WORKERS)
        self.coordinator = ThreadPoolExecutor(max_workers=1)
        self.lock = threading.RLock()
        self.items: dict[str, list[CollectedItem]] = {}
        self.aliases = {
            p.identifier: list(p.aliases) for p in session.scalars(select(InstrumentProfile))
        }
        self.names = {
            p.identifier: p.name_en or p.name_zh or p.identifier
            for p in session.scalars(select(InstrumentProfile))
        }
        self.movers = (
            [] if weekend else [u for u in select_units(signals, {}, cfg) if u.kind == "mover"]
        )
        self.mover_ids = {u.identifier for u in self.movers}
        self.movers_started = False
        self.pool_items: list[NewsItem] = []
        self.futures: list[Future[None]] = []
        self.selected: list[WorkUnit] = []
        self.errors: list[str] = []
        if self.cleaning is None:
            self.errors.append("search_filter: ValueError")
        self.searches = 0
        self.search_cap = cfg.caps.weekend_searches if weekend else cfg.caps.searches_per_run
        self.metrics: dict[str, ProviderMetrics] = {
            p: {
                "units": 0,
                "searches": 0,
                "urls_attempted": 0,
                "accepted": 0,
                "rejected": {},
                "cost_usd": 0.0,
                "budget": 0,
                "search_filtered": {},
                "search_classifier_cost_usd": 0.0,
            }
            for p in ("tavily", "parallel")
        }
        self.accepted: dict[str, set[str]] = {p: set() for p in self.metrics}
        self.dual_keys: set[str] = set()

    def collected(self, identifier: str, items: list[CollectedItem]) -> None:
        with self.lock:
            self.items[identifier] = items
            if self.mover_ids and self.mover_ids <= self.items.keys():
                self._start_movers()

    def _start_movers(self) -> None:
        if self.movers_started or not self.movers:
            return
        self.movers_started = True
        items = {k: list(v) for k, v in self.items.items()}
        self.futures.append(
            self.coordinator.submit(self.run_wave, self.movers, items, self.aliases)
        )

    def _pool_links(self, session: Session) -> dict[str, list[CollectedItem]]:
        result: dict[str, list[CollectedItem]] = defaultdict(list)
        for item in self.pool_items:
            nid = session.scalar(select(News.id).where(News.url_hash == item.url_hash))
            for identifier in match_instruments(
                item.title + " " + (item.summary or ""), self.aliases
            ):
                result[identifier].append(
                    CollectedItem(
                        item.title,
                        item.published_at,
                        item.url,
                        item.summary,
                        news_id=nid,
                        identifier=identifier,
                    )
                )
        return result

    def _call(
        self,
        provider: str,
        operation: str,
        query: str,
        *,
        start: object = None,
        leads: list[Lead] | None = None,
    ) -> tuple[str, PaidResult] | None:
        count = len(leads or []) if operation == "extract" else 1
        estimate = (
            Decimal(ceil(count / 5) if operation == "extract" else 1)
            if provider == "tavily"
            else Decimal(".001") * count
            if operation == "extract"
            else Decimal(".005")
        )
        reservation = self.usage.reserve(provider, estimate)
        if reservation is None:
            return None
        client = (
            TavilyClient(self.settings, self.cfg.extract.tavily_chunks_per_source)
            if provider == "tavily"
            else ParallelClient(self.settings, self.cfg.extract.tavily_chunks_per_source)
        )
        try:
            from datetime import date

            result = (
                client.extract([lead.url for lead in leads or []], query)
                if operation == "extract"
                else client.search(query, cast(date, start), self.run_date)
            )
            if result.http_status not in (None, 401, 403) or result.sent_timeout:
                with SessionLocal() as session:
                    self.usage.complete(
                        reservation,
                        operation,
                        result.units,
                        result.cost_usd,
                        result.http_status,
                        session=session,
                        commit=True,
                    )
            else:
                self.usage.release(reservation)
        except Exception:
            self.usage.release(reservation)
            raise
        with self.lock:
            self.metrics[provider]["cost_usd"] = float(self.metrics[provider]["cost_usd"]) + float(
                result.cost_usd
            )
            if operation == "search":
                self.metrics[provider]["searches"] = int(self.metrics[provider]["searches"]) + 1
        if result.status_class in ("invalid_key", "quota_or_rate"):
            self.usage.disable(provider, result.status_class)
        if result.status_class != "success":
            with self.lock:
                self.errors.append(f"{provider} {operation}: {result.status_class}")
        return provider, result

    def _provider(self, wanted: str) -> str | None:
        available = self.usage.available()
        return (
            wanted
            if wanted in available
            else next((p for p in ("tavily", "parallel") if p in available), None)
        )

    def _search(self, provider: str, unit: WorkUnit) -> tuple[str, list[Lead]]:
        name = self.names.get(unit.identifier, unit.identifier)
        query = (
            f"Why is {name} stock {'down' if '-' in unit.reason else 'up'}"
            if unit.kind == "mover"
            else f"{name} {unit.identifier} news"
        )
        for attempt in range(2):
            chosen = self._provider(provider)
            if not chosen:
                return provider, []
            if self.cleaning is None:
                return chosen, []
            with self.lock:
                if self.searches >= self.search_cap:
                    return chosen, []
            start = unit.window_start or self.previous.astimezone(ET).date()
            outcome = self._call(chosen, "search", query, start=start)
            if outcome and (outcome[1].http_status is not None or outcome[1].sent_timeout):
                with self.lock:
                    self.searches += 1
            if not outcome:
                return chosen, []
            _, result = outcome
            if result.status_class in ("invalid_key", "quota_or_rate") and attempt == 0:
                provider = "parallel" if chosen == "tavily" else "tavily"
                continue
            with SessionLocal() as session:
                leads = [
                    lead
                    for lead in result.leads
                    if not excluded(lead.url, self.cfg)
                    and (
                        lead.published_at is None
                        or lead.published_at.astimezone(ET).date() >= start - timedelta(days=7)
                    )
                    and not accepted_recently(session, lead.url, self.cfg, self.now)
                ]
            aliases = self.aliases.get(unit.identifier, [unit.identifier])
            survivors: list[tuple[Lead, CollectedItem]] = []
            for lead in leads:
                item = CollectedItem(lead.title, lead.published_at or self.now, lead.url)
                reason = block_reason(item, aliases, [], self.cleaning)
                if reason:
                    with self.lock:
                        filtered = self.metrics[chosen]["search_filtered"]
                        filtered[reason] = filtered.get(reason, 0) + 1
                    continue
                survivors.append((lead, item))
            if survivors:
                items = [item for _, item in survivors]
                labels, cost, failed = classify_headlines(items, unit.identifier, aliases)
                with self.lock:
                    self.metrics[chosen]["search_classifier_cost_usd"] = (
                        float(self.metrics[chosen]["search_classifier_cost_usd"]) + cost
                    )
                if failed:
                    with self.lock:
                        filtered = self.metrics[chosen]["search_filtered"]
                        filtered["classifier_failed"] = filtered.get("classifier_failed", 0) + len(
                            survivors
                        )
                        self.errors.append(failed)
                    survivors = []
                    leads = []
                else:
                    kept: list[Lead] = []
                    for index, (lead, _item) in enumerate(survivors):
                        label = labels.get(index)
                        if label in ("keep", "mention"):
                            kept.append(
                                replace(lead, title=strip_outlet_suffix(lead.title, aliases))
                            )
                            continue
                        reason = (
                            "unlabeled_llm"
                            if label is None
                            else {
                                "promo": "promo_llm",
                                "unrelated": "unrelated_llm",
                            }.get(label, "unlabeled_llm")
                        )
                        with self.lock:
                            filtered = self.metrics[chosen]["search_filtered"]
                            filtered[reason] = filtered.get(reason, 0) + 1
                    leads = kept
            else:
                leads = []
            leads.sort(
                key=lambda lead: (
                    lead.published_at is None,
                    -lead.published_at.timestamp() if lead.published_at else 0,
                )
            )
            return chosen, leads[:2]
        return provider, []

    def run_wave(
        self,
        units: list[WorkUnit],
        items: dict[str, list[CollectedItem]],
        aliases: dict[str, list[str]],
    ) -> None:
        assigned = (
            assign_providers(units, self.settings.INTEL_AB_MODE, self.usage.available(), self.cfg)
            if not any(u.providers for u in units)
            else units
        )
        with self.lock:
            self.selected.extend(assigned)
        groups: dict[str, list[list[tuple[WorkUnit, Lead]]]] = {"tavily": [], "parallel": []}
        with SessionLocal() as session:
            pool_links = self._pool_links(session)
            theme_items = {
                hit.theme: hit.articles
                for hit in detect_macro_signals(
                    self.pool_items, max_articles_per_theme=len(self.pool_items) or 1
                ).hits
            }
            for unit in assigned:
                candidates = (
                    items.get(unit.identifier, []) + pool_links.get(unit.identifier, [])
                    if unit.identifier
                    else [
                        CollectedItem(
                            i.title,
                            i.published_at,
                            i.url,
                            i.summary,
                            news_id=session.scalar(
                                select(News.id).where(News.url_hash == i.url_hash)
                            ),
                        )
                        for i in theme_items.get(unit.theme, [])
                    ]
                )
                leads = select_leads(
                    session,
                    unit,
                    candidates,
                    aliases.get(unit.identifier, [unit.identifier]),
                    self.cfg,
                    self.now,
                    self.previous,
                )
                if len(unit.providers) == 2:
                    with self.lock:
                        self.dual_keys.update(url_key(lead.url) for lead in leads)
                for provider in unit.providers:
                    chosen = provider
                    owned = leads
                    if not owned and unit.identifier:
                        chosen, owned = self._search(provider, unit)
                    if owned:
                        groups[chosen].append([(unit, lead) for lead in owned])
        jobs = []
        for provider, provider_groups in groups.items():
            batches = (
                extract_batches(provider_groups, self.cfg.extract.tavily_batch_max_urls)
                if provider == "tavily"
                else provider_groups
            )
            for batch in batches:
                jobs.append(self.pool.submit(self._extract_batch, provider, batch))
        for job in jobs:
            try:
                job.result()
            except Exception as exc:
                with self.lock:
                    self.errors.append(f"deepening: {type(exc).__name__}")

    def _extract_batch(
        self, wanted: str, batch: list[tuple[WorkUnit, Lead]], retried: bool = False
    ) -> None:
        provider = self._provider(wanted)
        if not provider:
            self.usage.skip(wanted)
            with self.lock:
                self.metrics[wanted]["budget"] = int(self.metrics[wanted]["budget"]) + len(batch)
            return
        if provider == "parallel" and len({u.identifier or u.theme for u, _ in batch}) > 1:
            grouped: dict[str, list[tuple[WorkUnit, Lead]]] = defaultdict(list)
            for unit, lead in batch:
                grouped[unit.identifier or unit.theme].append((unit, lead))
            for group in grouped.values():
                self._extract_batch(provider, group, retried)
            return
        count = self.usage.extract_size(provider, len(batch))
        with self.lock:
            self.metrics[provider]["budget"] = (
                int(self.metrics[provider]["budget"]) + len(batch) - count
            )
        batch = batch[:count]
        if not batch:
            return
        words = list(
            dict.fromkeys(
                w for _, lead in batch for w in re.findall(r"\w+", without_urls(lead.title))
            )
        )[:10]
        names = list(
            dict.fromkeys(
                (self.names.get(u.identifier, u.identifier) + " " + u.identifier).strip()
                if u.identifier
                else u.theme
                for u, _ in batch
            )
        )
        query = " ".join(names + words)
        outcome = self._call(provider, "extract", query, leads=[lead for _, lead in batch])
        if not outcome:
            if not retried:
                self._extract_batch("parallel" if provider == "tavily" else "tavily", batch, True)
            return
        _, result = outcome
        with self.lock:
            self.metrics[provider]["units"] = int(self.metrics[provider]["units"]) + len(
                {u.identifier or u.theme for u, _ in batch}
            )
            self.metrics[provider]["urls_attempted"] = int(
                self.metrics[provider]["urls_attempted"]
            ) + len(batch)
        with SessionLocal() as session:
            for unit, lead in batch:
                text = clean_body(lead.url, result.bodies.get(lead.url, ""), self.cfg)
                accepted, reason = body_verdict(text, self.cfg)
                if result.status_class != "success" or lead.url not in result.bodies:
                    accepted, reason = False, "provider_error"
                status = (
                    "accepted"
                    if accepted
                    else "failed"
                    if reason == "provider_error"
                    else "rejected"
                )
                record = (
                    build_article_record(lead.title, lead.published_at, self.now, text)
                    if accepted
                    else None
                )
                stmt = insert(IntelArticle).values(
                    slot_run_id=self.run_id,
                    provider=provider,
                    url_key=url_key(lead.url),
                    news_id=lead.news_id,
                    status=status,
                    reject_reason=reason,
                    record=record,
                    body_chars=len(text) if accepted else None,
                    fetched_at=self.now,
                )
                article_id = session.scalar(
                    stmt.on_conflict_do_update(
                        constraint="uq_intel_articles_key_provider",
                        set_={
                            k: getattr(stmt.excluded, k)
                            for k in (
                                "slot_run_id",
                                "news_id",
                                "status",
                                "reject_reason",
                                "record",
                                "body_chars",
                                "fetched_at",
                            )
                        },
                    ).returning(IntelArticle.id)
                )
                session.execute(
                    insert(IntelArticleLink)
                    .values(
                        article_id=article_id,
                        identifier=unit.identifier or None,
                        theme=unit.theme or None,
                        role=unit.kind,
                    )
                    .on_conflict_do_nothing(constraint="uq_intel_article_links_key")
                )
                with self.lock:
                    if accepted:
                        self.accepted[provider].add(url_key(lead.url))
                        self.metrics[provider]["accepted"] = (
                            int(self.metrics[provider]["accepted"]) + 1
                        )
                    else:
                        rejected = self.metrics[provider]["rejected"]
                        rejected[reason or "empty"] = rejected.get(reason or "empty", 0) + 1
            session.commit()
        if result.status_class in ("invalid_key", "quota_or_rate") and not retried:
            self._extract_batch("parallel" if provider == "tavily" else "tavily", batch, True)

    def finish(
        self, session: Session, signals: dict[str, Signal], pool_items: list[NewsItem]
    ) -> None:
        self.pool_items = pool_items
        with self.lock:
            self._start_movers()
        fresh = [
            i
            for i in pool_items
            if session.scalar(
                select(News.id).where(News.url_hash == i.url_hash, News.fetched_at > self.previous)
            )
            is not None
        ]
        counts = {
            hit.theme: len(hit.articles)
            for hit in detect_macro_signals(fresh, max_articles_per_theme=len(fresh) or 1).hits
        }
        histories: dict[str, list[int]] = defaultdict(list)
        for run in slot_history(session, self.slot, self.now, self.cfg, self.weekend):
            stored = run.details.get("theme_counts")
            if isinstance(stored, dict):
                for theme in counts:
                    histories[theme].append(int(stored.get(theme, 0)))
        self.theme_counts = counts
        units = [
            u
            for u in select_units(
                signals,
                counts,
                self.cfg,
                weekend=self.weekend,
                theme_history=histories,
                window_start=self.previous.astimezone(ET).date() if self.weekend else None,
            )
            if u.kind != "mover"
        ]
        self.futures.append(
            self.coordinator.submit(self.run_wave, units, dict(self.items), self.aliases)
        )
        self.close()

    def close(self) -> None:
        self.coordinator.shutdown(wait=True)
        self.pool.shutdown(wait=True)
        for future in self.futures:
            try:
                future.result()
            except Exception as exc:
                self.errors.append(f"deepening: {type(exc).__name__}")
        self.items.clear()
        self.pool_items.clear()

    def details(self) -> dict[str, object]:
        metrics = {}
        for provider, values in self.metrics.items():
            other = "parallel" if provider == "tavily" else "tavily"
            accepted = int(values["accepted"])
            metrics[provider] = {
                **values,
                "cost_per_accepted": float(values["cost_usd"]) / accepted if accepted else None,
                "unique_accepted": len(self.accepted[provider] - self.accepted[other]),
                "dual_unique_accepted": len(
                    (self.accepted[provider] - self.accepted[other]) & self.dual_keys
                ),
            }
        return {
            "selections": [
                {
                    **asdict(u),
                    "window_start": u.window_start.isoformat() if u.window_start else None,
                }
                for u in self.selected
            ],
            "usage": {
                p: {
                    "run": float(self.usage.run_used[p]),
                    "run_cap": float(self.usage.caps[p]),
                    "month": float(self.usage.month_used[p]),
                    "month_limit": float(self.usage.limits[p]),
                    "configured": self.usage.configured[p],
                }
                for p in self.metrics
            },
            "metrics": metrics,
            "errors": self.errors,
            "weekend": self.weekend,
            "mode": self.settings.INTEL_AB_MODE,
            "warning_ratio": self.settings.PAID_API_WARN_RATIO,
        }
