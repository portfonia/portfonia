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


# ---------------------------------------------------------------------------
# _targeted_weight_queries / _rank_title_matches_first (issue #128
# narrative-layer redesign, 2026-08-20 design amendment — extracted from
# report_generator.py so the real generate_report() path and a verification
# script import the SAME functions, not two independently-maintained copies).
# ---------------------------------------------------------------------------


def test_targeted_weight_queries_date_locks_to_the_report_window() -> None:
    queries = rs._targeted_weight_queries(
        ["TSM"], set(), date(2026, 8, 14), date(2026, 8, 17), max_n=5
    )
    assert queries == [("TSM", "TSM stock news catalyst 2026-08-14 to 2026-08-17")]


def test_targeted_weight_queries_skips_already_covered_identifiers() -> None:
    queries = rs._targeted_weight_queries(
        ["TSM", "QQQ"], {"TSM"}, date(2026, 8, 14), date(2026, 8, 17), max_n=5
    )
    assert [ident for ident, _q in queries] == ["QQQ"]


def test_targeted_weight_queries_respects_max_n() -> None:
    queries = rs._targeted_weight_queries(
        ["TSM", "QQQ", "ASML"], set(), date(2026, 8, 14), date(2026, 8, 17), max_n=2
    )
    assert len(queries) == 2


# ---------------------------------------------------------------------------
# date_windows: real Tavily start_date/end_date filtering, not just query text
# (PR #168 round 2 review, suggestion) — the "date lock" `_targeted_weight_
# queries` builds above was, until this fix, only extra tokens in the query
# STRING; Tavily's actual `start_date`/`end_date` publish-date filters were
# never sent, so an old article that never happens to mention those ISO
# strings could still return — the exact stale-article failure the 2026-08-20
# design amendment set out to close.
# ---------------------------------------------------------------------------


def test_run_tavily_search_sends_date_window_to_tavily_api(db_session: Session) -> None:
    query = "TSM stock news catalyst 2026-08-14 to 2026-08-17"
    with patch(
        "app.services.report_search.httpx.post", return_value=_fake_response(_ONE_RESULT)
    ) as mock_post:
        rs._run_tavily_search(
            db_session,
            [query],
            _DATE,
            budget=5,
            date_windows={query: (date(2026, 8, 14), date(2026, 8, 17))},
        )

    sent = mock_post.call_args.kwargs["json"]
    assert sent["start_date"] == "2026-08-14"
    assert sent["end_date"] == "2026-08-17"


def test_run_tavily_search_omits_date_params_for_queries_outside_date_windows(
    db_session: Session,
) -> None:
    """A query with no entry in `date_windows` (every pre-existing caller —
    Pass 1's macro queries, `_targeted_anomaly_queries`, the L1 leftover
    top-up) must send no date filter at all, exactly as before this fix."""
    with patch(
        "app.services.report_search.httpx.post", return_value=_fake_response(_ONE_RESULT)
    ) as mock_post:
        rs._run_tavily_search(db_session, ["NVDA earnings"], _DATE, budget=5)

    sent = mock_post.call_args.kwargs["json"]
    assert "start_date" not in sent
    assert "end_date" not in sent


def test_rank_title_matches_first_promotes_title_hit_over_relevance_score() -> None:
    """The v5 compare's TSM query returned a mostly-generic Tavily result set
    despite a stronger relevance score on the off-target item — this is the
    reranking that fixes it, now a standalone tested function rather than
    inline logic only exercised end-to-end."""
    off_target = {
        "query": "TSM stock news catalyst 2026-08-14 to 2026-08-17",
        "title": "Broad market roundup",
    }
    on_target = {
        "query": "TSM stock news catalyst 2026-08-14 to 2026-08-17",
        "title": "TSM posts record Q1",
    }
    ranked = rs._rank_title_matches_first(
        [off_target, on_target], {"TSM stock news catalyst 2026-08-14 to 2026-08-17": "TSM"}
    )
    assert ranked == [on_target, off_target]


def test_rank_title_matches_first_is_stable_within_each_group() -> None:
    """Reorders only; never discards or reshuffles ties — two title-matching
    results keep their original relative order, and so do two non-matching
    ones."""
    a = {"query": "q", "title": "TSM alpha"}
    b = {"query": "q", "title": "TSM beta"}
    c = {"query": "q", "title": "unrelated one"}
    d = {"query": "q", "title": "unrelated two"}
    ranked = rs._rank_title_matches_first([a, c, b, d], {"q": "TSM"})
    assert ranked == [a, b, c, d]


def test_rank_title_matches_first_returns_a_new_list() -> None:
    original = [{"query": "q", "title": "x"}]
    ranked = rs._rank_title_matches_first(original, {"q": "TSM"})
    assert ranked is not original
