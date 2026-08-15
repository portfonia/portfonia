"""Tavily search cache (issue #128, Ring 1 A2 — design doc §4.4).

Unlike the rest of report_search.py's functions (`_tavily_used_today` etc.,
currently exercised only indirectly via test_report_generator.py's mocked
pipeline — a pre-A2 gap, not something this file's scope extends to fixing),
the new cache-aware `_run_tavily_search` is unit-tested directly here against
a real db_session, mocking only the httpx boundary.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.search_cache import SearchCache
from app.services import report_search as rs

_DATE = date(2026, 8, 15)


def _fake_response(results: list[dict[str, object]]) -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {"results": results}
    return resp


_ONE_RESULT = [
    {"title": "NVDA earnings beat", "url": "https://x.com/1", "content": "c", "score": 0.9}
]


def test_second_identical_query_same_day_skips_http(db_session: Session) -> None:
    """UAT-5: the same query proposed twice in one day (two reports, or two
    phases of the same report) must not hit Tavily's HTTP API a second time."""
    with patch(
        "app.services.report_search.httpx.post", return_value=_fake_response(_ONE_RESULT)
    ) as mock_post:
        first = rs._run_tavily_search(db_session, ["NVDA earnings"], _DATE, budget=5)
        second = rs._run_tavily_search(db_session, ["NVDA earnings"], _DATE, budget=5)

    mock_post.assert_called_once()
    assert first
    assert second
    assert first[0]["title"] == second[0]["title"] == "NVDA earnings beat"


def test_cache_hit_does_not_consume_budget(db_session: Session) -> None:
    """A cache hit is free — a second call with budget=0 still returns the
    cached result instead of being truncated away."""
    with patch("app.services.report_search.httpx.post", return_value=_fake_response(_ONE_RESULT)):
        rs._run_tavily_search(db_session, ["NVDA earnings"], _DATE, budget=5)

    with patch("app.services.report_search.httpx.post") as mock_post:
        result = rs._run_tavily_search(db_session, ["NVDA earnings"], _DATE, budget=0)

    mock_post.assert_not_called()
    assert result and result[0]["title"] == "NVDA earnings beat"


def test_fresh_query_beyond_budget_is_skipped(db_session: Session) -> None:
    with patch("app.services.report_search.httpx.post") as mock_post:
        result = rs._run_tavily_search(db_session, ["brand new uncached query"], _DATE, budget=0)

    mock_post.assert_not_called()
    assert result == []


def test_query_normalization_shares_cache_across_case_and_whitespace(db_session: Session) -> None:
    with patch(
        "app.services.report_search.httpx.post", return_value=_fake_response(_ONE_RESULT)
    ) as mock_post:
        rs._run_tavily_search(db_session, ["  NVDA Earnings  "], _DATE, budget=5)
        rs._run_tavily_search(db_session, ["nvda earnings"], _DATE, budget=5)

    mock_post.assert_called_once()


def test_cache_hit_result_query_field_matches_the_current_caller(db_session: Session) -> None:
    """Round 4 review finding: a cache HIT returns the FIRST writer's stored
    results, whose `query` field is that writer's original (un-normalized)
    string — not the current caller's. A downstream consumer that maps
    results back to an identifier by exact `result["query"]` match (as
    report_generator.py's targeted-search-to-L1-headline remap does) would
    silently miss every cache-hit result whose caller used a differently-
    cased/spaced (but same-hash) query string."""
    with patch("app.services.report_search.httpx.post", return_value=_fake_response(_ONE_RESULT)):
        rs._run_tavily_search(db_session, ["  NVDA Earnings  "], _DATE, budget=5)

    result = rs._run_tavily_search(db_session, ["nvda earnings"], _DATE, budget=5)

    assert result[0]["query"] == "nvda earnings"


def test_different_trade_date_is_a_cache_miss(db_session: Session) -> None:
    with patch(
        "app.services.report_search.httpx.post", return_value=_fake_response(_ONE_RESULT)
    ) as mock_post:
        rs._run_tavily_search(db_session, ["NVDA earnings"], _DATE, budget=5)
        rs._run_tavily_search(db_session, ["NVDA earnings"], date(2026, 8, 16), budget=5)

    assert mock_post.call_count == 2


def test_writes_search_cache_row(db_session: Session) -> None:
    with patch("app.services.report_search.httpx.post", return_value=_fake_response(_ONE_RESULT)):
        rs._run_tavily_search(db_session, ["NVDA earnings"], _DATE, budget=5)

    row = db_session.execute(
        select(SearchCache).where(SearchCache.trade_date == _DATE)
    ).scalar_one()
    assert row.query == "NVDA earnings"
    assert row.results[0]["title"] == "NVDA earnings beat"


def test_failed_query_is_cached_as_empty_for_the_rest_of_the_day(db_session: Session) -> None:
    """Review round 1 bug: an exception (or a real 200-with-zero-results
    response) is still a REAL, billed attempt — if it isn't recorded, every
    later report the same day re-attempts (and re-pays for) the identical
    query. Both are treated as "searched today, nothing usable" and cached
    as an empty result — the tradeoff (a transient outage suppresses retries
    for the rest of the day) is deliberate: the daily cache boundary already
    treats "today" as the natural retry horizon everywhere else in this
    design."""
    with patch("app.services.report_search.httpx.post", side_effect=RuntimeError("network down")):
        result = rs._run_tavily_search(db_session, ["NVDA earnings"], _DATE, budget=5)

    assert result == []
    row = db_session.execute(
        select(SearchCache).where(SearchCache.trade_date == _DATE)
    ).scalar_one()
    assert row.results == []


def test_second_call_after_failure_does_not_retry_the_network(db_session: Session) -> None:
    with patch(
        "app.services.report_search.httpx.post", side_effect=RuntimeError("network down")
    ) as mock_post:
        rs._run_tavily_search(db_session, ["NVDA earnings"], _DATE, budget=5)
        rs._run_tavily_search(db_session, ["NVDA earnings"], _DATE, budget=5)

    mock_post.assert_called_once()


def test_successful_empty_response_is_cached_and_counted(db_session: Session) -> None:
    """Review round 1 bug: `_tavily_used_today` is a COUNT(*) over
    search_cache, but a genuinely successful 200-with-zero-results response
    used to be silently dropped (`if fresh:` was falsy for `[]`), so it was
    never counted as spend — a query that legitimately finds nothing got
    re-billed by every subsequent report that day."""
    with patch("app.services.report_search.httpx.post", return_value=_fake_response([])):
        rs._run_tavily_search(db_session, ["totally obscure query"], _DATE, budget=5)

    assert rs._tavily_used_today(db_session, _DATE) == 1


# ---------------------------------------------------------------------------
# _tavily_used_today: real API-call count, not proposed-query count
# ---------------------------------------------------------------------------


def test_tavily_used_today_counts_search_cache_rows_for_the_date(db_session: Session) -> None:
    db_session.add_all(
        [
            SearchCache(query_hash="h1", query="q1", trade_date=_DATE, results=[]),
            SearchCache(query_hash="h2", query="q2", trade_date=_DATE, results=[]),
            SearchCache(query_hash="h3", query="q3", trade_date=date(2026, 8, 14), results=[]),
        ]
    )
    db_session.flush()

    assert rs._tavily_used_today(db_session, _DATE) == 2


def test_tavily_used_today_zero_when_nothing_cached(db_session: Session) -> None:
    assert rs._tavily_used_today(db_session, _DATE) == 0


def test_cache_hit_does_not_inflate_tavily_used_today(db_session: Session) -> None:
    """The exact bug this fixes (design doc §4.4): a query that hits cache
    makes no real API call, so re-proposing it must not double the daily
    spend count."""
    with patch("app.services.report_search.httpx.post", return_value=_fake_response(_ONE_RESULT)):
        rs._run_tavily_search(db_session, ["NVDA earnings"], _DATE, budget=5)
        rs._run_tavily_search(db_session, ["NVDA earnings"], _DATE, budget=5)

    assert rs._tavily_used_today(db_session, _DATE) == 1
