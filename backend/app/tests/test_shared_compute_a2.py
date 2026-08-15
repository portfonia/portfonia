"""End-to-end UAT for Ring 1 stage A2 (issue #128), design doc §7.2
UAT-4/5/6: Hermes/Portfonia/Docs/Ring 1-A design.md.

Same style as test_shared_compute_a1.py: runs
`generate_incremental_report.run()` against a REAL three-user
holdings/price-snapshot fixture through a REAL `db_session`, mocking only
the LLM/Tavily-HTTP boundary — proving the actual cross-user sharing
properties (L1 shared once, search cached once) at the fan-out level, not
just that ticker_intel.py's/report_search.py's unit tests line up.

Unlike test_shared_compute_a1.py's `_run_batch`, `_run_tavily_search` is
NOT fully mocked here — Pass 1 is mocked to propose a real (but identical
across users) search query, and only the httpx boundary inside
report_search.py is mocked, so the real cache-first `_run_tavily_search`
code path actually runs and can be asserted on (UAT-5).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock, patch

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.price_snapshot import PriceSnapshot
from app.models.report import Report
from app.models.search_cache import SearchCache
from app.models.ticker_intel import TickerIntel
from app.services.macro_detector import MacroSignals
from app.services.portfolio_calculator import Concentration, PortfolioSnapshot
from app.services.window_data import BOOTSTRAP_WATERMARK

# Same fixture shape as test_shared_compute_a1.py: NVDA/AAPL shared by
# U1+U2, SGOL/gold isolated to U3.
_BASELINE_DATE = date(2026, 6, 1)
_BASELINE_AT = BOOTSTRAP_WATERMARK


def _close_at(ticker: str, d: date, close: float, captured_at: datetime) -> PriceSnapshot:
    return PriceSnapshot(
        ticker=ticker,
        market="US",
        session_node="close",
        trade_date=d,
        close=Decimal(str(close)),
        captured_at=captured_at,
    )


def _close(ticker: str, d: date, close: float) -> PriceSnapshot:
    return PriceSnapshot(
        ticker=ticker, market="US", session_node="close", trade_date=d, close=Decimal(str(close))
    )


def _seed_price_snapshots(db_session: Session) -> None:
    """Same shape as test_shared_compute_a1.py's fixture: NVDA flags for both
    U1 and U2, SGOL flags for U3 — every one of the three users has at least
    one anomaly, so none of them hits the quiet-day skip (which would bypass
    Pass 1/Tavily/L1 entirely and confound these tests' assertions)."""
    db_session.add_all(
        [
            _close_at("NVDA", _BASELINE_DATE, 200.0, _BASELINE_AT),
            _close("NVDA", date(2026, 6, 2), 215.0),
            _close("NVDA", date(2026, 6, 3), 215.0),
            _close("NVDA", date(2026, 6, 4), 215.0),
            _close("NVDA", date(2026, 6, 5), 215.0),
            _close("NVDA", date(2026, 6, 6), 215.0),
            _close_at("SGOL", _BASELINE_DATE, 180.0, _BASELINE_AT),
            _close("SGOL", date(2026, 6, 2), 190.0),
            _close("SGOL", date(2026, 6, 3), 190.0),
            _close("SGOL", date(2026, 6, 4), 190.0),
            _close("SGOL", date(2026, 6, 5), 190.0),
            _close("SGOL", date(2026, 6, 6), 190.0),
        ]
    )
    db_session.flush()


def _empty_portfolio_snap() -> PortfolioSnapshot:
    return PortfolioSnapshot(
        base_currency="USD",
        fx_date=date(2026, 6, 6),
        holdings=[],
        total_base=Decimal("0"),
        by_currency={},
        by_asset_type={},
        by_market={},
        by_sector={},
        by_asset_class={},
        concentration=Concentration(
            top_holding_name="",
            top_holding_ratio=Decimal("0"),
            top_holding_asset_class="",
            top3_ratio=Decimal("0"),
            top_asset_class_name="",
            top_asset_class_ratio=Decimal("0"),
            single_holding_watch=False,
            single_holding_high=False,
            top3_watch=False,
            asset_class_watch=False,
            asset_class_high=False,
        ),
        stale_tickers=[],
    )


_PASS2_FILLER = "Filler context. " * 130
_FAKE_LLM_PASS2 = (
    "## §2 Macro Signals\n\nNothing macro. [For information only — not investment advice]\n\n"
    "## §3 Holdings Intelligence\n\nSee anomalies. [For information only — not investment advice]\n\n"
    "## §4 Risk Radar\n\nSee anomalies. [For information only — not investment advice]\n\n"
    + _PASS2_FILLER
)


def _mock_report_llm(client: object, model: str, system: str, user: str, **kwargs: object) -> str:
    """Pass 1 returns the SAME single search query regardless of caller —
    every user's proposed query is textually identical, so the batch's
    second/third `_run_tavily_search` call must hit `search_cache` rather
    than the network (UAT-5)."""
    if kwargs.get("with_holdings"):
        return _FAKE_LLM_PASS2
    return '{"queries": ["gold rally catalyst"]}'


def _fake_tavily_response(results: list[dict[str, object]]) -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {"results": results}
    return resp


@dataclass
class _BatchOutcome:
    result: dict[str, Any]
    mock_l1: MagicMock


def _run_batch(l1_call_side_effect: object) -> _BatchOutcome:
    """Patches every LLM/HTTP boundary the batch can reach EXCEPT
    report_search.httpx.post — callers patch that themselves (as the
    outermost context manager, wrapping this call) so they get the mock
    object back to assert call counts on; nesting a second patch of the
    same target here would shadow the caller's mock instead of sharing it."""
    from app.tasks.report_tasks import generate_incremental_report

    with (
        patch(
            "app.services.report_generator.compute_portfolio", return_value=_empty_portfolio_snap()
        ),
        patch(
            "app.services.report_generator.detect_macro_signals",
            return_value=MacroSignals(hits=[], has_any_hit=False, total_matched_articles=0),
        ),
        patch("app.services.report_generator._openrouter_client", return_value=MagicMock()),
        patch("app.services.report_generator._call_llm", side_effect=_mock_report_llm),
        patch("app.services.report_translation._openrouter_client", return_value=MagicMock()),
        patch("app.services.report_translation._call_llm", side_effect=_mock_report_llm),
        patch("app.services.report_translation.time.sleep"),
        patch("app.services.ticker_intel._openrouter_client", return_value=MagicMock()),
        patch("app.services.ticker_intel._call_llm", side_effect=l1_call_side_effect) as mock_l1,
    ):
        result = generate_incremental_report.run()

    return _BatchOutcome(result=result, mock_l1=mock_l1)


def _mock_l1_llm_ok(client: object, model: str, system: str, user: str, **kwargs: object) -> str:
    return "NVDA rallied on a confirmed earnings beat. [Established]"


def test_l1_analysis_shared_once_across_users_holding_the_same_identifier(
    db_session: Session, three_user_holdings: dict[str, object]
) -> None:
    """UAT-4: U1 and U2 both flag NVDA as an anomaly in the same batch — the
    L1 shared-intel LLM call for NVDA must fire exactly once, not once per
    user."""
    _seed_price_snapshots(db_session)

    with patch(
        "app.services.report_search.httpx.post",
        return_value=_fake_tavily_response(
            [{"title": "Gold rallies", "url": "https://x.com/1", "content": "c", "score": 0.9}]
        ),
    ):
        outcome = _run_batch(_mock_l1_llm_ok)

    assert outcome.result["status"] == "completed"
    l1_calls_for_nvda = [c for c in outcome.mock_l1.call_args_list if "NVDA" in c.args[3]]
    assert len(l1_calls_for_nvda) == 1

    rows = (
        db_session.execute(select(TickerIntel).where(TickerIntel.identifier == "NVDA"))
        .scalars()
        .all()
    )
    assert len(rows) == 1


def test_second_users_identical_search_query_hits_cache(
    db_session: Session, three_user_holdings: dict[str, object]
) -> None:
    """UAT-5: every user's Pass 1 proposes the textually-identical query
    ("gold rally catalyst"), AND U1/U2 both trigger a targeted anomaly
    search for NVDA (identical text — same holding name "NVIDIA", same
    identifier) via §5's holding-relevant-news gap-fill. Across the whole
    3-user batch there are 3 DISTINCT query texts proposed 6 times total
    (pass1 x3 identical, NVDA-targeted x2 identical, gold-targeted x1) — the
    real Tavily endpoint must be hit once per distinct text, never once per
    proposal."""
    _seed_price_snapshots(db_session)

    with patch("app.services.report_search.httpx.post") as mock_post:
        mock_post.return_value = _fake_tavily_response(
            [{"title": "Gold rallies", "url": "https://x.com/1", "content": "c", "score": 0.9}]
        )
        outcome = _run_batch(_mock_l1_llm_ok)
        assert outcome.result["status"] == "completed"

        real_queries = [c.kwargs["json"]["query"] for c in mock_post.call_args_list]

    rows = db_session.execute(select(SearchCache)).scalars().all()
    assert len(rows) == 3
    # Every distinct query text was fetched over the network exactly once,
    # even though NVDA's targeted query was proposed by two users.
    assert len(real_queries) == len(set(real_queries)) == 3
    assert real_queries.count("gold rally catalyst") == 1
    nvda_targeted = [q for q in real_queries if "NVDA" in q]
    assert len(nvda_targeted) == 1


def test_forbidden_l1_output_does_not_block_report_generation(
    db_session: Session, three_user_holdings: dict[str, object]
) -> None:
    """UAT-6: an L1 analysis that trips the compliance scan must not be
    cached, must not propagate to any user's report status, and must not
    stop the batch — every user still gets a normal 'success' report."""

    def _bad_l1_llm(client: object, model: str, system: str, user: str, **kwargs: object) -> str:
        return "You should buy NVDA now."

    _seed_price_snapshots(db_session)

    with (
        patch(
            "app.services.report_search.httpx.post",
            return_value=_fake_tavily_response(
                [{"title": "Gold rallies", "url": "https://x.com/1", "content": "c", "score": 0.9}]
            ),
        ),
        patch("app.services.ticker_intel.send_ops_alert") as mock_alert,
    ):
        outcome = _run_batch(_bad_l1_llm)

    assert outcome.result["status"] == "completed"
    assert all(r["status"] == "success" for r in outcome.result["results"])
    assert mock_alert.called

    reports = db_session.execute(select(Report)).scalars().all()
    assert all(r.status == "success" for r in reports)

    # Review round 1 fix: a blocked attempt now writes a null-analysis
    # marker row (so it isn't re-attempted by every user in the batch) —
    # it just never serves the forbidden text.
    rows = (
        db_session.execute(select(TickerIntel).where(TickerIntel.identifier == "NVDA"))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].analysis is None
