"""Tavily search execution + daily-budget tracking + holding-relevant news gap-fill.

Split out of report_generator.py (#37).
"""

from __future__ import annotations

import logging
import uuid
from datetime import date
from typing import Any, cast

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.report import Report
from app.services.report_context import ReportInputsDict

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


def _run_tavily_search(
    queries: list[str],
    budget: int,
) -> list[dict[str, Any]]:
    """Execute up to `budget` Tavily searches.  Returns flattened result list.

    Each result dict: {query, title, url, content, score}.
    Failures on individual queries are logged and skipped (degraded mode).
    """
    settings = get_settings()
    api_key = settings.TAVILY_API_KEY.get_secret_value()
    effective_queries = queries[:budget]

    all_results: list[dict[str, Any]] = []
    for i, query in enumerate(effective_queries):
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
                all_results.append(
                    {
                        "query": query,
                        "title": r.get("title", ""),
                        "url": r.get("url", ""),
                        "content": r.get("content", "")[:600],
                        "score": r.get("score", 0.0),
                        "index": len(all_results) + 1,
                    }
                )
            logger.info(
                "Tavily query %d/%d: %d results",
                i + 1,
                len(effective_queries),
                len(data.get("results", [])),
            )
        except Exception:
            logger.exception("Tavily search failed for query: %s", query)
    return all_results


def _tavily_used_today(
    session: Session, report_date: date, exclude_report_id: uuid.UUID | None = None
) -> int:
    """Return the number of Tavily queries already fired today (ET calendar date).

    Counts search_queries stored in report_inputs of terminal-state reports so
    a second run in the same day (manual + after_close, or a retry) sees the
    cumulative daily spend. Excludes the current in-progress row to avoid
    double-counting a retry. Note: Hermes shares this Tavily key; cross-project
    spend is not tracked here — the budget is a Portfonia-only floor.
    """
    rows = session.execute(
        select(Report.report_inputs, Report.id).where(
            Report.report_date == report_date,
            Report.status.in_(("success", "skipped", "needs_review")),
        )
    ).all()
    total = 0
    for inputs, row_id in rows:
        if row_id == exclude_report_id:
            continue
        if inputs and isinstance(inputs, dict):
            total += len(cast(ReportInputsDict, inputs).get("search_queries", []))
    return total


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
