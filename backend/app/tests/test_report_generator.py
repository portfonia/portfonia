"""Tests for report_generator orchestration (generate_report/regenerate_report).

Strategy:
- All external calls (LLM, Tavily, news_fetcher, macro_detector,
  price_anomaly_detector, email_sender) are mocked via patch.
- DB operations use the db_session fixture (real Postgres).
- Tests cover: normal path, quiet-day skip, LLM failure, Tavily failure (degraded).

Split from a single file into per-module test files (#37) — this file keeps
only tests exercising generate_report/regenerate_report/_render_full_md/
_is_short_manual_quiet end-to-end. Unit tests for the individual pieces
(prompts, code-built sections, the LLM transport, search, translation,
serializers, the compliance scan, report_inputs types) moved to
test_report_prompts.py, test_report_sections.py, test_report_llm.py,
test_report_translation.py, test_report_serializers.py, test_output_scan.py,
and test_report_context.py respectively.
"""

from __future__ import annotations

import contextlib
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.forward_event import ForwardEvent
from app.models.report import Report
from app.services import report_generator as rg
from app.services.macro_detector import MacroSignals, ThemeHit
from app.services.news_fetcher import NewsItem
from app.services.portfolio_calculator import (
    Concentration,
    HoldingValue,
    PortfolioSnapshot,
)
from app.services.price_anomaly_detector import ConstituentMove, PriceAnomaly
from app.services.report_llm import _BYOK_PROVIDER_ORDER
from app.services.window_data import HoldingMove, MovesCache
from app.tests.conftest import seed_user

_USER = uuid.UUID("00000000-0000-0000-0000-000000000099")
_NOW = datetime(2026, 6, 4, 20, 0, tzinfo=UTC)
_TODAY = date(2026, 6, 4)


@pytest.fixture(autouse=True)
def _seed_test_user(db_session: Session) -> None:
    """issue #129 B7's new FKs need a `users` row for _USER before most
    tests here write a holding/report under it. A few tests
    (test_generate_report_assembly_path_also_gets_investor_preferences,
    `_seed_investment_context`) insert their own `User(id=_USER, ...)` row
    afterward with specific fields (locale/intel_focus) — those sites now
    check-first and skip their own insert if this fixture already created
    one, rather than the two racing on the same primary key."""
    seed_user(db_session, _USER)


def _day_move(identifier: str, **overrides: object) -> HoldingMove:
    """A minimal day-scoped `HoldingMove` for mocking `resolve_global_moves`
    directly — avoids seeding real `Holding`/`PriceSnapshot` rows in tests
    that only care whether L1 got a `day_pct`, not the number's specific
    value (round 6 review fix: L1 now skips any candidate with no real
    `day_pct`, so these tests need one to exercise the L1 path at all)."""
    defaults: dict[str, object] = {
        "identifier": identifier,
        "market": "US",
        "current_price": Decimal("215"),
        "prev_price": Decimal("200"),
        "net_pct": Decimal("0.075"),
        "max_day_pct": Decimal("0.075"),
        "max_day_date": _TODAY,
        "baseline_date": _TODAY,
        "latest_date": _TODAY,
        "prev_close": Decimal("200"),
        "day_open": Decimal("205"),
        "day_high": Decimal("216"),
        "day_low": Decimal("204"),
        "day_close": Decimal("215"),
        "after_hours": None,
    }
    defaults.update(overrides)
    return HoldingMove(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Module-level guard: block real email delivery in every test in this file.
# Without this, any test that reaches step 10 of generate_report() hits the
# live Resend API and sends an actual email.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_email() -> MagicMock:  # type: ignore[misc]
    with patch("app.services.report_generator.send_report_email") as mock:
        mock.return_value = True
        yield mock


# ---------------------------------------------------------------------------
# Module-level guard: generate_report always runs the L1 shared-intel step
# (issue #128 A2, ticker_intel.get_l1_intel_batch) after anomaly detection.
# Most tests here don't exercise it (no anomalies seeded -> no candidates ->
# no LLM call), but any that do must not hit a real OpenRouter endpoint --
# ticker_intel imports its own _call_llm/_openrouter_client bindings,
# independent of report_generator.py's (same reason report_translation needs
# its own mock, see test_shared_compute_a1.py's _run_batch docstring).
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _mock_l1_llm_boundary() -> None:  # type: ignore[misc]
    with (
        patch("app.services.ticker_intel._openrouter_client", return_value=MagicMock()),
        patch(
            "app.services.ticker_intel._call_llm",
            return_value="Nothing notable. [Speculative]",
        ),
    ):
        yield


# ---------------------------------------------------------------------------
# Same guard for the L2 shared macro-event step (issue #128 A3,
# macro_event_intel.get_l2_intel_batch). Most tests here produce no L2
# candidate with global facts (the `news`/`forward_events` tables are empty),
# but macro_event_intel resolves its own _call_llm/_openrouter_client, so an
# unmocked one would reach the live endpoint.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _mock_l3_llm_boundary() -> None:  # type: ignore[misc]
    """Same guard again for the L3 day-level synthesis step (issue #128
    quality gate, cross_name_intel.get_day_synthesis).

    Worth stating why this is not optional even though most tests here produce
    fewer than two servable L1 rows and therefore never reach the call: the
    step is wrapped in a try/except so the report survives a synthesis
    failure, which means an unmocked live call would NOT fail loudly here —
    it would just quietly bill a real request and pass. A silent boundary
    escape is worse than a noisy one.
    """
    with (
        patch("app.services.cross_name_intel._openrouter_client", return_value=MagicMock()),
        patch(
            "app.services.cross_name_intel._call_llm",
            return_value='{"clusters": []}',
        ),
    ):
        yield


@pytest.fixture(autouse=True)
def _mock_l2_llm_boundary() -> None:  # type: ignore[misc]
    with (
        patch("app.services.macro_event_intel._openrouter_client", return_value=MagicMock()),
        patch(
            "app.services.macro_event_intel._call_llm",
            return_value='{"analysis": "Nothing notable. [Speculative]", '
            '"affected_asset_classes": [], "affected_sectors": []}',
        ),
    ):
        yield


# ---------------------------------------------------------------------------
# Fixtures / factories
# ---------------------------------------------------------------------------


def _news_item(title: str) -> NewsItem:
    from app.services.news_fetcher import _url_hash

    url = f"https://example.com/{title.replace(' ', '-').lower()}"
    return NewsItem(
        url_hash=_url_hash(url),
        title=title,
        url=url,
        source="TEST",
        published_at=_NOW,
        summary=f"Summary of {title}",
    )


def _macro_hit() -> MacroSignals:
    item = _news_item("Fed raises rates")
    hit = ThemeHit(
        theme="货币政策",
        keywords_found=["Fed"],
        articles=[item],
    )
    return MacroSignals(hits=[hit], has_any_hit=True, total_matched_articles=1)


def _quiet_signals() -> MacroSignals:
    return MacroSignals(hits=[], has_any_hit=False, total_matched_articles=0)


def _anomaly() -> PriceAnomaly:
    return PriceAnomaly(
        name="NVIDIA",
        identifier="NVDA",
        asset_type="stock",
        current_price=Decimal("120.0"),
        prev_price=Decimal("110.0"),
        pct_change=Decimal("0.0909"),
        threshold=Decimal("0.03"),
    )


def _portfolio_snap() -> PortfolioSnapshot:
    hv = HoldingValue(
        holding_id=uuid.uuid4(),
        name="Apple Inc.",
        ticker="AAPL",
        fund_code=None,
        currency="USD",
        asset_type="stock",
        asset_class="STOCK",
        sector="Technology",
        market="US",
        market_value=Decimal("10000"),
        market_value_base=Decimal("10000"),
        price_as_of=_NOW,
    )
    return PortfolioSnapshot(
        base_currency="USD",
        fx_date=_TODAY,
        holdings=[hv],
        total_base=Decimal("10000"),
        by_currency={"USD": Decimal("10000")},
        by_asset_type={"stock": Decimal("10000")},
        by_market={"US": Decimal("10000")},
        by_sector={"Technology": Decimal("10000")},
        by_asset_class={"STOCK": Decimal("10000")},
        concentration=Concentration(
            top_holding_name="Apple Inc.",
            top_holding_ratio=Decimal("1.0"),
            top_holding_asset_class="STOCK",
            top3_ratio=Decimal("1.0"),
            top_asset_class_name="STOCK",
            top_asset_class_ratio=Decimal("1.0"),
            single_holding_watch=True,
            single_holding_high=True,
            top3_watch=True,
            asset_class_watch=True,
            asset_class_high=True,
        ),
        stale_tickers=[],
    )


# Padding so fake Pass 2 bodies clear _PASS2_MIN_CHARS (H-DEBT-2 completeness
# guard) — real Pass 2 output runs several thousand chars across §2/§3/§4.
_PASS2_FILLER = "Filler context. " * 130

_FAKE_LLM_PASS1 = (
    '{"queries": ["Federal Reserve rate decision impact", "NVIDIA earnings semiconductor"]}'
)
_FAKE_LLM_PASS2 = (
    "## §2 Macro Signals\n\nFed raised rates. [For information only — not investment advice]\n\n"
    "## §3 Holdings Intelligence\n\nNVIDIA up 9%. [For information only — not investment advice]\n\n"
    "## §4 Risk Radar\n\nConcentration watch. [For information only — not investment advice]\n\n"
    + _PASS2_FILLER
)

# F2-specific fake: includes [S#] citations and AAPL references to exercise annotations.
_FAKE_LLM_PASS2_F2 = (
    "## §2 Macro Signals\n\n"
    "Fed raised rates significantly according to recent reports [S1]. "
    "[For information only — not investment advice]\n\n"
    "## §3 Holdings Intelligence\n\n"
    "AAPL represents a large portion of the portfolio and is sensitive to rate changes. "
    "[For information only — not investment advice]\n\n"
    "## §4 Risk Radar\n\n"
    "Concentration above thresholds. [For information only — not investment advice]\n\n"
    + _PASS2_FILLER
)

_FAKE_TAVILY_RESULTS = [
    {
        "query": "Federal Reserve rate decision impact",
        "title": "Fed holds rates",
        "url": "https://reuters.com/fed",
        "content": "The Federal Reserve kept rates unchanged...",
        "score": 0.9,
        "index": 1,
    }
]


def _mock_llm(
    client: object,
    model: str,
    system: str,
    user: str,
    *,
    with_holdings: bool = False,
    **kwargs: object,
) -> str:
    if with_holdings:
        return _FAKE_LLM_PASS2
    return _FAKE_LLM_PASS1


# ---------------------------------------------------------------------------
# Tests: normal path
# ---------------------------------------------------------------------------


def test_generate_report_normal_path(db_session: Session) -> None:
    """Full pipeline: macro hit + anomaly → Pass1 → Tavily → Pass2 → DB write."""
    with (
        patch("app.services.report_generator.compute_portfolio", return_value=_portfolio_snap()),
        patch(
            "app.services.report_generator.load_news_window",
            return_value=[_news_item("Fed raises rates")],
        ),
        patch("app.services.report_generator.detect_macro_signals", return_value=_macro_hit()),
        patch(
            "app.services.report_generator.detect_window_anomalies", return_value=([_anomaly()], 2)
        ),
        patch("app.services.report_generator._openrouter_client", return_value=MagicMock()),
        patch("app.services.report_generator._call_llm", side_effect=_mock_llm),
        patch(
            "app.services.report_generator._run_tavily_search", return_value=_FAKE_TAVILY_RESULTS
        ),
    ):
        report = rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)

    assert report.status == "success"
    assert report.report_date == _TODAY
    assert report.report_type == "incremental"
    assert report.report_md is not None
    assert "§1 Portfolio Snapshot" in report.report_md
    assert "§2 Macro Signals" in report.report_md
    assert "§3 Holdings Intelligence" in report.report_md
    assert "§4 Risk Radar" in report.report_md
    assert report.generated_at is not None
    assert report.report_inputs is not None
    assert report.report_inputs["pass2_model"] != ""
    assert len(report.report_inputs["search_queries"]) == 2
    # 1 macro-themed result + 1 from the R-3 targeted anomaly search: the anomaly
    # holding has no recalled window news, so a targeted search runs and the mock
    # returns its (single) result a second time.
    assert len(report.report_inputs["search_results"]) == 2


def test_generate_report_empty_book_content_contract(db_session: Session) -> None:
    """issue #221 §2.7 (Ring 1-Onboarding.md): a user with no
    user_investment_context row and no holdings still gets a completed
    report — this is a content contract on the existing empty-list code
    path, not a new pipeline. §1 renders its headers over an empty table
    (no crash on division by a zero total). §2.5 still lists a
    holdings-independent scheduled event (FOMC/CPI-style, ticker="") with
    Exposed holdings rendered as "—" rather than omitted or crashing.
    No UserInvestmentContext row is seeded — Pass 2 falls back to the B1
    system default framework, which is already the existing behavior."""
    empty_portfolio = PortfolioSnapshot(base_currency="USD", fx_date=_TODAY)
    db_session.add(
        ForwardEvent(
            event_type="macro",
            name="FOMC Meeting",
            ticker="",
            scheduled_date=_TODAY + timedelta(days=1),
            source="fomc",
        )
    )
    db_session.flush()

    with (
        patch("app.services.report_generator.compute_portfolio", return_value=empty_portfolio),
        patch(
            "app.services.report_generator.load_news_window",
            return_value=[_news_item("Fed raises rates")],
        ),
        patch("app.services.report_generator.detect_macro_signals", return_value=_macro_hit()),
        patch("app.services.report_generator.detect_window_anomalies", return_value=([], 0)),
        patch("app.services.report_generator._openrouter_client", return_value=MagicMock()),
        patch("app.services.report_generator._call_llm", side_effect=_mock_llm),
        patch("app.services.report_generator._run_tavily_search", return_value=[]),
    ):
        report = rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)

    assert report.status == "success"
    assert report.report_md is not None
    assert "§1 Portfolio Snapshot" in report.report_md
    assert "USD 0" in report.report_md  # zero total, no ZeroDivisionError
    assert "§2.5 Forward Calendar" in report.report_md
    assert "FOMC Meeting" in report.report_md
    # No holding to expose it to -> "—", not omitted or crashed.
    assert "| FOMC Meeting | —" in report.report_md


def test_targeted_search_budget_uses_real_api_calls_not_result_item_count(
    db_session: Session,
) -> None:
    """Review round 1 bug: the targeted-search gate used to compute
    `daily_remaining - len(ctx.search_results)` — subtracting a RESULT-ITEM
    count (up to 5/query) from an HTTP-CALL budget. With TAVILY_DAILY_BUDGET
    at its default (10) and Pass 1 proposing 2 queries that each return 5
    items, that arithmetic reached 0 (10 - 10) and skipped the targeted NVDA
    search entirely — even though only 2 real HTTP calls (of the 10 allowed)
    had actually been made. This exercises the real report_search.py cache
    path (only httpx.post is mocked), unlike the other tests in this file
    which mock `_run_tavily_search` itself."""
    five_items = [
        {"title": f"headline {i}", "url": f"https://x.com/{i}", "content": "c", "score": 0.5}
        for i in range(5)
    ]

    def _fake_response(*args: object, **kwargs: object) -> MagicMock:
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"results": five_items}
        return resp

    with (
        patch("app.services.report_generator.compute_portfolio", return_value=_portfolio_snap()),
        patch(
            "app.services.report_generator.load_news_window",
            return_value=[_news_item("Fed raises rates")],
        ),
        patch("app.services.report_generator.detect_macro_signals", return_value=_macro_hit()),
        patch(
            "app.services.report_generator.detect_window_anomalies", return_value=([_anomaly()], 2)
        ),
        patch("app.services.report_generator._openrouter_client", return_value=MagicMock()),
        patch("app.services.report_generator._call_llm", side_effect=_mock_llm),
        patch("app.services.report_search.httpx.post", side_effect=_fake_response) as mock_post,
    ):
        report = rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)

    assert report.status == "success"
    real_queries = [c.kwargs["json"]["query"] for c in mock_post.call_args_list]
    assert any("NVDA" in q for q in real_queries)


def test_large_weight_holding_without_anomaly_gets_material(db_session: Session) -> None:
    """Narrative-layer redesign (issue #128, 2026-08-20): the 2026-08-17
    anchor report's TSM line (22.5% of the portfolio, +1.22% on the day —
    below its own asset-class anomaly threshold) got ZERO recalled news and
    ZERO targeted search in Pass 2's own inputs, because both only ever
    looked at `ctx.price_anomalies` — a holding that never crosses its
    threshold was invisible to material-gathering no matter how large its
    weight. Pass 2 wrote it from prior knowledge alone; the redesign doc's
    diagnosis is that this — not the writing model or its instructions — is
    the actual root cause (design doc, "narrative-layer redesign — quality
    gate reversal", the TSM worked example pinning down the gap).

    `_portfolio_snap()`'s single AAPL holding is 100% of the portfolio,
    which reproduces the "large weight, no anomaly" shape directly: with
    `detect_window_anomalies` returning an empty anomaly list and no window
    news to recall, AAPL must still trigger a targeted search whose result
    lands in `ctx.search_results` (persisted as
    `report_inputs["search_results"]`) — the same BACKGROUND RESEARCH slot
    the existing anomaly-targeted search already uses — so Pass 2's own
    prompt carries the material, not just L1's shared cache.
    """
    tavily_calls: list[list[str]] = []

    def _capture_tavily(
        session: Session, queries: list[str], eff_date: date, *, budget: int, **_kwargs: object
    ) -> list[dict[str, Any]]:
        tavily_calls.append(list(queries))
        return [
            {
                "query": q,
                "title": f"headline for {q}",
                "url": "https://example.com/x",
                "content": "c",
                "score": 0.5,
            }
            for q in queries
        ]

    with (
        patch("app.services.report_generator.compute_portfolio", return_value=_portfolio_snap()),
        patch("app.services.report_generator.load_news_window", return_value=[]),
        patch("app.services.report_generator.detect_macro_signals", return_value=_macro_hit()),
        patch("app.services.report_generator.detect_window_anomalies", return_value=([], 2)),
        patch("app.services.report_generator._openrouter_client", return_value=MagicMock()),
        patch("app.services.report_generator._call_llm", side_effect=_mock_llm),
        patch("app.services.report_generator._run_tavily_search", side_effect=_capture_tavily),
    ):
        report = rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)

    assert report.status == "success"
    all_queries = [q for batch in tavily_calls for q in batch]
    assert any("AAPL" in q for q in all_queries), (
        f"expected a targeted search query naming the large no-anomaly "
        f"holding AAPL among {all_queries}"
    )
    assert report.report_inputs is not None
    aapl_results = [
        r for r in report.report_inputs["search_results"] if "AAPL" in r.get("title", "")
    ]
    assert aapl_results, (
        f"expected an AAPL-titled result in search_results: "
        f"{report.report_inputs['search_results']}"
    )


def test_large_weight_holding_window_price_reaches_pass2_prompt(db_session: Session) -> None:
    """Design amendment item 3 (issue #128, 2026-08-20, "make Pass 2 write the
    connection again, not just name it"), extended by the second design
    amendment's item 3 (net and max-day fed as two separate facts): a large
    holding below the anomaly threshold previously had NO price fact anywhere
    in Pass 2's prompt at all — not even its own unremarkable window move —
    because PRICE ANOMALIES only lists holdings that crossed threshold.
    `resolve_global_moves` is mocked directly (the real captured-close store
    has no seeded price_snapshots for this fixture's synthetic AAPL holding);
    everything else is the same shape as
    `test_large_weight_holding_without_anomaly_gets_material`."""
    aapl_move = HoldingMove(
        identifier="AAPL",
        market="US",
        current_price=Decimal("101.22"),
        prev_price=Decimal("100.0"),
        net_pct=Decimal("0.0011"),
        max_day_pct=Decimal("0.0122"),
        max_day_date=_TODAY,
        baseline_date=_TODAY,
        latest_date=_TODAY,
        prev_close=None,
        day_open=None,
        day_high=None,
        day_low=None,
        day_close=None,
        after_hours=None,
    )
    captured: dict[str, str] = {}

    def _capture_pass2_llm(
        client: object,
        model: str,
        system: str,
        user: str,
        *,
        with_holdings: bool = False,
        **kwargs: object,
    ) -> str:
        if with_holdings:
            captured["pass2_user"] = user
            return _FAKE_LLM_PASS2
        return _FAKE_LLM_PASS1

    with (
        patch("app.services.report_generator.compute_portfolio", return_value=_portfolio_snap()),
        patch("app.services.report_generator.load_news_window", return_value=[]),
        patch("app.services.report_generator.detect_macro_signals", return_value=_macro_hit()),
        patch("app.services.report_generator.detect_window_anomalies", return_value=([], 2)),
        patch(
            "app.services.report_generator.resolve_global_moves",
            return_value=({"AAPL": aapl_move}, 2),
        ),
        patch("app.services.report_generator._openrouter_client", return_value=MagicMock()),
        patch("app.services.report_generator._call_llm", side_effect=_capture_pass2_llm),
        patch("app.services.report_generator._run_tavily_search", return_value=[]),
    ):
        report = rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)

    assert report.status == "success"
    assert report.report_inputs is not None
    stored = report.report_inputs["large_holding_moves"]
    assert stored["AAPL"]["net_pct"] == pytest.approx(0.0011)
    assert stored["AAPL"]["max_day_pct"] == pytest.approx(0.0122)
    assert stored["AAPL"]["max_day_date"] == _TODAY.isoformat()
    assert "pass2_user" in captured
    assert "LARGE HOLDINGS WINDOW PRICE" in captured["pass2_user"]
    assert "AAPL: +0.11% net this report period" in captured["pass2_user"]
    assert f"largest single day +1.22% on {_TODAY.isoformat()}" in captured["pass2_user"]


def test_weight_targeted_search_promotes_title_matches_first(db_session: Session) -> None:
    """Design amendment item 1 (2026-08-20): a targeted-search result whose
    title actually names the identifier must be ranked ahead of a result that
    only matched Tavily's own relevance score — the v5 compare's TSM query
    returned a mostly-generic result set, and burying the one on-target title
    among four off-target ones wastes the model's attention on exactly the
    holding this fix targets."""

    def _fake_tavily(
        session: Session, queries: list[str], eff_date: date, *, budget: int, **_kwargs: object
    ) -> list[dict[str, Any]]:
        # Only the weight-targeted AAPL query returns results — Pass 1's own
        # (differently worded) queries return nothing, so the two calls don't
        # bleed into each other in report_inputs["search_results"]. The real
        # `_run_tavily_search` always stamps each result's "query" with the
        # caller's own (date-qualified) query string — mirrored here since a
        # mismatched "query" field would make the identifier lookup miss.
        matches = [q for q in queries if "AAPL stock news catalyst" in q]
        if not matches:
            return []
        query = matches[0]
        # Off-target result first, exactly as Tavily's own relevance score
        # would rank it here (0.9 > 0.4) — the fix must still put the title
        # match ahead of it.
        return [
            {
                "query": query,
                "title": "Broad market roundup: tech stocks mixed",
                "url": "https://example.com/roundup",
                "content": "c",
                "score": 0.9,
            },
            {
                "query": query,
                "title": "Apple (AAPL) reports strong iPhone demand",
                "url": "https://example.com/aapl",
                "content": "c",
                "score": 0.4,
            },
        ]

    with (
        patch("app.services.report_generator.compute_portfolio", return_value=_portfolio_snap()),
        patch("app.services.report_generator.load_news_window", return_value=[]),
        patch("app.services.report_generator.detect_macro_signals", return_value=_macro_hit()),
        patch("app.services.report_generator.detect_window_anomalies", return_value=([], 2)),
        patch("app.services.report_generator._openrouter_client", return_value=MagicMock()),
        patch("app.services.report_generator._call_llm", side_effect=_mock_llm),
        patch("app.services.report_generator._run_tavily_search", side_effect=_fake_tavily),
    ):
        report = rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)

    assert report.status == "success"
    assert report.report_inputs is not None
    titles = [r["title"] for r in report.report_inputs["search_results"] if r.get("title")]
    aapl_titles = [t for t in titles if "iPhone" in t or "roundup" in t]
    assert aapl_titles == [
        "Apple (AAPL) reports strong iPhone demand",
        "Broad market roundup: tech stocks mixed",
    ]


def test_generate_report_pass2_call_excludes_l1_ticker_intel_text(db_session: Session) -> None:
    """Round 2 review finding: `ctx.ticker_intel` is populated and persisted
    on report_inputs, but nothing enforces that Pass 2 never receives it —
    isolation held only because `_build_pass2_prompt` happens not to be
    wired to it. Locks the contract with a red test, parallel to
    test_generate_report_pass1_call_has_no_holdings, so a future "wire L1
    into Pass 2" edit can't silently ship holdings-derived L1 prose into the
    per-user Pass 2 call without breaking a test."""
    _L1_MARKER = "ZZZ_L1_SHARED_ANALYSIS_MARKER_ZZZ"
    captured: dict[str, str] = {}

    def _capture_pass2_llm(
        client: object,
        model: str,
        system: str,
        user: str,
        *,
        with_holdings: bool = False,
        **kwargs: object,
    ) -> str:
        if with_holdings:
            captured["pass2_user"] = user
            return _FAKE_LLM_PASS2
        return _FAKE_LLM_PASS1

    def _mock_l1_llm(*args: object, **kwargs: object) -> str:
        return _L1_MARKER

    with (
        patch("app.services.report_generator.compute_portfolio", return_value=_portfolio_snap()),
        patch(
            "app.services.report_generator.load_news_window",
            return_value=[_news_item("Fed raises rates")],
        ),
        # `load_day_news` is L1's OWN, separate news source (design doc §4.8,
        # second addendum) — it queries the real `news` table directly, not
        # `load_news_window`'s (mocked, per-user) return value, so it needs
        # its own mock.
        patch(
            "app.services.report_generator.load_day_news",
            return_value=[_news_item("Nvidia beats earnings")],
        ),
        # `resolve_global_moves` mocked too (round 6 review fix): a headline
        # alone is no longer enough for L1 to analyze/cache an identifier —
        # a candidate needs a real `day_pct`, which this test has no reason
        # to seed real Holding/PriceSnapshot rows for.
        patch(
            "app.services.report_generator.resolve_global_moves",
            return_value=({"NVDA": _day_move("NVDA")}, 1),
        ),
        patch("app.services.report_generator.detect_macro_signals", return_value=_macro_hit()),
        patch(
            "app.services.report_generator.detect_window_anomalies", return_value=([_anomaly()], 2)
        ),
        patch("app.services.report_generator._openrouter_client", return_value=MagicMock()),
        patch("app.services.report_generator._call_llm", side_effect=_capture_pass2_llm),
        patch("app.services.report_generator._run_tavily_search", return_value=[]),
        patch("app.services.ticker_intel._openrouter_client", return_value=MagicMock()),
        patch("app.services.ticker_intel._call_llm", side_effect=_mock_l1_llm),
    ):
        report = rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)

    assert report.status == "success"
    assert report.report_inputs is not None
    assert report.report_inputs["ticker_intel"].get("NVDA") == _L1_MARKER  # sanity: L1 DID run
    assert "pass2_user" in captured
    assert _L1_MARKER not in captured["pass2_user"]


_L2_MARKER = "ZZZ_L2_SHARED_EVENT_MARKER_ZZZ"


def _mock_l2_llm(*args: object, **kwargs: object) -> str:
    import json

    return json.dumps(
        {
            "analysis": f"{_L2_MARKER} rate policy datapoint. [Established]",
            "affected_asset_classes": ["STOCK", "CRYPTO"],
            "affected_sectors": ["Financials"],
        }
    )


def _l2_patches(
    llm: object = _mock_l2_llm, day_title: str = "Fed holds rates steady"
) -> tuple[Any, Any, Any]:
    """L2 resolves its own bindings (its own `_call_llm`, its own
    `load_day_news`) — patching report_generator's does not reach it, the
    same independence L1/report_translation already have."""
    return (
        patch("app.services.macro_event_intel._openrouter_client", return_value=MagicMock()),
        patch("app.services.macro_event_intel._call_llm", side_effect=llm),
        patch(
            "app.services.macro_event_intel.load_day_news",
            return_value=[_news_item(day_title)],
        ),
    )


def test_generate_report_pass2_call_excludes_l2_macro_event_intel_text(
    db_session: Session,
) -> None:
    """Same contract A2 locked for L1 (issue #128 A3, design doc §1.2):
    `ctx.macro_event_intel` is populated and persisted, but A3 is
    cache-infrastructure only — report content stays byte-identical until A4
    assembles it, so nothing L2 produced may reach the per-user Pass 2 call."""
    captured: dict[str, str] = {}

    def _capture_pass2_llm(
        client: object,
        model: str,
        system: str,
        user: str,
        *,
        with_holdings: bool = False,
        **kwargs: object,
    ) -> str:
        if with_holdings:
            captured["pass2_user"] = user
            return _FAKE_LLM_PASS2
        return _FAKE_LLM_PASS1

    l2_client, l2_call, l2_news = _l2_patches()
    with (
        patch("app.services.report_generator.compute_portfolio", return_value=_portfolio_snap()),
        patch(
            "app.services.report_generator.load_news_window",
            return_value=[_news_item("Fed raises rates")],
        ),
        patch(
            "app.services.report_generator.load_day_news",
            return_value=[_news_item("Nvidia beats earnings")],
        ),
        patch(
            "app.services.report_generator.resolve_global_moves",
            return_value=({"NVDA": _day_move("NVDA")}, 1),
        ),
        patch("app.services.report_generator.detect_macro_signals", return_value=_macro_hit()),
        patch(
            "app.services.report_generator.detect_window_anomalies", return_value=([_anomaly()], 2)
        ),
        patch("app.services.report_generator._openrouter_client", return_value=MagicMock()),
        patch("app.services.report_generator._call_llm", side_effect=_capture_pass2_llm),
        patch("app.services.report_generator._run_tavily_search", return_value=[]),
        l2_client,
        l2_call,
        l2_news,
    ):
        report = rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)

    assert report.status == "success"
    assert report.report_inputs is not None
    intel = report.report_inputs["macro_event_intel"]
    # Sanity: L2 DID run for the theme the user's own signals selected.
    assert _L2_MARKER in intel["theme:货币政策"]["analysis"]
    # Out-of-taxonomy "CRYPTO" was dropped before it could be stored.
    assert intel["theme:货币政策"]["affected_asset_classes"] == ["STOCK"]
    # The per-user half: STOCK is the only class this portfolio holds.
    assert report.report_inputs["macro_event_exposure"] == {"theme:货币政策": ["STOCK"]}
    assert "pass2_user" in captured
    assert _L2_MARKER not in captured["pass2_user"]
    # Round-1 review nit (blacktomb42, PR #157): the Pass 2 prompt is only
    # the INPUT side of "A3 changes no report content". Assert the rendered
    # body too — a future edit could inject L2 text at assembly time
    # (_render_full_md) without ever touching the prompt.
    assert report.report_md is not None
    assert _L2_MARKER not in report.report_md


def test_generate_report_l2_prompt_uses_day_news_not_the_users_window(
    db_session: Session,
) -> None:
    """Design doc §4.8's second principle, applied to L2: the shared row must
    describe the trading day, not the calling user's report window. The user's
    own window headline ("Fed raises rates", from `load_news_window`) must not
    reach a row every other user will read; the day's global headline must."""
    captured: dict[str, str] = {}

    def _capture_l2_llm(
        client: object, model: str, system: str, user: str, **kwargs: object
    ) -> str:
        captured["l2_user"] = user
        return _mock_l2_llm()

    l2_client, l2_call, l2_news = _l2_patches(llm=_capture_l2_llm)
    with (
        patch("app.services.report_generator.compute_portfolio", return_value=_portfolio_snap()),
        patch(
            "app.services.report_generator.load_news_window",
            return_value=[_news_item("Fed raises rates")],
        ),
        patch(
            "app.services.report_generator.load_day_news",
            return_value=[_news_item("Nvidia beats earnings")],
        ),
        patch(
            "app.services.report_generator.resolve_global_moves",
            return_value=({"NVDA": _day_move("NVDA")}, 1),
        ),
        patch("app.services.report_generator.detect_macro_signals", return_value=_macro_hit()),
        patch(
            "app.services.report_generator.detect_window_anomalies", return_value=([_anomaly()], 2)
        ),
        patch("app.services.report_generator._openrouter_client", return_value=MagicMock()),
        patch("app.services.report_generator._call_llm", side_effect=_mock_llm),
        patch("app.services.report_generator._run_tavily_search", return_value=[]),
        l2_client,
        l2_call,
        l2_news,
    ):
        report = rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)

    assert report.status == "success"
    assert "l2_user" in captured
    assert "Fed holds rates steady" in captured["l2_user"]
    assert "Fed raises rates" not in captured["l2_user"]


def test_generate_report_l1_sees_targeted_search_headline_pass2_input_unchanged(
    db_session: Session,
) -> None:
    """Round 3 review finding: NVDA (the seeded anomaly) has no recalled
    window news, so §5's targeted-search gap-fill fires for it — but the
    targeted result used to be appended only to `ctx.search_results` (Pass
    2's input), never merged into `ctx.holding_news` (what
    `build_l1_candidates` reads for `news_headlines`). The identifier that
    most needed a catalyst got an empty L1 headline list even when the
    targeted search found one. Fix must reach L1 WITHOUT mutating
    `ctx.holding_news`/`report_inputs["holding_news"]` itself — that's Pass
    2's input too, and A2's report content must stay byte-identical."""
    _TARGETED_TITLE = "NVIDIA announces new datacenter chip"
    captured_l1_prompt: dict[str, str] = {}

    def _capture_l1_llm(
        client: object, model: str, system: str, user: str, **kwargs: object
    ) -> str:
        captured_l1_prompt["prompt"] = user
        return "NVDA moved on the new chip announcement. [Established]"

    def _fake_response(*args: object, **kwargs: object) -> MagicMock:
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {
            "results": [
                {
                    "title": _TARGETED_TITLE,
                    "url": "https://reuters.com/nvda-chip",
                    "content": "c",
                    "score": 0.9,
                }
            ]
        }
        return resp

    with (
        patch("app.services.report_generator.compute_portfolio", return_value=_portfolio_snap()),
        patch(
            "app.services.report_generator.load_news_window",
            return_value=[_news_item("Fed raises rates")],
        ),
        patch("app.services.report_generator.detect_macro_signals", return_value=_macro_hit()),
        patch(
            "app.services.report_generator.detect_window_anomalies", return_value=([_anomaly()], 2)
        ),
        # round 6 review fix: L1 needs a real `day_pct` to analyze/cache an
        # identifier at all now, not just a headline — see _day_move's docstring.
        patch(
            "app.services.report_generator.resolve_global_moves",
            return_value=({"NVDA": _day_move("NVDA")}, 1),
        ),
        patch("app.services.report_generator._openrouter_client", return_value=MagicMock()),
        patch("app.services.report_generator._call_llm", side_effect=_mock_llm),
        patch("app.services.report_search.httpx.post", side_effect=_fake_response),
        patch("app.services.ticker_intel._openrouter_client", return_value=MagicMock()),
        patch("app.services.ticker_intel._call_llm", side_effect=_capture_l1_llm),
    ):
        report = rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)

    assert report.status == "success"
    assert report.report_inputs is not None
    # L1's prompt DID receive the targeted-search headline.
    assert _TARGETED_TITLE in captured_l1_prompt.get("prompt", "")
    # Pass 2's stored input is untouched — report content stays byte-identical.
    assert report.report_inputs["holding_news"].get("NVDA", []) == []


def test_weight_targeted_search_stays_out_of_shared_l1_cache(db_session: Session) -> None:
    """PR #168 review round 1 bug: `_targeted_weight_queries`' results are
    date-locked to THIS user's own `period_start`/`period_end` (a per-user
    watermark) — unlike `_targeted_anomaly_queries`' results, which carry no
    date qualifier and are safe for L1's day-scoped, cross-user shared cache.
    The merge at `targeted = targeted + weight_targeted` let both flow through
    the same `l1_targeted_titles` collection undifferentiated, so a weight-
    targeted title reached `ticker_intel` (the shared L1 cache) exactly like
    an anomaly-targeted one does in
    test_generate_report_l1_sees_targeted_search_headline_pass2_input_unchanged
    above — reintroducing the "whoever's report reaches an identifier first
    freezes their own per-user data into the shared row for everyone else
    that day" leak the L1 redesign (design doc §4.8) already closed once, this
    time through a news-title channel instead of a price one.

    AAPL (100% weight, `_portfolio_snap()`) has no anomaly and no recalled
    window news, so `_targeted_weight_queries` is the only source of a
    targeted search here. The result must still land in Pass 2's own
    `ctx.search_results` (unchanged, existing contract) but must NOT reach
    L1's prompt."""
    _WEIGHT_TITLE = "Apple unveils new services push"
    captured_l1_prompt: dict[str, str] = {}

    def _capture_l1_llm(
        client: object, model: str, system: str, user: str, **kwargs: object
    ) -> str:
        captured_l1_prompt["prompt"] = user
        return "AAPL held steady on services growth. [Established]"

    def _fake_tavily(
        session: Session, queries: list[str], eff_date: date, *, budget: int, **_kwargs: object
    ) -> list[dict[str, Any]]:
        return [
            {
                "query": q,
                "title": _WEIGHT_TITLE,
                "url": "https://example.com/aapl-services",
                "content": "c",
                "score": 0.5,
            }
            for q in queries
        ]

    with (
        patch("app.services.report_generator.compute_portfolio", return_value=_portfolio_snap()),
        patch("app.services.report_generator.load_news_window", return_value=[]),
        patch("app.services.report_generator.detect_macro_signals", return_value=_macro_hit()),
        patch("app.services.report_generator.detect_window_anomalies", return_value=([], 2)),
        patch(
            "app.services.report_generator.resolve_global_moves",
            # Net move kept BELOW `_L1_SEARCH_MIN_MOVE` (0.03) on purpose: at
            # 0.075 (the `_day_move` default) AAPL would also qualify for the
            # unrelated "leftover-budget top-up" search (an un-dated query
            # that legitimately IS meant to reach L1) further down in
            # generate_report, which would make this test pass for the wrong
            # reason — the top-up path returning the same mocked title,
            # not the weight-targeted-query filter under test.
            return_value=(
                {"AAPL": _day_move("AAPL", net_pct=Decimal("0.01"), max_day_pct=Decimal("0.01"))},
                2,
            ),
        ),
        patch("app.services.report_generator._openrouter_client", return_value=MagicMock()),
        patch("app.services.report_generator._call_llm", side_effect=_mock_llm),
        patch("app.services.report_generator._run_tavily_search", side_effect=_fake_tavily),
        patch("app.services.ticker_intel._openrouter_client", return_value=MagicMock()),
        patch("app.services.ticker_intel._call_llm", side_effect=_capture_l1_llm),
    ):
        report = rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)

    assert report.status == "success"
    assert report.report_inputs is not None
    # Existing contract, unchanged: Pass 2's own material-gathering still
    # gets the weight-targeted title in ctx.search_results.
    aapl_results = [
        r for r in report.report_inputs["search_results"] if r.get("title") == _WEIGHT_TITLE
    ]
    assert aapl_results, (
        f"expected the weight-targeted AAPL title in search_results: "
        f"{report.report_inputs['search_results']}"
    )
    # The bug: the same title must NOT have reached L1's shared-cache prompt.
    assert _WEIGHT_TITLE not in captured_l1_prompt.get("prompt", "")


def test_weight_targeted_search_passes_real_date_window_to_tavily_api(
    db_session: Session,
) -> None:
    """PR #168 round 2 review, suggestion: the weight-targeted query's date
    lock was query-STRING text only — `_run_tavily_search` was never told
    Tavily's own `start_date`/`end_date` API filter. Locks that
    `generate_report` actually builds and passes `date_windows` for the
    weight-targeted query (report_search.py's own unit tests cover the
    plumbing itself; this is the wiring-level lock, same pairing as
    test_weight_targeted_search_stays_out_of_shared_l1_cache above)."""
    captured: dict[str, Any] = {}

    def _capture_tavily(
        session: Session, queries: list[str], eff_date: date, *, budget: int, **kwargs: object
    ) -> list[dict[str, Any]]:
        captured["date_windows"] = kwargs.get("date_windows")
        return []

    with (
        patch("app.services.report_generator.compute_portfolio", return_value=_portfolio_snap()),
        patch("app.services.report_generator.load_news_window", return_value=[]),
        patch("app.services.report_generator.detect_macro_signals", return_value=_macro_hit()),
        patch("app.services.report_generator.detect_window_anomalies", return_value=([], 2)),
        patch(
            "app.services.report_generator.resolve_global_moves",
            return_value=({"AAPL": _day_move("AAPL", net_pct=Decimal("0.01"))}, 2),
        ),
        patch("app.services.report_generator._openrouter_client", return_value=MagicMock()),
        patch("app.services.report_generator._call_llm", side_effect=_mock_llm),
        patch("app.services.report_generator._run_tavily_search", side_effect=_capture_tavily),
    ):
        report = rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)

    assert report.status == "success"
    date_windows = captured.get("date_windows")
    assert date_windows, f"expected a non-empty date_windows map, got {date_windows}"
    aapl_queries = [q for q in date_windows if "AAPL stock news catalyst" in q]
    assert aapl_queries, f"expected an AAPL weight-targeted query key, got {list(date_windows)}"
    start, end = date_windows[aapl_queries[0]]
    assert isinstance(start, date) and isinstance(end, date)
    assert start <= end


def test_generate_report_theme_anomaly_l1_keys_constituents_with_own_recall(
    db_session: Session,
) -> None:
    """Wiring-level lock for the round 3/4 bug family, at the level the
    §4.8 redesign actually fixed it.

    Pass 2's `ctx.holding_news` is keyed by the theme SLUG for a merged
    anomaly ("gold"), because that is what §5's recall is asked for and
    what Pass 2 renders. L1 must NOT re-key that map into its own
    constituent vocabulary (the round-4 fix sprayed the theme's headlines
    onto every constituent, then needed a `theme_slugs` guard to stop the
    slug sneaking back in as its own candidate through the news-only
    loop). It now runs its OWN `recall_holding_news` over its OWN
    identifiers, so:

      - the theme slug never appears in the shared cache (it has no
        `price_snapshots` row, so no global move, and A4 could never look
        it up by a real ticker);
      - constituents get their news through the DESIGNED mechanism — the
        `SGOL -> "gold"/"bullion"` alias already in
        config/holding_news_keywords.yml — not through spraying.
    """
    _GOLD_TITLE = "Gold rallies on safe-haven demand"
    l1_prompts: dict[str, str] = {}

    def _capture_l1_llm(
        client: object, model: str, system: str, user: str, **kwargs: object
    ) -> str:
        l1_prompts[user.splitlines()[0]] = user
        return "Bullion advanced over the window. [Probable]"

    theme_anomaly = PriceAnomaly(
        name="黄金",
        identifier="gold",
        asset_type="COMMODITY",
        current_price=Decimal("54.0"),
        prev_price=Decimal("50.0"),
        pct_change=Decimal("0.0512"),  # value-weighted by THIS user's mix
        threshold=Decimal("0.03"),
        theme="gold",
        constituents=[
            ConstituentMove(
                name="SPDR Gold",
                identifier="SGOL",
                pct_change=Decimal("0.08"),
                current_value=Decimal("9000"),
            ),
        ],
    )

    with (
        patch("app.services.report_generator.compute_portfolio", return_value=_portfolio_snap()),
        patch(
            "app.services.report_generator.load_news_window",
            return_value=[_news_item(_GOLD_TITLE)],
        ),
        # L1's own news source (design doc §4.8, second addendum) — see the
        # matching comment in
        # test_generate_report_pass2_call_excludes_l1_ticker_intel_text.
        patch(
            "app.services.report_generator.load_day_news",
            return_value=[_news_item(_GOLD_TITLE)],
        ),
        # round 6 review fix: a headline alone no longer keeps SGOL from
        # being dropped — it needs a real `day_pct` too (this test mocks
        # detect_window_anomalies, so there's no real price_snapshot row).
        patch(
            "app.services.report_generator.resolve_global_moves",
            return_value=({"SGOL": _day_move("SGOL")}, 1),
        ),
        patch("app.services.report_generator.detect_macro_signals", return_value=_macro_hit()),
        patch(
            "app.services.report_generator.detect_window_anomalies",
            return_value=([theme_anomaly], 2),
        ),
        patch("app.services.report_generator._openrouter_client", return_value=MagicMock()),
        patch("app.services.report_generator._call_llm", side_effect=_mock_llm),
        patch("app.services.report_generator._run_tavily_search", return_value=[]),
        patch("app.services.ticker_intel._openrouter_client", return_value=MagicMock()),
        patch("app.services.ticker_intel._call_llm", side_effect=_capture_l1_llm),
    ):
        report = rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)

    assert report.status == "success"
    assert report.report_inputs is not None
    intel = report.report_inputs["ticker_intel"]
    assert "gold" not in intel
    assert "SGOL" in intel
    # SGOL's own briefing was built from news recalled under ITS identifier.
    assert _GOLD_TITLE in l1_prompts["Identifier: SGOL"]
    # The user's value-weighted theme figure never reaches the shared prompt.
    assert "5.12" not in l1_prompts["Identifier: SGOL"]


def test_generate_report_l1_facts_are_independent_of_the_calling_users_watermark(
    db_session: Session,
) -> None:
    """THE round-5 review bug, reproduced and locked at the level it actually
    manifested: `ticker_intel` was cached as a system-wide daily row
    `(identifier, trade_date, prompt_version)`, but the facts written into
    it came from `resolve_global_moves(session, period_start, period_end,
    ...)` — and `period_start = user_watermark(user_id)` is per-user. Two
    users generating a report for the same `eff_date` could have very
    different windows (a brand-new user's watermark vs. a long-standing
    user's), so whichever one's `generate_report` call reached L1 first
    would cache THEIR window's price move for every other user that day.

    User A's watermark predates all seeded history by over a month (an
    old/cold-start-like user); User B's watermark is midday two days before
    `eff_date` (a long-running user who reported more recently). Under the
    old per-user-window code, these two would compute genuinely different
    multi-day `net_pct` figures for NVDA — this is verified directly below
    via `resolve_global_moves` with each user's own bounds, so the test
    doesn't just assert "the fix works," it also proves the fixture
    actually would have exposed the bug. The redesigned L1 must ignore both
    and always compute NVDA's single trading day's move (6/3's close of
    210 -> 6/4's close of 220 = +4.76%).

    All captured_at values are pinned to 16:00 ET (20:00 UTC, unambiguously
    the same ET calendar day as their trade_date, no DST-boundary surprises
    for this June/early-May range) so the window math below is exact and
    independent of when this test actually runs."""
    from app.models.holding import Holding
    from app.models.price_snapshot import PriceSnapshot
    from app.models.report import Report
    from app.models.ticker_intel import TickerIntel
    from app.services.window_data import resolve_global_moves

    user_a = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
    user_b = uuid.UUID("00000000-0000-0000-0000-0000000000a2")
    seed_user(db_session, user_a)
    seed_user(db_session, user_b)
    watermark_a = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)  # before ALL seeded closes
    watermark_b = datetime(2026, 6, 2, 12, 0, tzinfo=UTC)  # after 6/1's close, before 6/3's

    def _nvda_close(trade_date: date, close: str) -> PriceSnapshot:
        return PriceSnapshot(
            ticker="NVDA",
            market="US",
            session_node="close",
            trade_date=trade_date,
            close=Decimal(close),
            captured_at=datetime(
                trade_date.year, trade_date.month, trade_date.day, 20, 0, tzinfo=UTC
            ),
        )

    db_session.add_all(
        [
            Holding(
                user_id=user_a,
                name="NVIDIA",
                ticker="NVDA",
                pricing_mode="auto",
                currency="USD",
                asset_type="stock",
                asset_class="EQUITY_US_TECH",
            ),
            Report(
                user_id=user_a,
                report_date=date(2026, 4, 30),
                report_type="incremental",
                session_node="after_close",
                status="success",
                period_end=watermark_a,
            ),
            Report(
                user_id=user_b,
                report_date=date(2026, 6, 2),
                report_type="incremental",
                session_node="after_close",
                status="success",
                period_end=watermark_b,
            ),
            _nvda_close(date(2026, 4, 25), "190"),  # baseline for user A's own window
            _nvda_close(date(2026, 6, 1), "200"),  # baseline for user B's own window
            _nvda_close(date(2026, 6, 3), "210"),  # baseline for L1's day-scoped window
            _nvda_close(_TODAY, "220"),  # 2026-06-04 — the day-scoped "latest"
        ]
    )
    db_session.flush()

    # Prove the fixture actually distinguishes the two users' own windows —
    # otherwise this test would pass even under the old, buggy code.
    end = datetime(2026, 6, 4, 22, 0, tzinfo=UTC)
    moves_a, _ = resolve_global_moves(db_session, watermark_a, end)
    moves_b, _ = resolve_global_moves(db_session, watermark_b, end)
    assert moves_a["NVDA"].net_pct == Decimal("0.1579")  # (220-190)/190
    assert moves_b["NVDA"].net_pct == Decimal("0.1000")  # (220-200)/200
    assert moves_a["NVDA"].net_pct != moves_b["NVDA"].net_pct

    captured_prompts: list[str] = []

    def _capture_l1_llm(
        client: object, model: str, system: str, user: str, **kwargs: object
    ) -> str:
        captured_prompts.append(user)
        return "NVDA held roughly flat. [Speculative]"

    def _run_for(user_id: uuid.UUID) -> None:
        with (
            patch(
                "app.services.report_generator.compute_portfolio", return_value=_portfolio_snap()
            ),
            patch("app.services.report_generator.load_news_window", return_value=[]),
            patch("app.services.report_generator.detect_macro_signals", return_value=_macro_hit()),
            patch(
                "app.services.report_generator.detect_window_anomalies",
                return_value=([_anomaly()], 2),
            ),
            patch("app.services.report_generator._openrouter_client", return_value=MagicMock()),
            patch("app.services.report_generator._call_llm", side_effect=_mock_llm),
            patch("app.services.report_generator._run_tavily_search", return_value=[]),
            patch("app.services.ticker_intel._openrouter_client", return_value=MagicMock()),
            patch("app.services.ticker_intel._call_llm", side_effect=_capture_l1_llm),
        ):
            report = rg.generate_report(
                db_session, user_id=user_id, report_date=_TODAY, session_node="manual"
            )
        assert report.status == "success"

    _run_for(user_a)
    # Clear the cache row so user B's call actually recomputes instead of
    # reading back user A's cached (and, pre-fix, potentially different) text.
    db_session.execute(delete(TickerIntel).where(TickerIntel.identifier == "NVDA"))
    db_session.flush()
    _run_for(user_b)

    assert len(captured_prompts) == 2
    for prompt in captured_prompts:
        assert "Price change vs. the prior trading day's close: +4.76%" in prompt


def test_generate_report_retry_clears_stale_provider_message_id(db_session: Session) -> None:
    """issue #45 review follow-up: a row reused for retry (status not success/
    skipped) must clear provider_message_id alongside email_sent_at — otherwise
    a previously-sent report can carry a stale Resend id into its next attempt
    while email_sent_at reads NULL, breaking the "both set or both unset"
    invariant the pair is meant to hold."""

    def _mock_pipeline() -> list[contextlib.AbstractContextManager[object]]:
        return [
            patch(
                "app.services.report_generator.compute_portfolio", return_value=_portfolio_snap()
            ),
            patch(
                "app.services.report_generator.load_news_window",
                return_value=[_news_item("Fed raises rates")],
            ),
            patch("app.services.report_generator.detect_macro_signals", return_value=_macro_hit()),
            patch(
                "app.services.report_generator.detect_window_anomalies",
                return_value=([_anomaly()], 2),
            ),
            patch("app.services.report_generator._openrouter_client", return_value=MagicMock()),
            patch("app.services.report_generator._call_llm", side_effect=_mock_llm),
            patch(
                "app.services.report_generator._run_tavily_search",
                return_value=_FAKE_TAVILY_RESULTS,
            ),
        ]

    with contextlib.ExitStack() as stack:
        for cm in _mock_pipeline():
            stack.enter_context(cm)
        report = rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)

    assert report.status == "success"

    # Simulate a prior real send, then a state that makes the row eligible for
    # reset on the next generate_report() call (anything not success/skipped).
    report.status = "needs_review"
    report.email_sent_at = _NOW
    report.provider_message_id = "resend-stale-id-from-prior-send"
    db_session.commit()

    with contextlib.ExitStack() as stack:
        for cm in _mock_pipeline():
            stack.enter_context(cm)
        retried = rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)

    assert retried.id == report.id
    assert retried.status == "success"
    # send_report_email is mocked (module-level _no_email fixture) and never
    # touches provider_message_id, so a None here proves the reset branch
    # cleared it rather than it being silently repopulated by a real send.
    assert retried.provider_message_id is None


# ---------------------------------------------------------------------------
# Tests: stage-skip on retry (#61) — a retry must not redo Pass 1 + Pass 2
# when the failed row already carries a complete body, and must not redo
# ANYTHING for a success-but-unsent-email row.
# ---------------------------------------------------------------------------


def test_generate_report_retry_after_render_failure_skips_pass1_pass2(
    db_session: Session, _no_email: MagicMock
) -> None:
    """#61: Pass 2 succeeds, render raises -> the failed row's report_inputs
    already carries a complete pass2_raw (persisted by the outer except
    handler). A retry must resume straight from render using that stored
    body instead of redoing Pass 1 + Pass 2 (a real, costly LLM call) and
    re-fetching the portfolio/news/anomalies inputs those passes needed."""
    with (
        patch("app.services.report_generator.compute_portfolio", return_value=_portfolio_snap()),
        patch(
            "app.services.report_generator.load_news_window",
            return_value=[_news_item("Fed raises rates")],
        ),
        patch("app.services.report_generator.detect_macro_signals", return_value=_macro_hit()),
        patch(
            "app.services.report_generator.detect_window_anomalies", return_value=([_anomaly()], 2)
        ),
        patch("app.services.report_generator._openrouter_client", return_value=MagicMock()),
        patch("app.services.report_generator._call_llm", side_effect=_mock_llm) as mock_llm,
        patch("app.services.report_generator._run_tavily_search", return_value=[]),
        patch(
            "app.services.report_generator._render_full_md",
            side_effect=RuntimeError("render boom"),
        ),
        pytest.raises(RuntimeError, match="render boom"),
    ):
        rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)

    assert mock_llm.call_count == 2  # Pass 1 + Pass 2 each ran exactly once

    row = db_session.execute(
        select(Report).where(Report.user_id == _USER, Report.report_date == _TODAY)
    ).scalar_one()
    assert row.status == "failed"
    assert row.report_inputs is not None
    assert row.report_inputs.get("pass2_raw")

    with (
        patch("app.services.report_generator.compute_portfolio") as mock_portfolio2,
        patch("app.services.report_generator.load_news_window") as mock_news2,
        patch("app.services.report_generator.detect_macro_signals") as mock_macro2,
        patch("app.services.report_generator.detect_window_anomalies") as mock_anom2,
        patch("app.services.report_generator._openrouter_client") as mock_client2,
        patch("app.services.report_generator._call_llm") as mock_llm2,
    ):
        retried = rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)

    assert retried.id == row.id
    assert retried.status == "success"
    assert retried.report_md is not None
    assert "§4 Risk Radar" in retried.report_md
    mock_llm2.assert_not_called()
    mock_client2.assert_not_called()
    mock_portfolio2.assert_not_called()
    mock_news2.assert_not_called()
    mock_macro2.assert_not_called()
    mock_anom2.assert_not_called()


def test_generate_report_retry_after_prompt_version_bump_reruns_pass1_pass2(
    db_session: Session, _no_email: MagicMock
) -> None:
    """#61: the stage-skip reuse gate must not fire across a prompt_version
    change — a prompt/code change landing between the failed attempt and the
    retry must force a full regeneration under the new prompt, not ship a
    body produced under the stale one."""
    with (
        patch("app.services.report_generator.compute_portfolio", return_value=_portfolio_snap()),
        patch(
            "app.services.report_generator.load_news_window",
            return_value=[_news_item("Fed raises rates")],
        ),
        patch("app.services.report_generator.detect_macro_signals", return_value=_macro_hit()),
        patch(
            "app.services.report_generator.detect_window_anomalies", return_value=([_anomaly()], 2)
        ),
        patch("app.services.report_generator._openrouter_client", return_value=MagicMock()),
        patch("app.services.report_generator._call_llm", side_effect=_mock_llm),
        patch("app.services.report_generator._run_tavily_search", return_value=[]),
        patch(
            "app.services.report_generator._render_full_md",
            side_effect=RuntimeError("render boom"),
        ),
        pytest.raises(RuntimeError, match="render boom"),
    ):
        rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)

    row = db_session.execute(
        select(Report).where(Report.user_id == _USER, Report.report_date == _TODAY)
    ).scalar_one()
    assert row.status == "failed"
    assert row.report_inputs is not None
    assert row.report_inputs.get("pass2_raw")

    with (
        patch.object(rg, "_PROMPT_VERSION", rg._PROMPT_VERSION + "-bumped"),
        patch("app.services.report_generator.compute_portfolio", return_value=_portfolio_snap()),
        patch(
            "app.services.report_generator.load_news_window",
            return_value=[_news_item("Fed raises rates")],
        ),
        patch("app.services.report_generator.detect_macro_signals", return_value=_macro_hit()),
        patch(
            "app.services.report_generator.detect_window_anomalies", return_value=([_anomaly()], 2)
        ),
        patch("app.services.report_generator._openrouter_client", return_value=MagicMock()),
        patch("app.services.report_generator._call_llm", side_effect=_mock_llm) as mock_llm2,
        patch("app.services.report_generator._run_tavily_search", return_value=[]),
    ):
        retried = rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)

    assert retried.id == row.id
    assert retried.status == "success"
    mock_llm2.assert_called()  # full rerun, not the stage-skip reuse path
    assert retried.prompt_version == rg._PROMPT_VERSION + "-bumped"


def test_generate_report_retry_of_unsent_success_only_resends_email(
    db_session: Session,
) -> None:
    """#61: status=='success' but email_sent_at IS NULL (delivery never
    confirmed) must be resendable via a plain retry, without re-rendering or
    making any LLM call — the report body is already final."""
    with (
        patch("app.services.report_generator.compute_portfolio", return_value=_portfolio_snap()),
        patch(
            "app.services.report_generator.load_news_window",
            return_value=[_news_item("Fed raises rates")],
        ),
        patch("app.services.report_generator.detect_macro_signals", return_value=_macro_hit()),
        patch(
            "app.services.report_generator.detect_window_anomalies", return_value=([_anomaly()], 2)
        ),
        patch("app.services.report_generator._openrouter_client", return_value=MagicMock()),
        patch("app.services.report_generator._call_llm", side_effect=_mock_llm),
        patch("app.services.report_generator._run_tavily_search", return_value=[]),
        patch("app.services.report_generator.send_report_email", return_value=False) as mock_email,
    ):
        report = rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)

    assert report.status == "success"
    assert report.email_sent_at is None
    mock_email.assert_called_once()

    with (
        patch("app.services.report_generator.compute_portfolio") as mock_portfolio2,
        patch("app.services.report_generator.load_news_window") as mock_news2,
        patch("app.services.report_generator._openrouter_client") as mock_client2,
        patch("app.services.report_generator._call_llm") as mock_llm2,
        patch("app.services.report_generator._render_full_md") as mock_render2,
        patch("app.services.report_generator.send_report_email", return_value=True) as mock_email2,
    ):
        retried = rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)

    assert retried.id == report.id
    assert retried.status == "success"
    mock_email2.assert_called_once()
    mock_llm2.assert_not_called()
    mock_client2.assert_not_called()
    mock_portfolio2.assert_not_called()
    mock_news2.assert_not_called()
    mock_render2.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: quiet-day skip
# ---------------------------------------------------------------------------


def test_generate_report_quiet_day_returns_skipped(db_session: Session) -> None:
    """No signals and no anomalies → status=skipped, no LLM call."""
    with (
        patch("app.services.report_generator.compute_portfolio", return_value=_portfolio_snap()),
        patch("app.services.report_generator.load_news_window", return_value=[]),
        patch("app.services.report_generator.detect_macro_signals", return_value=_quiet_signals()),
        patch("app.services.report_generator.detect_window_anomalies", return_value=([], 0)),
        patch("app.services.report_generator._call_llm") as mock_llm,
    ):
        report = rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)

    assert report.status == "skipped"
    mock_llm.assert_not_called()
    assert report.report_md is not None
    assert "§1 Portfolio Snapshot" in report.report_md


def test_generate_report_quiet_day_unsent_email_does_not_log_sent(
    db_session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    """PR #181 review: send_report_email returning False now also means
    "recipient could not be resolved" (fail-closed), not just "commit
    failed after a real send". The caller's log line must not claim the
    email was sent when it demonstrably wasn't. Uses session_node=
    after_close so the short-manual-quiet suppression doesn't skip the
    email branch entirely."""
    import logging as _logging

    _logging.getLogger("app.services.report_generator").disabled = False
    with (
        patch("app.services.report_generator.compute_portfolio", return_value=_portfolio_snap()),
        patch("app.services.report_generator.load_news_window", return_value=[]),
        patch("app.services.report_generator.detect_macro_signals", return_value=_quiet_signals()),
        patch("app.services.report_generator.detect_window_anomalies", return_value=([], 0)),
        patch("app.services.report_generator._call_llm"),
        patch("app.services.report_generator.send_report_email", return_value=False),
        caplog.at_level("WARNING", logger="app.services.report_generator"),
    ):
        report = rg.generate_report(
            db_session, user_id=_USER, report_date=_TODAY, session_node="after_close"
        )

    assert report.status == "skipped"
    sent_claims = [r for r in caplog.records if "email sent" in r.getMessage().lower()]
    assert not sent_claims, (
        f"log line falsely claims delivery: {[r.getMessage() for r in sent_claims]}"
    )


# ---------------------------------------------------------------------------
# Tests: Tavily failure (degraded mode)
# ---------------------------------------------------------------------------


def test_generate_report_tavily_failure_degraded(db_session: Session) -> None:
    """When Tavily fails, the report is still generated (degraded mode)."""
    with (
        patch("app.services.report_generator.compute_portfolio", return_value=_portfolio_snap()),
        patch(
            "app.services.report_generator.load_news_window",
            return_value=[_news_item("Fed raises rates")],
        ),
        patch("app.services.report_generator.detect_macro_signals", return_value=_macro_hit()),
        patch("app.services.report_generator.detect_window_anomalies", return_value=([], 0)),
        patch("app.services.report_generator._openrouter_client", return_value=MagicMock()),
        patch("app.services.report_generator._call_llm", side_effect=_mock_llm),
        patch("app.services.report_generator._run_tavily_search", return_value=[]),
    ):
        report = rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)

    assert report.status == "success"
    assert report.report_inputs is not None
    assert report.report_inputs["search_results"] == []


# ---------------------------------------------------------------------------
# Tests: LLM Pass 1 returns invalid JSON (graceful fallback)
# ---------------------------------------------------------------------------


def test_generate_report_pass1_invalid_json(db_session: Session) -> None:
    """Pass 1 returns garbage JSON → search_queries empty, pipeline continues.

    `mock_tavily` is NOT asserted uncalled (pre-2026-08-20 behavior): the
    weight-driven targeted search added for issue #128's narrative-layer
    redesign runs independently of Pass 1's own query list — `_portfolio_snap()`'s
    single AAPL holding is 100% of the portfolio with no anomaly and no
    recalled news (the Fed-themed fixture article doesn't match AAPL), so it
    still triggers exactly one targeted call regardless of whether Pass 1
    parsed. What this test actually locks is narrower: Pass 1 failing to
    parse must not itself add any OF ITS OWN queries to `search_queries`.
    """

    def bad_pass1(
        client: object,
        model: str,
        system: str,
        user: str,
        *,
        with_holdings: bool = False,
        **kwargs: object,
    ) -> str:
        if not with_holdings:
            return "not valid json at all"
        return _FAKE_LLM_PASS2

    with (
        patch("app.services.report_generator.compute_portfolio", return_value=_portfolio_snap()),
        patch("app.services.report_generator.load_news_window", return_value=[_news_item("Fed")]),
        patch("app.services.report_generator.detect_macro_signals", return_value=_macro_hit()),
        patch("app.services.report_generator.detect_window_anomalies", return_value=([], 0)),
        patch("app.services.report_generator._openrouter_client", return_value=MagicMock()),
        patch("app.services.report_generator._call_llm", side_effect=bad_pass1),
        patch("app.services.report_generator._run_tavily_search", return_value=[]) as mock_tavily,
    ):
        report = rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)

    assert report.status == "success"
    assert report.report_inputs is not None
    assert report.report_inputs["search_queries"] == []
    called_queries = [c.args[1] for c in mock_tavily.call_args_list]
    assert len(called_queries) == 1
    assert len(called_queries[0]) == 1
    assert called_queries[0][0].startswith("AAPL stock news catalyst ")


# ---------------------------------------------------------------------------
# Tests: LLM failure → report status=failed
# ---------------------------------------------------------------------------


def test_generate_report_llm_failure_marks_failed(db_session: Session) -> None:
    """LLM exception → report persisted with status=failed, exception re-raised."""
    with (
        patch("app.services.report_generator.compute_portfolio", return_value=_portfolio_snap()),
        patch("app.services.report_generator.load_news_window", return_value=[_news_item("Fed")]),
        patch("app.services.report_generator.detect_macro_signals", return_value=_macro_hit()),
        patch("app.services.report_generator.detect_window_anomalies", return_value=([], 0)),
        patch("app.services.report_generator._openrouter_client", return_value=MagicMock()),
        patch("app.services.report_generator._call_llm", side_effect=RuntimeError("LLM down")),
        pytest.raises(RuntimeError, match="LLM down"),
    ):
        rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)

    # The failed report should have been persisted
    from sqlalchemy import select

    from app.models.report import Report

    row = db_session.execute(
        select(Report).where(Report.user_id == _USER, Report.report_date == _TODAY)
    ).scalar_one_or_none()
    assert row is not None
    assert row.status == "failed"


def test_generate_report_pass1_call_has_no_holdings(db_session: Session) -> None:
    """End-to-end: the with_holdings=False LLM call must not contain the
    portfolio ticker even when an anomaly for that holding exists."""
    captured: dict[str, str] = {}

    def _capture_llm(
        client: object,
        model: str,
        system: str,
        user: str,
        *,
        with_holdings: bool = False,
        **kwargs: object,
    ) -> str:
        if not with_holdings:
            captured["pass1_user"] = user
            return _FAKE_LLM_PASS1
        return _FAKE_LLM_PASS2

    aapl_anomaly = PriceAnomaly(
        name="Apple Inc.",
        identifier="AAPL",
        asset_type="stock",
        current_price=Decimal("200.0"),
        prev_price=Decimal("180.0"),
        pct_change=Decimal("0.1111"),
        threshold=Decimal("0.03"),
    )
    with (
        patch("app.services.report_generator.compute_portfolio", return_value=_portfolio_snap()),
        patch(
            "app.services.report_generator.load_news_window",
            return_value=[_news_item("Fed raises rates")],
        ),
        patch("app.services.report_generator.detect_macro_signals", return_value=_macro_hit()),
        patch(
            "app.services.report_generator.detect_window_anomalies",
            return_value=([aapl_anomaly], 2),
        ),
        patch("app.services.report_generator._openrouter_client", return_value=MagicMock()),
        patch("app.services.report_generator._call_llm", side_effect=_capture_llm),
        patch("app.services.report_generator._run_tavily_search", return_value=[]),
    ):
        rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)

    assert "pass1_user" in captured
    assert "AAPL" not in captured["pass1_user"]
    assert "Apple" not in captured["pass1_user"]


def test_generate_report_pass1_call_uses_byok_hard_pin(db_session: Session) -> None:
    """PR #79 review: Pass 1's _call_llm invocation must carry the BYOK hard-pin
    kwargs (order=["DeepSeek"], allow_fallbacks=False, deny off, reasoning off)
    — not just the isolation property covered by the test above. A future edit
    that drops any one of these would silently reopen the marketplace-fallback
    compliance gap the review flagged."""
    captured: dict[str, object] = {}

    def _capture_llm(
        client: object,
        model: str,
        system: str,
        user: str,
        *,
        with_holdings: bool = False,
        **kwargs: object,
    ) -> str:
        if not with_holdings:
            captured.update(kwargs)
            return _FAKE_LLM_PASS1
        return _FAKE_LLM_PASS2

    with (
        patch("app.services.report_generator.compute_portfolio", return_value=_portfolio_snap()),
        patch(
            "app.services.report_generator.load_news_window",
            return_value=[_news_item("Fed raises rates")],
        ),
        patch("app.services.report_generator.detect_macro_signals", return_value=_macro_hit()),
        patch(
            "app.services.report_generator.detect_window_anomalies",
            return_value=([], 2),
        ),
        patch("app.services.report_generator._openrouter_client", return_value=MagicMock()),
        patch("app.services.report_generator._call_llm", side_effect=_capture_llm),
        patch("app.services.report_generator._run_tavily_search", return_value=[]),
    ):
        rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)

    assert captured.get("provider_order") == _BYOK_PROVIDER_ORDER == ["DeepSeek"]
    assert captured.get("allow_fallbacks") is False
    assert captured.get("enforce_data_collection") is False
    assert captured.get("disable_reasoning") is True


# ---------------------------------------------------------------------------
# Tests: compliance output backstop (end-to-end wiring; the scan itself is
# covered in test_output_scan.py)
# ---------------------------------------------------------------------------


def _mock_llm_noncompliant(
    client: object,
    model: str,
    system: str,
    user: str,
    *,
    with_holdings: bool = False,
    **kwargs: object,
) -> str:
    if with_holdings:
        return (
            "## §2 Macro Signals\n\nYou should buy more semiconductors now. "
            "[For information only — not investment advice]\n\n"
            "## §3 Holdings Intelligence\n\nNVIDIA up 9%. [For information only — not investment advice]\n\n"
            "## §4 Risk Radar\n\nConcentration watch. [For information only — not investment advice]\n\n"
            + _PASS2_FILLER
        )
    return _FAKE_LLM_PASS1


def test_generate_report_blocks_noncompliant_body(
    db_session: Session, _no_email: MagicMock
) -> None:
    """A body that trips the blacklist is held as needs_review and never emailed."""
    with (
        patch("app.services.report_generator.compute_portfolio", return_value=_portfolio_snap()),
        patch("app.services.report_generator.load_news_window", return_value=[_news_item("Fed")]),
        patch("app.services.report_generator.detect_macro_signals", return_value=_macro_hit()),
        patch(
            "app.services.report_generator.detect_window_anomalies", return_value=([_anomaly()], 2)
        ),
        patch("app.services.report_generator._openrouter_client", return_value=MagicMock()),
        patch("app.services.report_generator._call_llm", side_effect=_mock_llm_noncompliant),
        patch("app.services.report_generator._run_tavily_search", return_value=[]),
    ):
        report = rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)

    assert report.status == "needs_review"
    assert report.report_md is not None  # content preserved for inspection
    _no_email.assert_not_called()


def test_generate_report_retry_of_needs_review_unmarks_prior_surfaced_news(
    db_session: Session, _no_email: MagicMock
) -> None:
    """PR #139 review: generate_report reopens a needs_review row and reuses its
    frozen window — unmark_news_surfaced must run on the reopen so the retry's
    load_news_window call reselects the same candidate set the first attempt
    saw, instead of silently seeing a smaller set because of the first
    attempt's own marks."""
    with (
        patch("app.services.report_generator.compute_portfolio", return_value=_portfolio_snap()),
        patch("app.services.report_generator.load_news_window", return_value=[_news_item("Fed")]),
        patch("app.services.report_generator.detect_macro_signals", return_value=_macro_hit()),
        patch(
            "app.services.report_generator.detect_window_anomalies", return_value=([_anomaly()], 2)
        ),
        patch("app.services.report_generator._openrouter_client", return_value=MagicMock()),
        patch("app.services.report_generator._call_llm", side_effect=_mock_llm_noncompliant),
        patch("app.services.report_generator._run_tavily_search", return_value=[]),
        patch("app.services.report_generator.unmark_news_surfaced") as mock_unmark,
    ):
        report1 = rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)
        assert report1.status == "needs_review"
        mock_unmark.assert_not_called()  # fresh row — nothing to unmark yet

        report2 = rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)
        assert report2.id == report1.id  # same row, reopened for retry
        mock_unmark.assert_called_once_with(db_session, report1.id)


def test_generate_report_quiet_day_sends_heartbeat(
    db_session: Session, _no_email: MagicMock
) -> None:
    """A quiet week must still deliver a heartbeat email so silence != broken."""
    with (
        patch("app.services.report_generator.compute_portfolio", return_value=_portfolio_snap()),
        patch("app.services.report_generator.load_news_window", return_value=[]),
        patch("app.services.report_generator.detect_macro_signals", return_value=_quiet_signals()),
        patch("app.services.report_generator.detect_window_anomalies", return_value=([], 0)),
    ):
        report = rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)

    assert report.status == "skipped"
    _no_email.assert_called_once()


# ---------------------------------------------------------------------------
# Tests: data window (#5), translation render (#8), re-render (#6)
# ---------------------------------------------------------------------------


def _normal_path_patches() -> list[object]:
    return [
        patch("app.services.report_generator.compute_portfolio", return_value=_portfolio_snap()),
        patch("app.services.report_generator.load_news_window", return_value=[_news_item("Fed")]),
        patch("app.services.report_generator.detect_macro_signals", return_value=_macro_hit()),
        patch(
            "app.services.report_generator.detect_window_anomalies", return_value=([_anomaly()], 2)
        ),
        patch("app.services.report_generator._openrouter_client", return_value=MagicMock()),
        patch("app.services.report_generator._call_llm", side_effect=_mock_llm),
        patch("app.services.report_generator._run_tavily_search", return_value=[]),
    ]


def test_generate_report_includes_data_window(db_session: Session) -> None:
    with contextlib.ExitStack() as stack:
        for p in _normal_path_patches():
            stack.enter_context(p)  # type: ignore[arg-type]
        report = rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)
    assert report.report_md is not None
    assert "Data window" in report.report_md


def test_generate_report_translates_when_output_lang_set(db_session: Session) -> None:
    """output_lang != en routes the assembled report through translation."""
    with contextlib.ExitStack() as stack:
        for p in _normal_path_patches():
            stack.enter_context(p)  # type: ignore[arg-type]
        stack.enter_context(
            patch(
                "app.services.report_generator._translate_md",
                side_effect=lambda md, lang: f"[{lang}]\n{md}",
            )
        )
        report = rg.generate_report(db_session, user_id=_USER, report_date=_TODAY, output_lang="zh")
    assert report.report_md is not None
    assert "[zh]" in report.report_md


def test_regenerate_render_is_token_free(db_session: Session) -> None:
    """mode=render rebuilds from stored Pass 2 body with no LLM call."""
    with contextlib.ExitStack() as stack:
        for p in _normal_path_patches():
            stack.enter_context(p)  # type: ignore[arg-type]
        report = rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)
    rid = report.id

    with (
        patch(
            "app.services.report_generator._call_llm",
            side_effect=AssertionError("render must not call the LLM"),
        ),
        patch(
            "app.services.report_generator.load_news_window",
            side_effect=AssertionError("render must not re-fetch"),
        ),
    ):
        out = rg.regenerate_report(db_session, rid, user_id=_USER, mode="render", output_lang="en")

    assert out.status == "success"
    assert out.report_md is not None
    assert "§1 Portfolio Snapshot" in out.report_md
    assert "Data window" in out.report_md


def test_regenerate_analyze_reruns_pass2_from_stored_intel(db_session: Session) -> None:
    """mode=analyze re-runs Pass 2 only — no news/Tavily/Pass 1 re-fetch."""
    with contextlib.ExitStack() as stack:
        for p in _normal_path_patches():
            stack.enter_context(p)  # type: ignore[arg-type]
        report = rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)
    rid = report.id

    new_body = (
        "## §2 Macro Signals\n\nReanalyzed view. [For information only — not investment advice]\n\n"
        "## §3 Holdings Intelligence\n\nNVIDIA up 9%. [For information only — not investment advice]\n\n"
        "## §4 Risk Radar\n\nConcentration watch. [For information only — not investment advice]\n\n"
        + _PASS2_FILLER
    )
    with (
        patch("app.services.report_generator._openrouter_client", return_value=MagicMock()),
        patch("app.services.report_generator._call_llm", return_value=new_body),
        patch(
            "app.services.report_generator.load_news_window",
            side_effect=AssertionError("analyze must not re-fetch news"),
        ),
        patch(
            "app.services.report_generator._run_tavily_search",
            side_effect=AssertionError("analyze must not re-run search"),
        ),
    ):
        out = rg.regenerate_report(db_session, rid, user_id=_USER, mode="analyze", output_lang="en")

    assert out.report_md is not None
    assert "Reanalyzed view" in out.report_md
    assert out.report_inputs is not None
    assert "Reanalyzed view" in out.report_inputs["pass2_raw"]


# --- R-7: short manual quiet-window email suppression ----------------------


def test_is_short_manual_quiet_true_for_tiny_empty_manual_window() -> None:
    start = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)
    end = datetime(2026, 6, 10, 12, 18, tzinfo=UTC)
    assert rg._is_short_manual_quiet("manual", start, end, [], []) is True


def test_is_short_manual_quiet_false_for_scheduled_trigger() -> None:
    start = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)
    end = datetime(2026, 6, 10, 12, 18, tzinfo=UTC)
    assert rg._is_short_manual_quiet("after_close", start, end, [], []) is False


def test_is_short_manual_quiet_false_when_window_long() -> None:
    start = datetime(2026, 6, 9, 12, 0, tzinfo=UTC)
    end = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)
    assert rg._is_short_manual_quiet("manual", start, end, [], []) is False


def test_is_short_manual_quiet_false_when_news_present() -> None:
    start = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)
    end = datetime(2026, 6, 10, 12, 18, tzinfo=UTC)
    assert rg._is_short_manual_quiet("manual", start, end, [_news_item("x")], []) is False


# ---------------------------------------------------------------------------
# Tests: integration — inline markers are stripped from the generated report (#9)
# ---------------------------------------------------------------------------


def _mock_llm_f2(
    client: object,
    model: str,
    system: str,
    user: str,
    *,
    with_holdings: bool = False,
    **kwargs: object,
) -> str:
    if with_holdings:
        return _FAKE_LLM_PASS2_F2
    return _FAKE_LLM_PASS1


def test_generate_report_strips_inline_markers(db_session: Session) -> None:
    """The generated report must carry no [S#] citations, provenance tags, or
    per-sentence disclaimer suffixes — only the footer disclaimer remains (#9)."""
    with (
        patch("app.services.report_generator.compute_portfolio", return_value=_portfolio_snap()),
        patch(
            "app.services.report_generator.load_news_window",
            return_value=[_news_item("Fed raises rates")],
        ),
        patch("app.services.report_generator.detect_macro_signals", return_value=_macro_hit()),
        patch(
            "app.services.report_generator.detect_window_anomalies", return_value=([_anomaly()], 2)
        ),
        patch("app.services.report_generator._openrouter_client", return_value=MagicMock()),
        patch("app.services.report_generator._call_llm", side_effect=_mock_llm_f2),
        patch(
            "app.services.report_generator._run_tavily_search", return_value=_FAKE_TAVILY_RESULTS
        ),
    ):
        report = rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)

    assert report.status == "success"
    assert report.report_md is not None
    md = report.report_md
    # Body markers are gone.
    assert "[S1]" not in md
    assert "[新闻]" not in md
    assert "[行情]" not in md
    assert "[分析]" not in md
    # The body's per-sentence disclaimer suffix is stripped, but the footer's
    # single bilingual disclaimer (which legitimately says "not investment advice")
    # remains — so the phrase still appears, only in the footer.
    assert "Data Sources & Disclaimer" in md


# ---------------------------------------------------------------------------
# Tests: F3 footer (integration — the code-built footer itself is covered in
# test_report_sections.py)
# ---------------------------------------------------------------------------


def test_generate_report_normal_path_has_footer(db_session: Session) -> None:
    """Footer must appear in every successfully generated report."""
    with (
        patch("app.services.report_generator.compute_portfolio", return_value=_portfolio_snap()),
        patch(
            "app.services.report_generator.load_news_window",
            return_value=[_news_item("Fed raises rates")],
        ),
        patch("app.services.report_generator.detect_macro_signals", return_value=_macro_hit()),
        patch(
            "app.services.report_generator.detect_window_anomalies", return_value=([_anomaly()], 2)
        ),
        patch("app.services.report_generator._openrouter_client", return_value=MagicMock()),
        patch("app.services.report_generator._call_llm", side_effect=_mock_llm),
        patch(
            "app.services.report_generator._run_tavily_search", return_value=_FAKE_TAVILY_RESULTS
        ),
    ):
        report = rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)

    assert report.status == "success"
    assert report.report_md is not None
    assert "免责声明" in report.report_md
    assert "Data Sources & Disclaimer" in report.report_md


def test_generate_report_quiet_day_has_footer(db_session: Session) -> None:
    """Footer must also appear on quiet-day (status=skipped) reports."""
    with (
        patch("app.services.report_generator.compute_portfolio", return_value=_portfolio_snap()),
        patch("app.services.report_generator.load_news_window", return_value=[]),
        patch("app.services.report_generator.detect_macro_signals", return_value=_quiet_signals()),
        patch("app.services.report_generator.detect_window_anomalies", return_value=([], 0)),
        patch("app.services.report_generator._call_llm") as mock_llm,
    ):
        report = rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)

    assert report.status == "skipped"
    mock_llm.assert_not_called()
    assert report.report_md is not None
    assert "免责声明" in report.report_md
    assert "Data Sources & Disclaimer" in report.report_md


# ---------------------------------------------------------------------------
# Tests: explicit user_id (issue #128 A1)
# ---------------------------------------------------------------------------

_OTHER_USER = uuid.UUID("00000000-0000-0000-0000-0000000000aa")


def test_generate_report_uses_explicit_user_id(db_session: Session) -> None:
    """user_id (issue #129 B3: required, no ambient fallback) is used as-is
    for the report row — the multi-user fan-out (report_tasks.py) relies on
    this to generate each user's report under their own identity."""
    seed_user(db_session, _OTHER_USER)
    with (
        patch("app.services.report_generator.compute_portfolio", return_value=_portfolio_snap()),
        patch("app.services.report_generator.load_news_window", return_value=[]),
        patch("app.services.report_generator.detect_macro_signals", return_value=_quiet_signals()),
        patch("app.services.report_generator.detect_window_anomalies", return_value=([], 0)),
        patch("app.services.report_generator._call_llm") as mock_llm,
    ):
        report = rg.generate_report(db_session, user_id=_OTHER_USER, report_date=_TODAY)

    mock_llm.assert_not_called()  # quiet day, never reaches Pass 1/2
    assert report.user_id == _OTHER_USER


def test_generate_report_forwards_moves_cache_to_detect_window_anomalies(
    db_session: Session,
) -> None:
    """moves_cache must reach detect_window_anomalies unchanged — this is
    the plumbing that lets report_tasks.py's fan-out share one
    compute_global_moves() call across a whole batch."""
    cache: MovesCache = {}
    with (
        patch("app.services.report_generator.compute_portfolio", return_value=_portfolio_snap()),
        patch("app.services.report_generator.load_news_window", return_value=[]),
        patch("app.services.report_generator.detect_macro_signals", return_value=_quiet_signals()),
        patch(
            "app.services.report_generator.detect_window_anomalies", return_value=([], 0)
        ) as mock_detect,
        patch("app.services.report_generator._call_llm"),
    ):
        rg.generate_report(db_session, user_id=_USER, report_date=_TODAY, moves_cache=cache)

    assert mock_detect.call_args.args[-1] is cache


# ---------------------------------------------------------------------------
# A4 personalized assembly wiring (issue #128, design doc §6)
#
# The assembly pass resolves its OWN _call_llm binding (app.services.
# report_assembly), so these tests patch it separately from
# report_generator's — the same module-boundary reason the L1/L2 fixtures
# above exist.
# ---------------------------------------------------------------------------

_FAKE_ASSEMBLED_BODY = (
    "## §2 Macro Signals\n\nRates repriced; the portfolio's US equity sleeve is exposed.\n\n"
    "## §3 Holdings Analysis\n\nNVIDIA, the heaviest position, rose on an earnings beat. "
    "[Established]\n\n"
    "## §4 Risk Radar\n\nNVDA — earnings beat drove the move [Established]\n\n" + _PASS2_FILLER
)


def _assembly_ready_patches(**setting_overrides: object) -> list[object]:
    """The normal path, plus a real L1 result so there IS shared intel to
    assemble from, plus the requested A4 settings."""
    settings = get_settings()
    patches = _normal_path_patches()
    patches.append(
        patch(
            "app.services.report_generator.resolve_global_moves",
            return_value=({"NVDA": _day_move("NVDA")}, 1),
        )
    )
    for name, value in setting_overrides.items():
        patches.append(patch.object(settings, name, value))
    return patches


def test_generate_report_uses_pass2_when_shared_compute_is_disabled(
    db_session: Session,
) -> None:
    """`SHARED_COMPUTE_ENABLED=false` is the production default and must be
    byte-for-byte the pre-A4 pipeline (design doc §6.5): the assembly pass is
    never even constructed."""
    with contextlib.ExitStack() as stack:
        for p in _assembly_ready_patches(
            SHARED_COMPUTE_ENABLED=False, ASSEMBLY_LLM_MODEL="cheap/model"
        ):
            stack.enter_context(p)  # type: ignore[arg-type]
        mock_assembly = stack.enter_context(patch("app.services.report_assembly._call_llm"))
        report = rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)

    mock_assembly.assert_not_called()
    assert report.report_inputs is not None
    assert report.report_inputs["body_source"] == "pass2"
    assert report.report_inputs["pass2_raw"]
    assert not report.report_inputs["assembly_raw"]


def test_generate_report_assembles_from_shared_intel_when_enabled(
    db_session: Session,
) -> None:
    """The A4 architecture switch: the body comes from the assembly pass over
    pre-computed L1/L2 intel, and the giant Pass 2 call does not happen at
    all — that call not happening IS the cost reduction."""
    with contextlib.ExitStack() as stack:
        for p in _assembly_ready_patches(
            SHARED_COMPUTE_ENABLED=True, ASSEMBLY_LLM_MODEL="cheap/model"
        ):
            stack.enter_context(p)  # type: ignore[arg-type]
        mock_assembly = stack.enter_context(
            patch("app.services.report_assembly._call_llm", return_value=_FAKE_ASSEMBLED_BODY)
        )
        report = rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)

    mock_assembly.assert_called_once()
    assert mock_assembly.call_args.kwargs["with_holdings"] is True, (
        "the assembly payload carries portfolio weights — deny must stay enforced"
    )
    assert report.status == "success"
    assert report.report_inputs is not None
    assert report.report_inputs["body_source"] == "assembly"
    assert report.report_inputs["assembly_raw"] == _FAKE_ASSEMBLED_BODY
    assert report.report_inputs["assembly_model"] == "cheap/model"
    # Pass 2 never ran, so it left no body behind.
    assert not report.report_inputs["pass2_raw"]
    assert report.report_md is not None
    assert "heaviest position" in report.report_md


def test_generate_report_assembly_path_also_gets_investor_preferences(
    db_session: Session,
) -> None:
    """PR #212 review bug finding: the original implementation only wired
    investor-preference injection into the Pass 2 fallback branch, so an
    assembled report (SHARED_COMPUTE_ENABLED=True, the intended A4 cost
    architecture) silently ignored the user's questionnaire AND recorded no
    audit snapshot in report_inputs, even though a row existed."""
    # `_seed_test_user` (module-level autouse fixture) already created the
    # `users` row for _USER.
    from app.models.user_investment_context import UserInvestmentContext

    db_session.add(
        UserInvestmentContext(
            user_id=_USER,
            questionnaire={
                "asset_scale": "500K_2M",
                "markets": ["US"],
                "style": "GROWTH",
                "horizon": "LONG",
                "risk_appetite": "AGGRESSIVE",
                "sectors_of_interest": ["Technology"],
                "objective": "GROWTH",
                "intel_focus": "GEOPOLITICS",
            },
            questionnaire_version="v1",
            free_text="Concentrated in AI infrastructure names on purpose.",
        )
    )
    db_session.flush()

    with contextlib.ExitStack() as stack:
        for p in _assembly_ready_patches(
            SHARED_COMPUTE_ENABLED=True, ASSEMBLY_LLM_MODEL="cheap/model"
        ):
            stack.enter_context(p)  # type: ignore[arg-type]
        mock_assembly = stack.enter_context(
            patch("app.services.report_assembly._call_llm", return_value=_FAKE_ASSEMBLED_BODY)
        )
        report = rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)

    assert report.report_inputs is not None
    assert report.report_inputs["body_source"] == "assembly"
    # The snapshot must be set regardless of body source.
    snap = report.report_inputs["investor_questionnaire_snapshot"]
    assert snap["intel_focus"] == "GEOPOLITICS"
    # The assembly prompt itself must carry the INVESTOR PREFERENCES block —
    # `prompt` is run_assembly_pass's 4th positional arg to _call_llm.
    assembly_prompt = mock_assembly.call_args.args[3]
    assert "INVESTOR PREFERENCES" in assembly_prompt
    assert "geopolitical developments" in assembly_prompt
    assert "Reader locale: zh" in assembly_prompt


def test_generate_report_assembly_keeps_the_code_built_sections(
    db_session: Session,
) -> None:
    """Design doc §6.3 contract table: §1/§4.2/§4.4 stay code-built. The
    assembled body is a drop-in replacement for Pass 2's, so the same
    injection points must still fire."""
    with contextlib.ExitStack() as stack:
        for p in _assembly_ready_patches(
            SHARED_COMPUTE_ENABLED=True, ASSEMBLY_LLM_MODEL="cheap/model"
        ):
            stack.enter_context(p)  # type: ignore[arg-type]
        stack.enter_context(
            patch("app.services.report_assembly._call_llm", return_value=_FAKE_ASSEMBLED_BODY)
        )
        report = rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)

    assert report.report_md is not None
    assert "§1" in report.report_md
    assert "Data window" in report.report_md
    # The single footer disclaimer, unchanged.
    assert "Data Sources & Disclaimer" in report.report_md


def test_generate_report_falls_back_to_pass2_when_shared_caches_are_empty(
    db_session: Session,
) -> None:
    """Cold start / capped / every candidate blocked: there is nothing to
    assemble, so the run degrades to Pass 2 rather than shipping a hollow
    report (design doc §6.3)."""
    with contextlib.ExitStack() as stack:
        for p in _assembly_ready_patches(
            SHARED_COMPUTE_ENABLED=True, ASSEMBLY_LLM_MODEL="cheap/model"
        ):
            stack.enter_context(p)  # type: ignore[arg-type]
        # No L1 and no L2 intel this run.
        stack.enter_context(
            patch("app.services.report_generator.get_l1_intel_batch", return_value={})
        )
        stack.enter_context(
            patch("app.services.report_generator.get_l2_intel_batch", return_value={})
        )
        mock_assembly = stack.enter_context(patch("app.services.report_assembly._call_llm"))
        report = rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)

    mock_assembly.assert_not_called()
    assert report.status == "success"
    assert report.report_inputs is not None
    assert report.report_inputs["body_source"] == "pass2"


def test_generate_report_falls_back_to_pass2_when_assembled_body_is_truncated(
    db_session: Session,
) -> None:
    """A provider can return a short/mangled 200. Pass 2 raises on that so
    Celery retries; the assembly path instead degrades to Pass 2 in the same
    run — the promise is that enabling A4 can never produce a WORSE report
    than not enabling it."""
    with contextlib.ExitStack() as stack:
        for p in _assembly_ready_patches(
            SHARED_COMPUTE_ENABLED=True, ASSEMBLY_LLM_MODEL="cheap/model"
        ):
            stack.enter_context(p)  # type: ignore[arg-type]
        mock_assembly = stack.enter_context(
            patch("app.services.report_assembly._call_llm", return_value="## §2 too short")
        )
        report = rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)

    mock_assembly.assert_called_once()
    assert report.status == "success"
    assert report.report_inputs is not None
    assert report.report_inputs["body_source"] == "pass2"
    assert report.report_inputs["pass2_raw"]
    assert report.report_md is not None
    assert "NVIDIA up 9%" in report.report_md


def test_generate_report_falls_back_to_pass2_when_the_assembly_call_raises(
    db_session: Session,
) -> None:
    with contextlib.ExitStack() as stack:
        for p in _assembly_ready_patches(
            SHARED_COMPUTE_ENABLED=True, ASSEMBLY_LLM_MODEL="cheap/model"
        ):
            stack.enter_context(p)  # type: ignore[arg-type]
        stack.enter_context(
            patch("app.services.report_assembly._call_llm", side_effect=RuntimeError("provider"))
        )
        report = rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)

    assert report.status == "success"
    assert report.report_inputs is not None
    assert report.report_inputs["body_source"] == "pass2"


def test_generate_report_scans_the_assembled_body_for_compliance(
    db_session: Session,
) -> None:
    """Compliance > everything: the assembled body goes through the identical
    Layer-4 backstop, so a forbidden phrase holds the report as needs_review
    and it is never emailed."""
    bad_body = (
        "## §2 Macro Signals\n\nRates moved.\n\n"
        "## §3 Holdings Analysis\n\nWe recommend you buy more NVDA immediately.\n\n"
        "## §4 Risk Radar\n\nNVDA — moved [Established]\n\n" + _PASS2_FILLER
    )
    with contextlib.ExitStack() as stack:
        for p in _assembly_ready_patches(
            SHARED_COMPUTE_ENABLED=True, ASSEMBLY_LLM_MODEL="cheap/model"
        ):
            stack.enter_context(p)  # type: ignore[arg-type]
        stack.enter_context(patch("app.services.report_assembly._call_llm", return_value=bad_body))
        mock_email = stack.enter_context(
            patch("app.services.report_generator.send_report_email", return_value=True)
        )
        report = rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)

    assert report.status == "needs_review"
    assert report.report_inputs is not None
    assert report.report_inputs["body_source"] == "assembly"
    mock_email.assert_not_called()


# --- Shadow comparison (design doc §6.3.1) ---------------------------------


def test_generate_report_shadow_models_are_stored_but_never_shipped(
    db_session: Session,
) -> None:
    """One round yields both comparisons the design asks for: the shipped
    Pass 2 body vs each assembled body (architecture), and the listed models
    against each other (selection). The shadow output must not touch what
    the user receives."""
    shadow_body = "## §2 shadow\n\n## §3 shadow\n\n## §4 shadow\n\n" + _PASS2_FILLER
    with contextlib.ExitStack() as stack:
        for p in _assembly_ready_patches(
            SHARED_COMPUTE_ENABLED=False,
            ASSEMBLY_SHADOW_MODELS="cheap/model, mid/model",
        ):
            stack.enter_context(p)  # type: ignore[arg-type]
        mock_assembly = stack.enter_context(
            patch("app.services.report_assembly._call_llm", return_value=shadow_body)
        )
        report = rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)

    assert mock_assembly.call_count == 2, "one assembly pass per shadow model"
    assert report.report_inputs is not None
    shadow = report.report_inputs["assembly_shadow"]
    assert set(shadow) == {"cheap/model", "mid/model"}
    assert shadow["cheap/model"]["raw"] == shadow_body
    # The shipped report is untouched by the shadow run.
    assert report.report_inputs["body_source"] == "pass2"
    assert report.report_md is not None
    assert "shadow" not in report.report_md
    assert "NVIDIA up 9%" in report.report_md


def test_generate_report_shadow_failure_never_fails_the_report(
    db_session: Session,
) -> None:
    """A comparison harness must not be able to break the thing it measures."""
    with contextlib.ExitStack() as stack:
        for p in _assembly_ready_patches(
            SHARED_COMPUTE_ENABLED=False, ASSEMBLY_SHADOW_MODELS="broken/model"
        ):
            stack.enter_context(p)  # type: ignore[arg-type]
        stack.enter_context(
            patch("app.services.report_assembly._call_llm", side_effect=RuntimeError("provider"))
        )
        report = rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)

    assert report.status == "success"
    assert report.report_inputs is not None
    assert "error" in report.report_inputs["assembly_shadow"]["broken/model"]


def test_generate_report_shadow_prompt_construction_failure_never_fails_the_report(
    db_session: Session,
) -> None:
    """Round 2 review finding (PR #163): `_run_shadow_assembly`'s own
    try/except only wraps the per-model `run_assembly_pass` call — the ONE
    prompt-build call before that loop (`_assembly_prompt_from_ctx`) was
    unguarded. A defect there would propagate past a Pass 2 body that
    already succeeded and flip the whole report to 'failed', which is
    exactly the "measurement breaks what it measures" contract this
    harness exists to avoid."""
    with contextlib.ExitStack() as stack:
        for p in _assembly_ready_patches(
            SHARED_COMPUTE_ENABLED=False, ASSEMBLY_SHADOW_MODELS="broken/model"
        ):
            stack.enter_context(p)  # type: ignore[arg-type]
        stack.enter_context(
            patch(
                "app.services.report_generator.build_assembly_prompt",
                side_effect=RuntimeError("prompt construction blew up"),
            )
        )
        report = rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)

    assert report.status == "success"
    assert report.report_inputs is not None
    assert report.report_inputs["body_source"] == "pass2"
    assert report.report_inputs["pass2_raw"]
    assert report.report_inputs["assembly_shadow"] == {}


def test_generate_report_shadow_is_skipped_when_there_is_no_shared_intel(
    db_session: Session,
) -> None:
    """Nothing to compare against — spending two model calls on an empty
    assembly prompt would just bill for noise."""
    with contextlib.ExitStack() as stack:
        for p in _assembly_ready_patches(ASSEMBLY_SHADOW_MODELS="cheap/model"):
            stack.enter_context(p)  # type: ignore[arg-type]
        stack.enter_context(
            patch("app.services.report_generator.get_l1_intel_batch", return_value={})
        )
        stack.enter_context(
            patch("app.services.report_generator.get_l2_intel_batch", return_value={})
        )
        mock_assembly = stack.enter_context(patch("app.services.report_assembly._call_llm"))
        report = rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)

    mock_assembly.assert_not_called()
    assert report.report_inputs is not None
    assert report.report_inputs["assembly_shadow"] == {}


# --- Re-render contract (#6) with an assembled body ------------------------


def test_regenerate_render_rebuilds_an_assembled_report_without_llm_calls(
    db_session: Session,
) -> None:
    """Design doc §6.3 contract: `mode=render` stays token-free (except
    translation) for an assembly-sourced report too — the stored
    `assembly_raw` is the body it rebuilds from."""
    with contextlib.ExitStack() as stack:
        for p in _assembly_ready_patches(
            SHARED_COMPUTE_ENABLED=True, ASSEMBLY_LLM_MODEL="cheap/model"
        ):
            stack.enter_context(p)  # type: ignore[arg-type]
        stack.enter_context(
            patch("app.services.report_assembly._call_llm", return_value=_FAKE_ASSEMBLED_BODY)
        )
        report = rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)

    assert report.report_inputs is not None
    assert report.report_inputs["body_source"] == "assembly"

    with (
        patch("app.services.report_generator._call_llm") as mock_pass2,
        patch("app.services.report_assembly._call_llm") as mock_assembly,
    ):
        rebuilt = rg.regenerate_report(db_session, report.id, user_id=_USER, mode="render")

    mock_pass2.assert_not_called()
    mock_assembly.assert_not_called()
    assert rebuilt.report_md is not None
    assert "heaviest position" in rebuilt.report_md


def test_regenerate_analyze_reruns_the_pass_that_wrote_the_body(
    db_session: Session,
) -> None:
    """`analyze` means "re-run this report's body pass". For an
    assembly-sourced report that is the assembly pass, not Pass 2.

    Re-running Pass 2 here would not just be the wrong pass — it would leave
    `assembly_raw` holding the SUPERSEDED body while `report_md` showed the
    new one, so a later `mode=render` would silently rebuild the old report.
    """
    with contextlib.ExitStack() as stack:
        for p in _assembly_ready_patches(
            SHARED_COMPUTE_ENABLED=True, ASSEMBLY_LLM_MODEL="cheap/model"
        ):
            stack.enter_context(p)  # type: ignore[arg-type]
        stack.enter_context(
            patch("app.services.report_assembly._call_llm", return_value=_FAKE_ASSEMBLED_BODY)
        )
        report = rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)

    reanalyzed = (
        "## §2 Macro Signals\n\nReanalyzed macro read.\n\n"
        "## §3 Holdings Analysis\n\nReanalyzed holdings read. [Probable]\n\n"
        "## §4 Risk Radar\n\nNVDA — reanalyzed [Probable]\n\n" + _PASS2_FILLER
    )
    with (
        patch("app.services.report_generator.compute_portfolio", return_value=_portfolio_snap()),
        patch("app.services.report_generator._call_llm") as mock_pass2,
        patch("app.services.report_assembly._call_llm", return_value=reanalyzed) as mock_assembly,
    ):
        updated = rg.regenerate_report(db_session, report.id, user_id=_USER, mode="analyze")

    mock_pass2.assert_not_called()
    mock_assembly.assert_called_once()
    assert updated.report_md is not None
    assert "Reanalyzed holdings read" in updated.report_md
    assert updated.report_inputs is not None
    assert updated.report_inputs["assembly_raw"] == reanalyzed

    # And the stored body is now genuinely the one that shipped: a follow-up
    # render must not resurrect the superseded text.
    with (
        patch("app.services.report_generator._call_llm"),
        patch("app.services.report_assembly._call_llm"),
    ):
        rerendered = rg.regenerate_report(db_session, report.id, user_id=_USER, mode="render")

    assert rerendered.report_md is not None
    assert "Reanalyzed holdings read" in rerendered.report_md
    assert "heaviest position" not in rerendered.report_md


def test_regenerate_analyze_recomputes_macro_event_exposure_from_fresh_portfolio(
    db_session: Session,
) -> None:
    """Round 2 review finding (PR #163): `analyze` already refreshes
    `portfolio` from the live DB (so a holdings edit between generation and
    regenerate is picked up), but was replaying the STORED
    `macro_event_exposure` — the intersection computed against the
    ORIGINAL `by_asset_class`. If a confirm between generation and
    regenerate drops the only class an event bore on, the stale exposure
    would still tell the model "your exposure: <class you no longer
    hold>". Exposure is cheap set arithmetic (`user_event_exposure`, zero
    LLM calls) and must be recomputed against the SAME fresh portfolio the
    prompt is otherwise built from.
    """
    with contextlib.ExitStack() as stack:
        for p in _assembly_ready_patches(
            SHARED_COMPUTE_ENABLED=True, ASSEMBLY_LLM_MODEL="cheap/model"
        ):
            stack.enter_context(p)  # type: ignore[arg-type]
        stack.enter_context(
            patch("app.services.report_assembly._call_llm", return_value=_FAKE_ASSEMBLED_BODY)
        )
        report = rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)

    assert report.report_inputs is not None
    assert report.report_inputs["body_source"] == "assembly"

    # Simulate the original exposure having been computed against a
    # portfolio that held STOCK — matches _portfolio_snap()'s by_asset_class.
    stale_inputs = dict(report.report_inputs)
    stale_inputs["macro_event_intel"] = {
        "theme:x": {
            "analysis": "STALE_EVENT_MARKER touches the STOCK sleeve.",
            "affected_asset_classes": ["STOCK"],
            "affected_sectors": [],
        }
    }
    stale_inputs["macro_event_exposure"] = {"theme:x": ["STOCK"]}
    report.report_inputs = stale_inputs
    db_session.commit()

    # A holdings edit since generation moved this portfolio entirely out of
    # STOCK — the event no longer bears on anything this user holds.
    fresh_snap = _portfolio_snap()
    fresh_snap.by_asset_class = {"EQUITY_US_BROAD": Decimal("10000")}

    with (
        patch("app.services.report_generator.compute_portfolio", return_value=fresh_snap),
        patch(
            "app.services.report_assembly._call_llm", return_value=_FAKE_ASSEMBLED_BODY
        ) as mock_assembly,
    ):
        rg.regenerate_report(db_session, report.id, user_id=_USER, mode="analyze")

    sent_prompt = mock_assembly.call_args.args[3]
    assert "theme:x" not in sent_prompt, (
        "a dropped asset class must remove the event from the prompt, "
        "not carry forward the exposure computed against the OLD portfolio"
    )


def test_regenerate_render_still_works_for_a_pre_a4_report(db_session: Session) -> None:
    """Historical rows have no `assembly_raw` key at all (`ReportInputsDict`
    is total=False). They must keep rebuilding from `pass2_raw` exactly as
    before — design doc §6.4's stored-structure risk."""
    with contextlib.ExitStack() as stack:
        for p in _normal_path_patches():
            stack.enter_context(p)  # type: ignore[arg-type]
        report = rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)

    # Simulate a row written before A4 existed.
    report.report_inputs = {
        k: v
        for k, v in dict(report.report_inputs or {}).items()
        if not k.startswith("assembly_") and k != "body_source"
    }
    db_session.commit()

    with (
        patch("app.services.report_generator._call_llm") as mock_pass2,
    ):
        rebuilt = rg.regenerate_report(db_session, report.id, user_id=_USER, mode="render")

    mock_pass2.assert_not_called()
    assert rebuilt.report_md is not None
    assert "NVIDIA up 9%" in rebuilt.report_md


# ---------------------------------------------------------------------------
# L3 day-level cross-name synthesis wiring (issue #128 quality gate — design
# doc §6.7 item 1)
# ---------------------------------------------------------------------------


_L3_CLUSTERS = [
    {
        "identifiers": ["NVDA", "AAPL"],
        "mechanism": "ai_capex_stack",
        "summary": "Accelerator demand set the tape while the long end cut the other way.",
        "confidence": "Probable",
    }
]


def test_generate_report_runs_the_synthesis_after_l1_rows_exist(
    db_session: Session,
) -> None:
    """Ordering is load-bearing, not incidental: the synthesis reads the day's
    L1 rows out of `ticker_intel`, so running it before `get_l1_intel_batch`
    has written this user's rows would analyze a day that is missing exactly
    the names this report is about."""
    call_order: list[str] = []

    def _l1(*args: object, **kwargs: object) -> dict[str, str]:
        call_order.append("l1")
        return {"NVDA": "It rose. [Probable]"}

    def _l3(*args: object, **kwargs: object) -> list[dict[str, Any]]:
        call_order.append("l3")
        return list(_L3_CLUSTERS)

    with contextlib.ExitStack() as stack:
        for p in _assembly_ready_patches(
            SHARED_COMPUTE_ENABLED=True, ASSEMBLY_LLM_MODEL="cheap/model"
        ):
            stack.enter_context(p)  # type: ignore[arg-type]
        stack.enter_context(
            patch("app.services.report_generator.get_l1_intel_batch", side_effect=_l1)
        )
        stack.enter_context(
            patch("app.services.report_generator.get_day_synthesis", side_effect=_l3)
        )
        stack.enter_context(
            patch("app.services.report_assembly._call_llm", return_value=_FAKE_ASSEMBLED_BODY)
        )
        rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)

    assert call_order == ["l1", "l3"]


def test_generate_report_stores_only_clusters_touching_this_users_holdings(
    db_session: Session,
) -> None:
    """The per-user narrowing has to happen at the boundary where the global
    clusters enter this report, not later — `report_inputs` is stored, read
    back by regenerate, and re-rendered, so an unnarrowed cluster there would
    outlive any downstream filtering."""
    with contextlib.ExitStack() as stack:
        for p in _assembly_ready_patches(
            SHARED_COMPUTE_ENABLED=True, ASSEMBLY_LLM_MODEL="cheap/model"
        ):
            stack.enter_context(p)  # type: ignore[arg-type]
        stack.enter_context(
            patch(
                "app.services.report_generator.get_l1_intel_batch",
                return_value={"NVDA": "It rose. [Probable]"},
            )
        )
        stack.enter_context(
            patch(
                "app.services.report_generator.get_day_synthesis",
                return_value=[
                    {
                        "identifiers": ["NVDA", "SGOL"],
                        "mechanism": "safe_haven",
                        "summary": "A haven bid ran through the group.",
                        "confidence": "Probable",
                    }
                ],
            )
        )
        stack.enter_context(
            patch("app.services.report_assembly._call_llm", return_value=_FAKE_ASSEMBLED_BODY)
        )
        report = rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)

    assert report.report_inputs is not None
    # Only NVDA has L1 intel for this user, so the two-name floor drops the
    # cluster entirely rather than storing a one-name "group".
    assert report.report_inputs["cross_name_intel"] == []


def test_synthesis_failure_never_fails_the_report(db_session: Session) -> None:
    """Same degradation contract every shared layer answers to: a cross-name
    conclusion is an enrichment, so losing it costs a sentence, never a
    report."""
    with contextlib.ExitStack() as stack:
        for p in _assembly_ready_patches(
            SHARED_COMPUTE_ENABLED=True, ASSEMBLY_LLM_MODEL="cheap/model"
        ):
            stack.enter_context(p)  # type: ignore[arg-type]
        stack.enter_context(
            patch(
                "app.services.report_generator.get_day_synthesis",
                side_effect=RuntimeError("provider exploded"),
            )
        )
        stack.enter_context(
            patch("app.services.report_assembly._call_llm", return_value=_FAKE_ASSEMBLED_BODY)
        )
        report = rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)

    assert report.status == "success"
    assert report.report_inputs is not None
    assert report.report_inputs["cross_name_intel"] == []


def test_big_mover_without_a_headline_gets_the_leftover_tavily_budget(
    db_session: Session,
) -> None:
    """Design doc §6.7 item 3. An L1 candidate that moved hard and recalled
    NOTHING is the case where a [Speculative] shrug is guaranteed — and it is
    exactly the case worth spending an unused search on. The pre-existing
    targeted search covers ANOMALIES only, so a weight/L2-class extra (a 22%
    holding that did not cross its threshold) could never reach it."""
    with contextlib.ExitStack() as stack:
        for p in _assembly_ready_patches():
            stack.enter_context(p)  # type: ignore[arg-type]
        stack.enter_context(
            patch(
                "app.services.report_generator.resolve_global_moves",
                return_value=({"NVDA": _day_move("NVDA", net_pct=0.09)}, 1),
            )
        )
        # Nothing recalled for NVDA from the captured corpus.
        stack.enter_context(
            patch("app.services.report_generator.recall_holding_news", return_value={})
        )
        captured_l1_facts: dict[str, object] = {}

        def _capture_facts(*args: object, **kwargs: object) -> dict[str, object]:
            captured_l1_facts["headlines"] = args[2]
            return {}

        stack.enter_context(
            patch("app.services.report_generator.build_l1_facts", side_effect=_capture_facts)
        )

        def _echo_search(
            _session: object, queries: list[str], *args: object, **kwargs: object
        ) -> list[dict[str, str]]:
            # `_run_tavily_search` echoes the query on every result — the
            # caller keys results back to their identifier by exact match, so
            # a stub that invents its own query string would test nothing.
            return [
                {"query": q, "title": "NVDA guides higher", "url": "u"}
                for q in queries
                if "NVDA" in q
            ]

        stack.enter_context(
            patch("app.services.report_generator._run_tavily_search", side_effect=_echo_search)
        )
        rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)

    headlines = captured_l1_facts["headlines"]
    assert isinstance(headlines, dict)
    assert any("guides higher" in h for h in headlines.get("NVDA", [])), (
        "a targeted-search title must reach L1, not only Pass 2's holding_news"
    )


def test_leftover_tavily_topup_respects_fair_share_budget(db_session: Session) -> None:
    """PR #167 review round 3, suggestion: the leftover-budget top-up used
    `settings.TAVILY_DAILY_BUDGET - _tavily_used_today(...)` directly — the
    FULL remaining daily budget — with no `fair_share_budget(remaining,
    users_remaining)` division, unlike every other shared-budget consumer in
    this same function (L1's own analyses, L2's, L3's synthesis). In a
    fan-out the first `active_user_ids` user could therefore spend the
    day's entire remaining Tavily budget on its own top-up searches before
    any later user gets a turn — the exact sequential-starvation shape A4's
    `fair_share_budget` exists to close everywhere else."""
    with contextlib.ExitStack() as stack:
        for p in _assembly_ready_patches():
            stack.enter_context(p)  # type: ignore[arg-type]
        stack.enter_context(
            patch(
                "app.services.report_generator.resolve_global_moves",
                return_value=({"NVDA": _day_move("NVDA", net_pct=0.09)}, 1),
            )
        )
        stack.enter_context(
            patch("app.services.report_generator.recall_holding_news", return_value={})
        )
        stack.enter_context(
            patch("app.services.report_generator._tavily_used_today", return_value=0)
        )
        settings = get_settings()
        stack.enter_context(patch.object(settings, "TAVILY_DAILY_BUDGET", 9))
        search_mock = stack.enter_context(
            patch("app.services.report_generator._run_tavily_search", return_value=[])
        )
        rg.generate_report(db_session, user_id=_USER, report_date=_TODAY, users_remaining=3)

    # `_anomaly()` (from `_normal_path_patches`, via `detect_window_anomalies`)
    # also fires the PRE-EXISTING targeted-anomaly search for NVDA — a
    # different, out-of-scope-for-this-PR call that builds a DIFFERENTLY
    # SHAPED query ("NVIDIA NVDA stock news catalyst", via the anomaly's
    # name+identifier) than the top-up's own ("NVDA stock news catalyst",
    # identifier alone) — matched on the exact top-up format so this test
    # isolates only the call under fix, not the older one.
    topup_calls = [c for c in search_mock.call_args_list if "NVDA stock news catalyst" in c.args[1]]
    assert topup_calls, "expected the leftover top-up search to fire for NVDA's uncovered move"
    # fair_share_budget(9, 3) == 3, not the full remaining 9.
    assert topup_calls[0].kwargs["budget"] == 3


def test_targeted_search_budget_respects_fair_share_budget(db_session: Session) -> None:
    """PR #168 round 2 review, suggestion: the combined anomaly+weight
    targeted-search budget (`targeted_budget`, the call feeding both
    `_targeted_anomaly_queries` and `_targeted_weight_queries` results) used
    the FULL remaining daily Tavily budget with no `fair_share_budget`
    division — the same sequential-starvation shape
    `test_leftover_tavily_topup_respects_fair_share_budget` already locks for
    the L1 leftover top-up, reopened here because this call sits earlier in
    `generate_report` and runs first in a fan-out, so it could exhaust the
    day's budget before any later consumer (including that very top-up) gets
    a turn."""
    with contextlib.ExitStack() as stack:
        for p in _normal_path_patches():
            stack.enter_context(p)  # type: ignore[arg-type]
        stack.enter_context(
            patch("app.services.report_generator._tavily_used_today", return_value=0)
        )
        settings = get_settings()
        stack.enter_context(patch.object(settings, "TAVILY_DAILY_BUDGET", 9))
        search_mock = stack.enter_context(
            patch("app.services.report_generator._run_tavily_search", return_value=[])
        )
        rg.generate_report(db_session, user_id=_USER, report_date=_TODAY, users_remaining=3)

    # `_anomaly()` (from `_normal_path_patches`) has no recalled window news,
    # so the targeted-anomaly query fires and is the one call this test's
    # budget assertion targets — the ONLY `_run_tavily_search` call in this
    # path besides Pass 1's own macro-theme search (a separate, earlier call
    # with a differently-shaped query list, unaffected by this fix).
    targeted_calls = [
        c for c in search_mock.call_args_list if any("NVIDIA NVDA" in q for q in c.args[1])
    ]
    assert targeted_calls, "expected the targeted-anomaly search to fire for NVDA"
    # fair_share_budget(9, 3) == 3, not the full remaining 9.
    assert targeted_calls[0].kwargs["budget"] == 3


def test_leftover_search_is_skipped_when_the_candidate_already_has_a_headline(
    db_session: Session,
) -> None:
    """The budget is scarce and shared across the fan-out — a name that
    already recalled something must not spend it."""
    with contextlib.ExitStack() as stack:
        for p in _assembly_ready_patches():
            stack.enter_context(p)  # type: ignore[arg-type]
        stack.enter_context(
            patch(
                "app.services.report_generator.resolve_global_moves",
                return_value=({"NVDA": _day_move("NVDA", net_pct=0.09)}, 1),
            )
        )
        stack.enter_context(
            patch(
                "app.services.report_generator.recall_holding_news",
                return_value={"NVDA": [_news_item("NVDA beats")]},
            )
        )
        search = stack.enter_context(
            patch("app.services.report_generator._run_tavily_search", return_value=[])
        )
        rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)

    l1_queries = [
        c for c in search.call_args_list if any("NVDA" in q for q in (c.args[1] if c.args else []))
    ]
    assert not l1_queries, "an already-covered candidate must not buy a search"


def test_pass2_prompt_never_receives_cross_name_intel(db_session: Session) -> None:
    """The synthesis is A4's input. Feeding it to Pass 2 as well would make
    the shadow comparison meaningless — the two architectures would no longer
    be reading different inputs (design doc §6.3.1)."""
    with contextlib.ExitStack() as stack:
        for p in _assembly_ready_patches(SHARED_COMPUTE_ENABLED=False):
            stack.enter_context(p)  # type: ignore[arg-type]
        stack.enter_context(
            patch("app.services.report_generator.get_day_synthesis", return_value=_L3_CLUSTERS)
        )
        report = rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)

    assert report.report_inputs is not None
    assert "ai_capex_stack" not in report.report_inputs["pass2_prompt"]


def test_regenerate_analyze_persists_the_renarrowed_cross_name_clusters(
    db_session: Session,
) -> None:
    """PR #167 review round 3, suggestion: `analyze` already re-narrows
    stored clusters with `clusters_for_user` against the FRESH portfolio (a
    holdings edit since generation must not leave a stale cluster naming a
    position no longer held — same correction PR #163 made for
    `macro_event_exposure`), and feeds the result into the assembly prompt.
    But the re-narrowed result was never written back to
    `report_inputs["cross_name_intel"]` — only the prompt saw it. The stored
    field is a PER-USER PROJECTION (already narrowed once at generation via
    `clusters_for_user`), exactly like `macro_event_exposure`, not shared
    mechanism text that must stay untouched; leaving it stale means a later
    reader of `report_inputs["cross_name_intel"]` sees identifiers the fresh
    book no longer holds, even though the body just generated does not.
    """
    with contextlib.ExitStack() as stack:
        for p in _assembly_ready_patches(
            SHARED_COMPUTE_ENABLED=True, ASSEMBLY_LLM_MODEL="cheap/model"
        ):
            stack.enter_context(p)  # type: ignore[arg-type]
        stack.enter_context(
            patch("app.services.report_assembly._call_llm", return_value=_FAKE_ASSEMBLED_BODY)
        )
        report = rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)

    assert report.report_inputs is not None

    # Simulate a stored cluster naming two identifiers, both present in the
    # generation-time L1 key set.
    stale_inputs = dict(report.report_inputs)
    stale_inputs["ticker_intel"] = {
        "NVDA": "NVDA rose on an earnings beat. [Established]",
        "AAPL": "AAPL tracked the broader move. [Probable]",
    }
    stale_inputs["cross_name_intel"] = [
        {
            "identifiers": ["NVDA", "AAPL"],
            "mechanism": "discount_rate",
            "summary": "NVDA and AAPL moved together on the rate channel.",
            "confidence": "Probable",
        }
    ]
    report.report_inputs = stale_inputs
    db_session.commit()

    # A holdings edit since generation drops AAPL entirely — the fresh
    # portfolio holds only NVDA. Re-narrowing a 2-identifier cluster against
    # a 1-identifier held set must drop the cluster (below the 2-name floor).
    fresh_snap = _portfolio_snap()
    fresh_snap.holdings[0].ticker = "NVDA"
    fresh_snap.holdings[0].name = "NVIDIA"

    with (
        patch("app.services.report_generator.compute_portfolio", return_value=fresh_snap),
        patch("app.services.report_assembly._call_llm", return_value=_FAKE_ASSEMBLED_BODY),
    ):
        rebuilt = rg.regenerate_report(db_session, report.id, user_id=_USER, mode="analyze")

    assert rebuilt.report_inputs is not None
    assert rebuilt.report_inputs["cross_name_intel"] == [], (
        "the stored field must reflect the re-narrowed (now empty) cluster set "
        "actually sent to the prompt, not the stale 2-identifier value from "
        "before the holdings edit"
    )


# ---------------------------------------------------------------------------
# B6 investor preferences (issue #129 checkpoint B6, decision point 6)
# ---------------------------------------------------------------------------


def _seed_investment_context(
    session: Session, user_id: uuid.UUID, *, locale: str = "zh", intel_focus: str = "GEOPOLITICS"
) -> None:
    from app.models.user import User
    from app.models.user_investment_context import UserInvestmentContext

    # The module-level `_seed_test_user` autouse fixture already creates a
    # `users` row for _USER (issue #129 B7); update it in place rather than
    # inserting a second row under the same primary key.
    existing = session.get(User, user_id)
    if existing is not None:
        existing.locale = locale
    else:
        session.add(
            User(
                id=user_id,
                auth_provider="supabase",
                auth_subject=f"sub-{user_id}",
                email=f"{user_id}@example.com",
                status="active",
                locale=locale,
                base_currency="USD",
                report_cadence="mwf",
            )
        )
    session.add(
        UserInvestmentContext(
            user_id=user_id,
            questionnaire={
                "asset_scale": "500K_2M",
                "markets": ["US"],
                "style": "GROWTH",
                "horizon": "LONG",
                "risk_appetite": "AGGRESSIVE",
                "sectors_of_interest": ["Technology"],
                "objective": "GROWTH",
                "intel_focus": intel_focus,
            },
            questionnaire_version="v1",
            free_text="Concentrated in AI infrastructure names on purpose.",
        )
    )
    session.flush()


def test_generate_report_pass2_prompt_carries_locale_and_intel_focus(
    db_session: Session,
) -> None:
    _seed_investment_context(db_session, _USER, locale="zh", intel_focus="GEOPOLITICS")
    captured: dict[str, str] = {}

    def _capture_pass2_llm(
        client: object,
        model: str,
        system: str,
        user: str,
        *,
        with_holdings: bool = False,
        **kw: object,
    ) -> str:
        if with_holdings:
            captured["pass2_user"] = user
            return _FAKE_LLM_PASS2
        return _FAKE_LLM_PASS1

    with contextlib.ExitStack() as stack:
        for p in _normal_path_patches():
            stack.enter_context(p)  # type: ignore[arg-type]
        stack.enter_context(
            patch("app.services.report_generator._call_llm", side_effect=_capture_pass2_llm)
        )
        report = rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)

    assert report.status == "success"
    assert "INVESTOR PREFERENCES" in captured["pass2_user"]
    assert "Reader locale: zh" in captured["pass2_user"]
    assert "geopolitical developments" in captured["pass2_user"]


def test_generate_report_snapshots_questionnaire_into_report_inputs(db_session: Session) -> None:
    """§8.4/§8.6: the closed-enum answers actually used for this report are
    snapshotted for audit. free_text is NOT folded into that dedicated
    snapshot dict (see investment_context.py's InvestorPreferences
    docstring for the precise, narrower guarantee this is — free_text
    inevitably still appears inside the stored pass2_prompt text itself,
    same as holdings data already does; what this guards against is
    free_text ALSO existing as its own plainly-labeled, bulk-queryable key)."""
    _seed_investment_context(db_session, _USER, locale="zh", intel_focus="GEOPOLITICS")
    with contextlib.ExitStack() as stack:
        for p in _normal_path_patches():
            stack.enter_context(p)  # type: ignore[arg-type]
        report = rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)

    assert report.report_inputs is not None
    snap = report.report_inputs["investor_questionnaire_snapshot"]
    assert snap["intel_focus"] == "GEOPOLITICS"
    assert snap["risk_appetite"] == "AGGRESSIVE"
    assert report.report_inputs["investor_questionnaire_version"] == "v1"
    assert "free_text" not in snap


def test_generate_report_with_no_questionnaire_omits_intel_focus(db_session: Session) -> None:
    """§8.6 'can be skipped': no UserInvestmentContext row -> no intel_focus
    in the prompt, but locale (from users.locale, NOT NULL) still renders
    once a users row exists; with no users row either, locale falls back to
    'en' inside load_investor_preferences and the block is fully omitted
    only when both are absent — this exercises the no-row-at-all case."""
    captured: dict[str, str] = {}

    def _capture_pass2_llm(
        client: object,
        model: str,
        system: str,
        user: str,
        *,
        with_holdings: bool = False,
        **kw: object,
    ) -> str:
        if with_holdings:
            captured["pass2_user"] = user
            return _FAKE_LLM_PASS2
        return _FAKE_LLM_PASS1

    with contextlib.ExitStack() as stack:
        for p in _normal_path_patches():
            stack.enter_context(p)  # type: ignore[arg-type]
        stack.enter_context(
            patch("app.services.report_generator._call_llm", side_effect=_capture_pass2_llm)
        )
        report = rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)

    assert report.status == "success"
    assert "Stated intel focus" not in captured["pass2_user"]


def test_regenerate_render_does_not_refetch_investor_preferences(db_session: Session) -> None:
    _seed_investment_context(db_session, _USER, locale="zh", intel_focus="GEOPOLITICS")
    with contextlib.ExitStack() as stack:
        for p in _normal_path_patches():
            stack.enter_context(p)  # type: ignore[arg-type]
        report = rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)
    rid = report.id

    with (
        patch(
            "app.services.report_generator.load_investor_preferences",
            side_effect=AssertionError("render must not re-fetch investor preferences"),
        ),
        patch(
            "app.services.report_generator.load_news_window",
            side_effect=AssertionError("render must not re-fetch"),
        ),
        patch(
            "app.services.report_generator._run_tavily_search",
            side_effect=AssertionError("render must not re-fetch"),
        ),
    ):
        out = rg.regenerate_report(db_session, rid, user_id=_USER, mode="render", output_lang="en")
    assert out.report_md is not None


def test_regenerate_analyze_refreshes_investor_preferences(db_session: Session) -> None:
    """A user who changed their questionnaire answers between the original
    generation and this regenerate should see the NEW answers reflected —
    same "re-fetched live" treatment as fresh_technical/fresh_exposure."""
    _seed_investment_context(db_session, _USER, locale="zh", intel_focus="MACRO")
    with contextlib.ExitStack() as stack:
        for p in _normal_path_patches():
            stack.enter_context(p)  # type: ignore[arg-type]
        report = rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)
    rid = report.id

    # Re-answer with a different intel_focus.
    from app.models.user_investment_context import UserInvestmentContext

    ctx = db_session.get(UserInvestmentContext, _USER)
    assert ctx is not None
    ctx.questionnaire = {**ctx.questionnaire, "intel_focus": "FUNDAMENTALS"}
    db_session.flush()

    captured: dict[str, str] = {}

    def _capture_pass2_llm(
        client: object,
        model: str,
        system: str,
        user: str,
        *,
        with_holdings: bool = False,
        **kw: object,
    ) -> str:
        captured["pass2_user"] = user
        return _FAKE_LLM_PASS2

    with (
        patch("app.services.report_generator._openrouter_client", return_value=MagicMock()),
        patch("app.services.report_generator._call_llm", side_effect=_capture_pass2_llm),
        patch(
            "app.services.report_generator.load_news_window",
            side_effect=AssertionError("analyze must not re-fetch news"),
        ),
        patch(
            "app.services.report_generator._run_tavily_search",
            side_effect=AssertionError("analyze must not re-run search"),
        ),
    ):
        out = rg.regenerate_report(db_session, rid, user_id=_USER, mode="analyze", output_lang="en")

    assert "individual-holding fundamentals" in captured["pass2_user"]
    assert out.report_inputs is not None
    assert out.report_inputs["investor_questionnaire_snapshot"]["intel_focus"] == "FUNDAMENTALS"
