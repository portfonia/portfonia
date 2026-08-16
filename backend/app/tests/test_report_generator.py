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
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import delete
from sqlalchemy.orm import Session

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

_USER = uuid.UUID("00000000-0000-0000-0000-000000000099")
_NOW = datetime(2026, 6, 4, 20, 0, tzinfo=UTC)
_TODAY = date(2026, 6, 4)


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
        patch("app.services.report_generator.get_current_user_id", return_value=_USER),
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
        report = rg.generate_report(db_session, report_date=_TODAY)

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
        patch("app.services.report_generator.get_current_user_id", return_value=_USER),
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
        report = rg.generate_report(db_session, report_date=_TODAY)

    assert report.status == "success"
    real_queries = [c.kwargs["json"]["query"] for c in mock_post.call_args_list]
    assert any("NVDA" in q for q in real_queries)


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
        patch("app.services.report_generator.get_current_user_id", return_value=_USER),
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
        report = rg.generate_report(db_session, report_date=_TODAY)

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
        patch("app.services.report_generator.get_current_user_id", return_value=_USER),
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
        report = rg.generate_report(db_session, report_date=_TODAY)

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
        patch("app.services.report_generator.get_current_user_id", return_value=_USER),
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
        report = rg.generate_report(db_session, report_date=_TODAY)

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
        patch("app.services.report_generator.get_current_user_id", return_value=_USER),
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
        report = rg.generate_report(db_session, report_date=_TODAY)

    assert report.status == "success"
    assert report.report_inputs is not None
    # L1's prompt DID receive the targeted-search headline.
    assert _TARGETED_TITLE in captured_l1_prompt.get("prompt", "")
    # Pass 2's stored input is untouched — report content stays byte-identical.
    assert report.report_inputs["holding_news"].get("NVDA", []) == []


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
        patch("app.services.report_generator.get_current_user_id", return_value=_USER),
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
        report = rg.generate_report(db_session, report_date=_TODAY)

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
            patch("app.services.report_generator.get_current_user_id", return_value=user_id),
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
            patch("app.services.report_generator.get_current_user_id", return_value=_USER),
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
        report = rg.generate_report(db_session, report_date=_TODAY)

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
        retried = rg.generate_report(db_session, report_date=_TODAY)

    assert retried.id == report.id
    assert retried.status == "success"
    # send_report_email is mocked (module-level _no_email fixture) and never
    # touches provider_message_id, so a None here proves the reset branch
    # cleared it rather than it being silently repopulated by a real send.
    assert retried.provider_message_id is None


# ---------------------------------------------------------------------------
# Tests: quiet-day skip
# ---------------------------------------------------------------------------


def test_generate_report_quiet_day_returns_skipped(db_session: Session) -> None:
    """No signals and no anomalies → status=skipped, no LLM call."""
    with (
        patch("app.services.report_generator.get_current_user_id", return_value=_USER),
        patch("app.services.report_generator.compute_portfolio", return_value=_portfolio_snap()),
        patch("app.services.report_generator.load_news_window", return_value=[]),
        patch("app.services.report_generator.detect_macro_signals", return_value=_quiet_signals()),
        patch("app.services.report_generator.detect_window_anomalies", return_value=([], 0)),
        patch("app.services.report_generator._call_llm") as mock_llm,
    ):
        report = rg.generate_report(db_session, report_date=_TODAY)

    assert report.status == "skipped"
    mock_llm.assert_not_called()
    assert report.report_md is not None
    assert "§1 Portfolio Snapshot" in report.report_md


# ---------------------------------------------------------------------------
# Tests: Tavily failure (degraded mode)
# ---------------------------------------------------------------------------


def test_generate_report_tavily_failure_degraded(db_session: Session) -> None:
    """When Tavily fails, the report is still generated (degraded mode)."""
    with (
        patch("app.services.report_generator.get_current_user_id", return_value=_USER),
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
        report = rg.generate_report(db_session, report_date=_TODAY)

    assert report.status == "success"
    assert report.report_inputs is not None
    assert report.report_inputs["search_results"] == []


# ---------------------------------------------------------------------------
# Tests: LLM Pass 1 returns invalid JSON (graceful fallback)
# ---------------------------------------------------------------------------


def test_generate_report_pass1_invalid_json(db_session: Session) -> None:
    """Pass 1 returns garbage JSON → search_queries empty, pipeline continues."""

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
        patch("app.services.report_generator.get_current_user_id", return_value=_USER),
        patch("app.services.report_generator.compute_portfolio", return_value=_portfolio_snap()),
        patch("app.services.report_generator.load_news_window", return_value=[_news_item("Fed")]),
        patch("app.services.report_generator.detect_macro_signals", return_value=_macro_hit()),
        patch("app.services.report_generator.detect_window_anomalies", return_value=([], 0)),
        patch("app.services.report_generator._openrouter_client", return_value=MagicMock()),
        patch("app.services.report_generator._call_llm", side_effect=bad_pass1),
        patch("app.services.report_generator._run_tavily_search", return_value=[]) as mock_tavily,
    ):
        report = rg.generate_report(db_session, report_date=_TODAY)

    assert report.status == "success"
    assert report.report_inputs is not None
    assert report.report_inputs["search_queries"] == []
    mock_tavily.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: LLM failure → report status=failed
# ---------------------------------------------------------------------------


def test_generate_report_llm_failure_marks_failed(db_session: Session) -> None:
    """LLM exception → report persisted with status=failed, exception re-raised."""
    with (
        patch("app.services.report_generator.get_current_user_id", return_value=_USER),
        patch("app.services.report_generator.compute_portfolio", return_value=_portfolio_snap()),
        patch("app.services.report_generator.load_news_window", return_value=[_news_item("Fed")]),
        patch("app.services.report_generator.detect_macro_signals", return_value=_macro_hit()),
        patch("app.services.report_generator.detect_window_anomalies", return_value=([], 0)),
        patch("app.services.report_generator._openrouter_client", return_value=MagicMock()),
        patch("app.services.report_generator._call_llm", side_effect=RuntimeError("LLM down")),
        pytest.raises(RuntimeError, match="LLM down"),
    ):
        rg.generate_report(db_session, report_date=_TODAY)

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
        patch("app.services.report_generator.get_current_user_id", return_value=_USER),
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
        rg.generate_report(db_session, report_date=_TODAY)

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
        patch("app.services.report_generator.get_current_user_id", return_value=_USER),
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
        rg.generate_report(db_session, report_date=_TODAY)

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
        patch("app.services.report_generator.get_current_user_id", return_value=_USER),
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
        report = rg.generate_report(db_session, report_date=_TODAY)

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
        patch("app.services.report_generator.get_current_user_id", return_value=_USER),
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
        report1 = rg.generate_report(db_session, report_date=_TODAY)
        assert report1.status == "needs_review"
        mock_unmark.assert_not_called()  # fresh row — nothing to unmark yet

        report2 = rg.generate_report(db_session, report_date=_TODAY)
        assert report2.id == report1.id  # same row, reopened for retry
        mock_unmark.assert_called_once_with(db_session, report1.id)


def test_generate_report_quiet_day_sends_heartbeat(
    db_session: Session, _no_email: MagicMock
) -> None:
    """A quiet week must still deliver a heartbeat email so silence != broken."""
    with (
        patch("app.services.report_generator.get_current_user_id", return_value=_USER),
        patch("app.services.report_generator.compute_portfolio", return_value=_portfolio_snap()),
        patch("app.services.report_generator.load_news_window", return_value=[]),
        patch("app.services.report_generator.detect_macro_signals", return_value=_quiet_signals()),
        patch("app.services.report_generator.detect_window_anomalies", return_value=([], 0)),
    ):
        report = rg.generate_report(db_session, report_date=_TODAY)

    assert report.status == "skipped"
    _no_email.assert_called_once()


# ---------------------------------------------------------------------------
# Tests: data window (#5), translation render (#8), re-render (#6)
# ---------------------------------------------------------------------------


def _normal_path_patches() -> list[object]:
    return [
        patch("app.services.report_generator.get_current_user_id", return_value=_USER),
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
        report = rg.generate_report(db_session, report_date=_TODAY)
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
        report = rg.generate_report(db_session, report_date=_TODAY, output_lang="zh")
    assert report.report_md is not None
    assert "[zh]" in report.report_md


def test_regenerate_render_is_token_free(db_session: Session) -> None:
    """mode=render rebuilds from stored Pass 2 body with no LLM call."""
    with contextlib.ExitStack() as stack:
        for p in _normal_path_patches():
            stack.enter_context(p)  # type: ignore[arg-type]
        report = rg.generate_report(db_session, report_date=_TODAY)
    rid = report.id

    with (
        patch("app.services.report_generator.get_current_user_id", return_value=_USER),
        patch(
            "app.services.report_generator._call_llm",
            side_effect=AssertionError("render must not call the LLM"),
        ),
        patch(
            "app.services.report_generator.load_news_window",
            side_effect=AssertionError("render must not re-fetch"),
        ),
    ):
        out = rg.regenerate_report(db_session, rid, mode="render", output_lang="en")

    assert out.status == "success"
    assert out.report_md is not None
    assert "§1 Portfolio Snapshot" in out.report_md
    assert "Data window" in out.report_md


def test_regenerate_analyze_reruns_pass2_from_stored_intel(db_session: Session) -> None:
    """mode=analyze re-runs Pass 2 only — no news/Tavily/Pass 1 re-fetch."""
    with contextlib.ExitStack() as stack:
        for p in _normal_path_patches():
            stack.enter_context(p)  # type: ignore[arg-type]
        report = rg.generate_report(db_session, report_date=_TODAY)
    rid = report.id

    new_body = (
        "## §2 Macro Signals\n\nReanalyzed view. [For information only — not investment advice]\n\n"
        "## §3 Holdings Intelligence\n\nNVIDIA up 9%. [For information only — not investment advice]\n\n"
        "## §4 Risk Radar\n\nConcentration watch. [For information only — not investment advice]\n\n"
        + _PASS2_FILLER
    )
    with (
        patch("app.services.report_generator.get_current_user_id", return_value=_USER),
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
        out = rg.regenerate_report(db_session, rid, mode="analyze", output_lang="en")

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
        patch("app.services.report_generator.get_current_user_id", return_value=_USER),
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
        report = rg.generate_report(db_session, report_date=_TODAY)

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
        patch("app.services.report_generator.get_current_user_id", return_value=_USER),
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
        report = rg.generate_report(db_session, report_date=_TODAY)

    assert report.status == "success"
    assert report.report_md is not None
    assert "免责声明" in report.report_md
    assert "Data Sources & Disclaimer" in report.report_md


def test_generate_report_quiet_day_has_footer(db_session: Session) -> None:
    """Footer must also appear on quiet-day (status=skipped) reports."""
    with (
        patch("app.services.report_generator.get_current_user_id", return_value=_USER),
        patch("app.services.report_generator.compute_portfolio", return_value=_portfolio_snap()),
        patch("app.services.report_generator.load_news_window", return_value=[]),
        patch("app.services.report_generator.detect_macro_signals", return_value=_quiet_signals()),
        patch("app.services.report_generator.detect_window_anomalies", return_value=([], 0)),
        patch("app.services.report_generator._call_llm") as mock_llm,
    ):
        report = rg.generate_report(db_session, report_date=_TODAY)

    assert report.status == "skipped"
    mock_llm.assert_not_called()
    assert report.report_md is not None
    assert "免责声明" in report.report_md
    assert "Data Sources & Disclaimer" in report.report_md


# ---------------------------------------------------------------------------
# Tests: explicit user_id (issue #128 A1)
# ---------------------------------------------------------------------------

_OTHER_USER = uuid.UUID("00000000-0000-0000-0000-0000000000aa")


def test_generate_report_uses_explicit_user_id_not_current_user(db_session: Session) -> None:
    """An explicit user_id must be used as-is and must NOT fall through to
    get_current_user_id() — the multi-user fan-out (report_tasks.py) relies
    on this to generate each user's report under their own identity."""
    with (
        patch("app.services.report_generator.get_current_user_id") as mock_current_user,
        patch("app.services.report_generator.compute_portfolio", return_value=_portfolio_snap()),
        patch("app.services.report_generator.load_news_window", return_value=[]),
        patch("app.services.report_generator.detect_macro_signals", return_value=_quiet_signals()),
        patch("app.services.report_generator.detect_window_anomalies", return_value=([], 0)),
        patch("app.services.report_generator._call_llm") as mock_llm,
    ):
        report = rg.generate_report(db_session, report_date=_TODAY, user_id=_OTHER_USER)

    mock_current_user.assert_not_called()
    mock_llm.assert_not_called()  # quiet day, never reaches Pass 1/2
    assert report.user_id == _OTHER_USER


def test_generate_report_falls_back_to_current_user_id_when_omitted(db_session: Session) -> None:
    """user_id=None (every pre-A1 call site) must still resolve via
    get_current_user_id() — the Ring 0 single-user path is unchanged."""
    with (
        patch("app.services.report_generator.get_current_user_id", return_value=_USER) as mock_cur,
        patch("app.services.report_generator.compute_portfolio", return_value=_portfolio_snap()),
        patch("app.services.report_generator.load_news_window", return_value=[]),
        patch("app.services.report_generator.detect_macro_signals", return_value=_quiet_signals()),
        patch("app.services.report_generator.detect_window_anomalies", return_value=([], 0)),
        patch("app.services.report_generator._call_llm"),
    ):
        report = rg.generate_report(db_session, report_date=_TODAY)

    mock_cur.assert_called_once()
    assert report.user_id == _USER


def test_generate_report_forwards_moves_cache_to_detect_window_anomalies(
    db_session: Session,
) -> None:
    """moves_cache must reach detect_window_anomalies unchanged — this is
    the plumbing that lets report_tasks.py's fan-out share one
    compute_global_moves() call across a whole batch."""
    cache: MovesCache = {}
    with (
        patch("app.services.report_generator.get_current_user_id", return_value=_USER),
        patch("app.services.report_generator.compute_portfolio", return_value=_portfolio_snap()),
        patch("app.services.report_generator.load_news_window", return_value=[]),
        patch("app.services.report_generator.detect_macro_signals", return_value=_quiet_signals()),
        patch(
            "app.services.report_generator.detect_window_anomalies", return_value=([], 0)
        ) as mock_detect,
        patch("app.services.report_generator._call_llm"),
    ):
        rg.generate_report(db_session, report_date=_TODAY, moves_cache=cache)

    assert mock_detect.call_args.args[-1] is cache
