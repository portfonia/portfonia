"""Two in-memory deepening waves, concurrent paid calls and URL-free evidence."""

import re
import threading
from collections import defaultdict
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, replace
from datetime import date, datetime, timedelta
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
    EarningsCache,
    block_reason,
    classify_headlines,
    classify_macro,
    load_cleaning_config,
    near_duplicate_title,
)
from app.services.instrument_news_sources import CollectedItem
from app.services.instrument_profiles import match_instruments
from app.services.instrument_universe import UniverseEntry
from app.services.intel_body import body_verdict, clean_body, without_urls
from app.services.intel_deepen_config import DeepenConfig
from app.services.intel_leads import (
    Lead,
    accepted_recently,
    excluded,
    select_headlines,
    select_leads,
    stored_headlines,
    url_key,
)
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
    headlines_resolved: int
    headlines_unresolved: int


class UnitOutcome(TypedDict):
    kind: str
    identifier: str
    theme: str
    reason: str
    provider: str | None
    via: str
    searches: int
    accepted: int
    rejected: dict[str, int]
    note: str | None


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
        *,
        earnings_cache: EarningsCache | None = None,
    ) -> None:
        self.earnings_cache = earnings_cache or EarningsCache()
        self.symbols = {entry.identifier: entry.ticker for entry in universe}
        self.search_samples: dict[str, list[str]] = {}
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
        self.movers = [u for u in select_units(signals, {}, cfg) if u.kind == "mover"]
        self.mover_ids = {u.identifier for u in self.movers}
        self.movers_started = False
        self.pool_items: list[NewsItem] = []
        self.futures: list[Future[None]] = []
        self.selected: list[WorkUnit] = []
        self.outcomes: list[UnitOutcome] = []
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
                "headlines_resolved": 0,
                "headlines_unresolved": 0,
            }
            for p in ("tavily", "parallel")
        }
        self.accepted: dict[str, set[str]] = {p: set() for p in self.metrics}
        self.dual_keys: set[str] = set()
        self.macro_rank_failed = 0
        self.macro_rank_partial = 0
        self.macro_classifier_cost_usd = 0.0
        self.macro_leads: list[Lead] = []
        self.macro_themes: dict[str, set[str]] = {}

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
        start: date | None = None,
        end: date | None = None,
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
            result = (
                client.extract([lead.url for lead in leads or []], query)
                if operation == "extract"
                else client.search(query, cast(date, start), end or self.run_date)
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
                error = f"{provider} {operation}: {result.status_class}"
                if result.http_status is not None:
                    error += f" HTTP {result.http_status}"
                    if result.detail:
                        error += f" {result.detail}"
                self.errors.append(error)
        return provider, result

    def _provider(self, wanted: str) -> str | None:
        available = self.usage.available()
        return (
            wanted
            if wanted in available
            else next((p for p in ("tavily", "parallel") if p in available), None)
        )

    def _outcome(self, unit: WorkUnit, provider: str | None, via: str) -> UnitOutcome:
        with self.lock:
            for outcome in self.outcomes:
                if (
                    outcome["kind"],
                    outcome["identifier"],
                    outcome["theme"],
                    outcome["provider"],
                ) == (unit.kind, unit.identifier, unit.theme, provider):
                    return outcome
            outcome = UnitOutcome(
                kind=unit.kind,
                identifier=unit.identifier,
                theme=unit.theme,
                reason=unit.reason,
                provider=provider,
                via=via,
                searches=0,
                accepted=0,
                rejected={},
                note=None,
            )
            self.outcomes.append(outcome)
            return outcome

    def _resolve(
        self, provider: str, unit: WorkUnit, headlines: list[CollectedItem]
    ) -> tuple[str, list[tuple[str, Lead]]]:
        owned: list[tuple[str, Lead]] = []
        chosen = self._provider(provider) or provider
        for headline in headlines:
            outcome = self._outcome(unit, chosen, "search")
            with self.lock:
                if self.searches >= self.search_cap:
                    outcome["note"] = "cap_reached"
                    break
            chosen, leads = self._search_headline(
                chosen, unit, headline, {url_key(lead.url) for _, lead in owned}
            )
            outcome = self._outcome(unit, chosen, "search")
            if leads:
                owned.append((chosen, leads[0]))
            else:
                if outcome["note"] != "cap_reached":
                    outcome["note"] = "no_result"
            with self.lock:
                self.metrics[chosen]["headlines_resolved" if leads else "headlines_unresolved"] += 1
        for owner, _ in owned:
            outcome = self._outcome(unit, owner, "search")
            if outcome["note"] == "no_result":
                outcome["note"] = None
        return chosen, owned

    def _search_headline(
        self,
        provider: str,
        unit: WorkUnit,
        headline: CollectedItem,
        selected: set[str],
    ) -> tuple[str, list[Lead]]:
        query = headline.title
        start = headline.published_at.astimezone(ET).date() - timedelta(days=1)
        end = min(headline.published_at.astimezone(ET).date() + timedelta(days=1), self.run_date)
        caller = provider
        for attempt in range(2):
            chosen = self._provider(provider)
            if not chosen:
                # Record the stop on the provider that last searched; never open an
                # outcome for a fallback that was not called.
                self._outcome(unit, caller, "search")["note"] = "cap_reached"
                return caller, []
            report = self._outcome(unit, chosen, "search")
            caller = chosen
            if self.cleaning is None:
                return chosen, []
            with self.lock:
                if self.searches >= self.search_cap:
                    report["note"] = "cap_reached"
                    return chosen, []
            outcome = self._call(chosen, "search", query, start=start, end=end)
            if outcome and (outcome[1].http_status is not None or outcome[1].sent_timeout):
                with self.lock:
                    self.searches += 1
                    report["searches"] += 1
            if not outcome:
                report["note"] = "cap_reached"
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
                        or lead.published_at.astimezone(ET).date() >= start
                    )
                    and not accepted_recently(session, lead.url, self.cfg, self.now)
                    and url_key(lead.url) not in selected
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
                stale = self.earnings_cache.stale_reason(
                    item, self.symbols.get(unit.identifier, unit.identifier), self.cleaning
                )
                if stale:
                    with self.lock:
                        filtered = self.metrics[chosen]["search_filtered"]
                        filtered[stale] = filtered.get(stale, 0) + 1
                        if stale == "stale_rule":
                            samples = self.search_samples.setdefault(stale, [])
                            if len(samples) < 3:
                                samples.append(item.title)
                    if stale == "stale_rule":
                        continue
                survivors.append((lead, item))
            if survivors:
                items = [item for _, item in survivors]
                symbol = self.symbols.get(unit.identifier, unit.identifier)

                def recap_is_stale(
                    item: CollectedItem,
                    symbol: str = symbol,
                    provider: str = chosen,
                    cleaning: CleaningConfig = self.cleaning,
                ) -> bool:
                    # A title matching the earnings or preview patterns was already checked above.
                    if any(
                        p.search(item.title)
                        for p in cleaning.earnings_patterns + cleaning.preview_patterns
                    ):
                        return False
                    reason = self.earnings_cache.stale_reason(item, symbol, cleaning, recap=True)
                    if reason == "stale_lookup_failed":
                        with self.lock:
                            filtered = self.metrics[provider]["search_filtered"]
                            filtered[reason] = filtered.get(reason, 0) + 1
                    return reason == "stale_rule"

                labels, cost, failed = classify_headlines(
                    items,
                    unit.identifier,
                    aliases,
                    batch_date=self.now.astimezone(ET).date(),
                    recap_is_stale=recap_is_stale,
                )
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
                            kept.append(lead)
                            continue
                        reason = (
                            "unlabeled_llm"
                            if label is None
                            else {
                                "promo": "promo_llm",
                                "unrelated": "unrelated_llm",
                                "stale": "stale_llm",
                            }.get(label, "unlabeled_llm")
                        )
                        with self.lock:
                            filtered = self.metrics[chosen]["search_filtered"]
                            filtered[reason] = filtered.get(reason, 0) + 1
                            if reason == "stale_llm":
                                samples = self.search_samples.setdefault(reason, [])
                                if len(samples) < 3:
                                    samples.append(_item.title)
                    leads = kept
            else:
                leads = []
            # The extracted article records the searched headline's news row, so a
            # stored headline is not searched again once its body is accepted (#681).
            return chosen, [
                replace(lead, news_id=lead.news_id or headline.news_id) for lead in leads[:1]
            ]
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
                macro_labels = None
                if unit.kind == "macro" and unit.providers:
                    candidates = [
                        item
                        for item in candidates
                        if not unit.window_start
                        or item.published_at.astimezone(ET).date() >= unit.window_start
                    ]
                    if candidates:
                        labels, cost, error = classify_macro(candidates)
                        self.macro_classifier_cost_usd += cost
                        if error or not labels:
                            self.macro_rank_failed += 1
                        else:
                            macro_labels = labels
                            self.macro_rank_partial += len(candidates) - len(labels)
                linked_existing: list[Lead] = []
                leads = select_leads(
                    session,
                    unit,
                    candidates,
                    aliases.get(unit.identifier, [unit.identifier]),
                    self.cfg,
                    self.now,
                    macro_labels=macro_labels,
                    macro_selected_keys=set(self.macro_themes) if unit.kind == "macro" else None,
                    linked_existing=linked_existing,
                )
                linked_only = False
                if unit.kind == "macro":
                    had_leads = bool(leads or linked_existing)
                    leads = self._unique_macro_leads(session, unit, [*linked_existing, *leads])
                    linked_only = had_leads and not leads
                if len(unit.providers) == 2:
                    with self.lock:
                        self.dual_keys.update(url_key(lead.url) for lead in leads)
                headlines = (
                    select_headlines(
                        session,
                        unit,
                        candidates,
                        aliases.get(unit.identifier, [unit.identifier]),
                        self.cfg,
                        self.now,
                    )
                    if not leads and unit.identifier
                    else []
                )
                if not leads and not headlines and unit.identifier:
                    # #681: a price move reported in an earlier batch is searched by title.
                    headlines = stored_headlines(
                        session,
                        unit,
                        aliases.get(unit.identifier, [unit.identifier]),
                        self.cfg,
                        self.now,
                    )
                if not leads and not headlines:
                    self._outcome(unit, None, "none")["note"] = (
                        "linked_existing" if linked_only else "no_news"
                    )
                    continue
                if not unit.providers:
                    self._outcome(unit, None, "none")["note"] = "cap_reached"
                for provider in unit.providers:
                    if not leads and unit.identifier:
                        _, resolved = self._resolve(provider, unit, headlines)
                        by_provider: dict[str, list[tuple[WorkUnit, Lead]]] = defaultdict(list)
                        for owner, lead in resolved:
                            by_provider[owner].append((unit, lead))
                        for owner, batch in by_provider.items():
                            groups[owner].append(batch)
                    else:
                        self._outcome(unit, provider, "direct")
                        if leads:
                            groups[provider].append([(unit, lead) for lead in leads])
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

    def _unique_macro_leads(
        self, session: Session, unit: WorkUnit, leads: list[Lead]
    ) -> list[Lead]:
        unique = []
        for lead in leads:
            duplicate = next(
                (
                    prior
                    for prior in self.macro_leads
                    if url_key(prior.url) == url_key(lead.url)
                    or (
                        self.cleaning is not None
                        and near_duplicate_title(prior.title, lead.title, self.cleaning.threshold)
                    )
                ),
                None,
            )
            if duplicate is not None:
                key = url_key(duplicate.url)
                self.macro_themes[key].add(unit.theme)
                for article_id in session.scalars(
                    select(IntelArticle.id).where(
                        IntelArticle.slot_run_id == self.run_id, IntelArticle.url_key == key
                    )
                ):
                    session.execute(
                        insert(IntelArticleLink)
                        .values(article_id=article_id, theme=unit.theme, role="macro")
                        .on_conflict_do_nothing(constraint="uq_intel_article_links_key")
                    )
                session.commit()
                continue
            self.macro_leads.append(lead)
            self.macro_themes[url_key(lead.url)] = {unit.theme}
            unique.append(lead)
        return unique

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
                via = next(
                    (
                        entry["via"]
                        for entry in self.outcomes
                        if (entry["kind"], entry["identifier"], entry["theme"])
                        == (unit.kind, unit.identifier, unit.theme)
                    ),
                    "direct",
                )
                unit_outcome = self._outcome(unit, provider, via)
                text = clean_body(lead.url, result.bodies.get(lead.url, ""), self.cfg)
                accepted, reason = body_verdict(text, self.cfg)
                if result.status_class != "success" or lead.url not in result.bodies:
                    accepted, reason = False, "provider_error"
                if accepted and unit.identifier:
                    aliases = self.aliases.get(unit.identifier, [unit.identifier])
                    if not match_instruments(text, {unit.identifier: aliases}):
                        accepted, reason = False, "off_topic"
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
                if record is not None and lead.macro_label is not None:
                    record.update(lead.macro_label)
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
                        where=IntelArticle.status != "accepted" if reason == "off_topic" else None,
                    ).returning(IntelArticle.id)
                )
                themes = (
                    self.macro_themes.get(url_key(lead.url), {unit.theme})
                    if unit.kind == "macro"
                    else {unit.theme}
                )
                for theme in sorted(themes) if reason != "off_topic" else []:
                    session.execute(
                        insert(IntelArticleLink)
                        .values(
                            article_id=article_id,
                            identifier=unit.identifier or None,
                            theme=theme or None,
                            role=unit.kind,
                        )
                        .on_conflict_do_nothing(constraint="uq_intel_article_links_key")
                    )
                with self.lock:
                    if accepted:
                        unit_outcome["accepted"] += 1
                        self.accepted[provider].add(url_key(lead.url))
                        self.metrics[provider]["accepted"] = (
                            int(self.metrics[provider]["accepted"]) + 1
                        )
                    else:
                        unit_rejected = unit_outcome["rejected"]
                        unit_rejected[reason or "empty"] = (
                            unit_rejected.get(reason or "empty", 0) + 1
                        )
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
            "macro_rank_failed": self.macro_rank_failed,
            "macro_rank_partial": self.macro_rank_partial,
            "macro_classifier_cost_usd": self.macro_classifier_cost_usd,
            "outcomes": self.outcomes,
            "search_samples": self.search_samples,
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
