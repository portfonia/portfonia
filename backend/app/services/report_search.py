"""Tavily search execution + daily-budget tracking + holding-relevant news gap-fill.

Split out of report_generator.py (#37). Search-result caching added in issue
#128 A2 (design doc §4.4, Hermes/Portfonia/Docs/Ring 1-A design.md).
"""

from __future__ import annotations

import hashlib
import logging
from datetime import date
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.search_cache import SearchCache

logger = logging.getLogger(__name__)

# Maximum search queries to run per report (avoids blowing Tavily daily budget
# on a single run; the LLM is instructed to output ≤ this many anyway).
_MAX_SEARCH_QUERIES = 5
_TAVILY_MAX_RESULTS = 5  # results per query
_TAVILY_SEARCH_DEPTH = "basic"

# Cap on anomaly holdings that get a targeted live search when the captured
# store has nothing for them (the INTC-Google-foundry miss: a window-relevant
# story that the RSS sources never carried). Bounded to protect the Tavily daily
# budget; ordered by |move| so the most-moved holdings are covered first.
_MAX_TARGETED_ANOMALY_SEARCHES = 3


def _normalize_query(query: str) -> str:
    return " ".join(query.strip().lower().split())


def _query_hash(query: str) -> str:
    return hashlib.sha256(_normalize_query(query).encode()).hexdigest()


def _get_cached_search(
    session: Session, query: str, trade_date: date
) -> list[dict[str, Any]] | None:
    row = session.execute(
        select(SearchCache.results).where(
            SearchCache.query_hash == _query_hash(query), SearchCache.trade_date == trade_date
        )
    ).scalar_one_or_none()
    return row


def _put_cached_search(
    session: Session, query: str, trade_date: date, results: list[dict[str, Any]]
) -> None:
    stmt = (
        pg_insert(SearchCache)
        .values(
            query_hash=_query_hash(query),
            query=query,
            trade_date=trade_date,
            results=results,
        )
        .on_conflict_do_nothing(constraint="uq_search_cache_query_date")
    )
    session.execute(stmt)
    session.flush()


def _fetch_one_query(query: str) -> list[dict[str, Any]]:
    """Execute exactly one Tavily search HTTP call. Failures are logged and
    swallowed (degraded mode) — same contract the pre-A2 batch loop had."""
    settings = get_settings()
    api_key = settings.TAVILY_API_KEY.get_secret_value()
    results: list[dict[str, Any]] = []
    try:
        resp = httpx.post(
            "https://api.tavily.com/search",
            json={
                "api_key": api_key,
                "query": query,
                "search_depth": _TAVILY_SEARCH_DEPTH,
                "topic": "news",
                "max_results": _TAVILY_MAX_RESULTS,
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        for r in data.get("results", []):
            results.append(
                {
                    "query": query,
                    "title": r.get("title", ""),
                    "url": r.get("url", ""),
                    "content": r.get("content", "")[:600],
                    "score": r.get("score", 0.0),
                    "index": len(results) + 1,
                }
            )
        logger.info("Tavily query: %d results (%s)", len(results), query)
    except Exception:
        logger.exception("Tavily search failed for query: %s", query)
    return results


def _run_tavily_search(
    session: Session,
    queries: list[str],
    trade_date: date,
    budget: int,
) -> list[dict[str, Any]]:
    """Cache-first execution of Tavily searches. Returns flattened result list.

    Each query is checked against `search_cache` (keyed by normalized-query
    hash + trade_date, issue #128 A2) before hitting the network. A cache hit
    costs nothing against `budget` — `budget` is the number of REAL HTTP calls
    still allowed today (pre-A2 this parameter counted PROPOSED queries, and a
    cache-hit query didn't exist yet to distinguish from a real one — see
    `_tavily_used_today`). Once `budget` is exhausted, remaining cache-miss
    queries are skipped (degraded mode, same as the pre-A2 truncation).

    ANY real attempt (a genuinely empty 200, or an exception) is cached as an
    empty result — review round 1 bug: caching only non-empty results meant
    an empty-but-successful response, or a failed request, was silently
    uncounted, so the identical query got re-billed by every later report the
    same day (`_tavily_used_today` derives its count from `search_cache` row
    presence, not from the results inside them). The tradeoff — a transient
    outage suppresses retries for the identifier for the rest of the day — is
    deliberate and matches the daily-boundary retry horizon this whole cache
    already uses everywhere else.
    """
    all_results: list[dict[str, Any]] = []
    remaining = budget
    for query in queries:
        cached = _get_cached_search(session, query, trade_date)
        if cached is not None:
            # Round 4 review finding: a cache hit returns the FIRST writer's
            # stored results, whose "query" field is that writer's own
            # (un-normalized) string — not this caller's. Rewrite it to the
            # current query so a downstream exact-string remap (e.g.
            # report_generator.py's targeted-search-to-L1-headline mapping)
            # doesn't silently miss a same-hash, differently-cased/spaced hit.
            all_results.extend({**r, "query": query} for r in cached)
            continue
        if remaining <= 0:
            logger.info("Tavily query skipped (daily budget exhausted): %s", query)
            continue
        fresh = _fetch_one_query(query)
        _put_cached_search(session, query, trade_date, fresh)
        all_results.extend(fresh)
        remaining -= 1
    return all_results


def _tavily_used_today(session: Session, report_date: date) -> int:
    """Return the number of REAL Tavily API calls made today (ET calendar date).

    Issue #128 A2: previously this counted `search_queries` stored in
    report_inputs across today's reports — a query that hit `search_cache`
    (no network call, no cost) was still counted as if it had spent budget.
    `search_cache` rows are written exactly once per distinct
    (normalized query, trade_date) via ON CONFLICT DO NOTHING regardless of
    which report/user triggered the fetch, so counting rows for today IS the
    actual spend — no per-report bookkeeping or exclude_report_id needed
    anymore (a retry of the same report doesn't re-spend budget for queries
    the day already paid for; search_cache isn't tied to any one report).
    Note: Hermes shares this Tavily key; cross-project spend is not tracked
    here — the budget is a Portfonia-only floor.
    """
    return int(
        session.execute(
            select(func.count())
            .select_from(SearchCache)
            .where(SearchCache.trade_date == report_date)
        ).scalar_one()
    )


def _targeted_anomaly_queries(
    anomalies: list[dict[str, Any]],
    covered: set[str],
    max_n: int = _MAX_TARGETED_ANOMALY_SEARCHES,
) -> list[tuple[str, str]]:
    """Build (identifier, query) for the most-moved anomaly holdings that have NO
    recalled window news, so a targeted live search can explain the move.

    Holdings are holdings-derived, so this runs after Pass 1 (Pass 1 stays
    holdings-free) and its results are supplied only to Pass 2. `anomalies` is
    already sorted by largest |move| upstream.
    """
    queries: list[tuple[str, str]] = []
    for a in anomalies:
        ident = a.get("identifier", "")
        if not ident or ident in covered:
            continue
        # Funds and non-US tickers tend to have no clean English news query;
        # still try with the name, but the ticker drives recall precision.
        name = a.get("name", "")
        label = f"{name} {ident}".strip()
        queries.append((ident, f"{label} stock news catalyst"))
        if len(queries) >= max_n:
            break
    return queries


def _targeted_weight_queries(
    weight_ids: list[str],
    covered: set[str],
    window_start: date,
    window_end: date,
    max_n: int,
) -> list[tuple[str, str]]:
    """Build (identifier, query) for large-weight holdings with no recalled
    window news (issue #128 narrative-layer redesign, 2026-08-20 design
    amendment "make v6 the production path" item 1).

    Unlike `_targeted_anomaly_queries`, the query is date-locked to this
    report's own window — an unqualified "{ident} stock news catalyst" pulled
    generic, sometimes months-stale articles in the v5 overlay compare (one
    TSM hit was dated ~9 months before the window). Extracted as a standalone
    function (previously inlined in report_generator.py) specifically so a
    verification script can import and call the SAME code the real
    generate_report() path runs, rather than re-deriving the query string
    format itself — v5/v6's overlay compares had reimplemented this logic in
    the one-off script, which is exactly what this refactor stops needing.

    Callers append these results to whatever Pass 1's own macro-theme search
    already produced (never replace it) — this function only builds queries,
    it has no opinion on how its results get merged into ctx.search_results.
    """
    return [
        (
            ident,
            f"{ident} stock news catalyst {window_start.isoformat()} to {window_end.isoformat()}",
        )
        for ident in weight_ids
        if ident not in covered
    ][:max_n]


def _rank_title_matches_first(
    results: list[dict[str, Any]], query_to_identifier: dict[str, str]
) -> list[dict[str, Any]]:
    """Stable-sort targeted-search results so a title that actually names the
    identifier is ranked ahead of one that only matched Tavily's own
    relevance score (design amendment item 1) — the same "a term in the
    headline is what the story is ABOUT" lesson `recall_holding_news` already
    applies. Reorders only; never discards or truncates. Returns a new list
    (does not mutate `results`).
    """

    def _not_title_match(result: dict[str, Any]) -> int:
        ident = query_to_identifier.get(result.get("query", ""), "")
        return 0 if ident and ident.upper() in result.get("title", "").upper() else 1

    return sorted(results, key=_not_title_match)
