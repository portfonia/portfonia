"""End-to-end UAT for Ring 1 stage A1 (issue #128), design doc §7.2 UAT-1/2/3:
Hermes/Portfonia/Docs/Ring 1-A design.md.

Unlike test_report_tasks.py (SessionLocal + generate_report fully mocked,
pure task-orchestration unit tests) and test_window_data.py (anomaly-
detection unit tests), this file runs `generate_incremental_report.run()`
against a REAL three-user holdings/price-snapshot fixture through a REAL
`db_session`, with only the LLM/Tavily boundary mocked — proving the actual
no-cross-user-leakage and shared-compute properties, not just that the
plumbing between mocks lines up.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from unittest.mock import MagicMock, patch

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.price_snapshot import PriceSnapshot
from app.models.report import Report
from app.services import window_data
from app.services.macro_detector import MacroSignals
from app.services.portfolio_calculator import Concentration, PortfolioSnapshot
from app.services.window_data import BOOTSTRAP_WATERMARK

# The real (unfrozen) clock is used throughout this file (PR #151 review):
# generate_incremental_report stamps ONE `now` for the whole batch and passes
# it into every user's generate_report call (report_tasks.py), which is what
# actually makes moves_cache hit across users — not test-side clock-freezing.
# An earlier version of this file froze report_generator.datetime.now to a
# fixed value, which made test_compute_global_moves_runs_once_for_the_whole_
# batch pass for the WRONG reason (each generate_report call independently
# landing on the same mocked instant) even before generate_incremental_report
# stamped a shared `now` itself — see window_data.MovesCache /
# detect_window_anomalies' docstring. period_start for all three users is
# BOOTSTRAP_WATERMARK (2026-06-01 16:00 ET) since none of them has a prior
# report — cold start, so it's identical regardless of the real clock too.
# Snapshot dates below are fixed in the past; period_end being "whenever this
# test actually runs" only widens the window past the last seeded snapshot,
# it does not change which snapshots fall inside it.
_BASELINE_DATE = date(2026, 6, 1)
_BASELINE_AT = BOOTSTRAP_WATERMARK


def _close(ticker: str, d: date, close: float) -> PriceSnapshot:
    return PriceSnapshot(
        ticker=ticker, market="US", session_node="close", trade_date=d, close=Decimal(str(close))
    )


def _close_at(ticker: str, d: date, close: float, captured_at: datetime) -> PriceSnapshot:
    return PriceSnapshot(
        ticker=ticker,
        market="US",
        session_node="close",
        trade_date=d,
        close=Decimal(str(close)),
        captured_at=captured_at,
    )


def _seed_price_snapshots(db_session: Session) -> None:
    """Baseline dated _BASELINE_DATE (6/1), captured exactly at
    BOOTSTRAP_WATERMARK so it's picked up as the pre-window close, not a
    window member. Series runs 6/2-6/6 (5 trading days).

    NVDA: single-day +7.5% (single_day trigger, identical for U1/U2 — both
    classify it EQUITY_US_TECH). AAPL: gradual +13.1% net over 5 days, no
    single day >= 5% (cumulative trigger) — clears U1's STOCK cap (10%) but
    NOT U2's EQUITY_US_TECH cap (25% at trading_days=5), proving per-user
    threshold judgment on the SAME globally-computed move. SGOL: single-day
    +5.6% (U3 only, PRECIOUS_METALS per_day=0.04)."""
    db_session.add_all(
        [
            _close_at("NVDA", _BASELINE_DATE, 200.0, _BASELINE_AT),
            _close("NVDA", date(2026, 6, 2), 215.0),
            _close("NVDA", date(2026, 6, 3), 215.0),
            _close("NVDA", date(2026, 6, 4), 215.0),
            _close("NVDA", date(2026, 6, 5), 215.0),
            _close("NVDA", date(2026, 6, 6), 215.0),
            _close_at("AAPL", _BASELINE_DATE, 100.0, _BASELINE_AT),
            _close("AAPL", date(2026, 6, 2), 102.5),
            _close("AAPL", date(2026, 6, 3), 105.06),
            _close("AAPL", date(2026, 6, 4), 107.69),
            _close("AAPL", date(2026, 6, 5), 110.39),
            _close("AAPL", date(2026, 6, 6), 113.14),
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


def _mock_llm(client: object, model: str, system: str, user: str, **kwargs: object) -> str:
    if kwargs.get("with_holdings"):
        return _FAKE_LLM_PASS2
    return '{"queries": []}'


def _mock_l1_llm(client: object, model: str, system: str, user: str, **kwargs: object) -> str:
    return "Nothing notable. [Speculative]"


def _run_batch(db_session: Session) -> None:
    """Mocks the LLM/Tavily boundary in report_generator.py (Pass 1/2),
    report_translation.py, AND ticker_intel.py (issue #128 A2's L1 step) —
    OUTPUT_LANG defaults to "zh" (app/core/config.py) so
    generate_incremental_report's real call to generate_report(output_lang=
    "zh") exercises the real translation pass, and every user with a
    price-anomaly candidate exercises the real L1 step; each of these
    modules imports its own `_call_llm`/`_openrouter_client` bindings
    independent of report_generator.py's — missing any one of these mocks
    makes the test hang on a real OpenRouter network call instead of
    failing fast.

    Deliberately does NOT freeze `datetime.now` (PR #151 review) — the real
    clock exercises generate_incremental_report's own `batch_now` stamp,
    which is what actually has to make moves_cache hit across users, not a
    test-controlled clock.
    """
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
        patch("app.services.report_generator._call_llm", side_effect=_mock_llm),
        patch("app.services.report_generator._run_tavily_search", return_value=[]),
        patch("app.services.report_translation._openrouter_client", return_value=MagicMock()),
        patch("app.services.report_translation._call_llm", side_effect=_mock_llm),
        patch("app.services.report_translation.time.sleep"),  # skip the real 2s/chunk pacing
        patch("app.services.ticker_intel._openrouter_client", return_value=MagicMock()),
        patch("app.services.ticker_intel._call_llm", side_effect=_mock_l1_llm),
    ):
        result = generate_incremental_report.run()

    assert result["status"] == "completed"
    assert all(r["status"] == "success" for r in result["results"])


def _anomaly_identifiers(db_session: Session, user_id: object) -> set[str]:
    report = db_session.execute(
        select(Report).where(Report.user_id == user_id, Report.report_type == "incremental")
    ).scalar_one()
    assert report.report_inputs is not None
    anomalies = report.report_inputs["price_anomalies"]
    return {a["identifier"] for a in anomalies}


def test_no_cross_user_leakage_across_the_batch(
    db_session: Session, three_user_holdings: dict[str, object]
) -> None:
    """UAT-1: three users generated in one batch; no user's anomaly list may
    contain another user's exclusively-held identifier."""
    _seed_price_snapshots(db_session)
    _run_batch(db_session)

    u1 = _anomaly_identifiers(db_session, three_user_holdings["U1"])
    u2 = _anomaly_identifiers(db_session, three_user_holdings["U2"])
    u3 = _anomaly_identifiers(db_session, three_user_holdings["U3"])

    # SGOL is seeded in ticker_themes under theme "gold" (Ring 0 seed data), so
    # it surfaces under the theme key, not the raw ticker — see
    # test_sgol_only_flags_for_the_user_who_holds_it.
    assert "gold" not in u1
    assert "gold" not in u2
    assert "NVDA" not in u3
    assert "AAPL" not in u3


def test_per_user_threshold_divergence_on_the_same_shared_identifier(
    db_session: Session, three_user_holdings: dict[str, object]
) -> None:
    """The concrete manifestation of design doc §3.3: AAPL's SAME globally-
    computed move clears U1's STOCK threshold but not U2's EQUITY_US_TECH
    threshold. NVDA (same asset_class for both) flags for both."""
    _seed_price_snapshots(db_session)
    _run_batch(db_session)

    u1 = _anomaly_identifiers(db_session, three_user_holdings["U1"])
    u2 = _anomaly_identifiers(db_session, three_user_holdings["U2"])

    assert u1 == {"NVDA", "AAPL"}
    assert u2 == {"NVDA"}


def test_sgol_only_flags_for_the_user_who_holds_it(
    db_session: Session, three_user_holdings: dict[str, object]
) -> None:
    """SGOL is seeded in ticker_themes under theme "gold" (Ring 0 seed data),
    so a flagged SGOL surfaces under that theme key — single-member merge,
    unchanged pre-A1 behavior (design doc §3.3's merge-stays-per-user note)."""
    _seed_price_snapshots(db_session)
    _run_batch(db_session)

    assert _anomaly_identifiers(db_session, three_user_holdings["U3"]) == {"gold"}


def test_compute_global_moves_runs_once_per_distinct_window_not_per_user(
    db_session: Session, three_user_holdings: dict[str, object]
) -> None:
    """UAT-2: the global move computation must not scale with user count —
    three users sharing one window trigger exactly one compute_global_moves
    call for that window, not three. Runs against the real (unfrozen) clock
    via _run_batch (PR #151 review) — this is the actual regression for the
    bug where each user's independent `datetime.now()` call defeated
    moves_cache's cache key in production even though a test that froze the
    clock stayed green.

    2 calls total, not 1 (design doc §4.8, second addendum): the batch's
    shared report window (`period_start`/`period_end`, identical for all
    three users here — cold start, same BOOTSTRAP_WATERMARK) accounts for
    one call, and L1's day-scoped window (`day_window_bounds(eff_date)`,
    deliberately a DIFFERENT window from the report's — see
    ticker_intel.build_l1_facts's docstring) accounts for the other. Both
    are still shared across all three users via the same `moves_cache`, so
    the count stays at 2 regardless of user count — the property this test
    guards (no N-user scaling) still holds, it's just bounded by the number
    of distinct windows requested per batch, not by 1."""
    _seed_price_snapshots(db_session)

    with patch(
        "app.services.window_data.compute_global_moves", wraps=window_data.compute_global_moves
    ) as spy:
        _run_batch(db_session)

    assert spy.call_count == 2
