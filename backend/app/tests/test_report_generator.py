"""Tests for report_generator (F1/F2/F3/G).

Strategy:
- All external calls (LLM, Tavily, news_fetcher, macro_detector,
  price_anomaly_detector, email_sender) are mocked via patch.
- DB operations use the db_session fixture (real Postgres).
- Tests cover: normal path, quiet-day skip, LLM failure, Tavily failure (degraded).
"""

from __future__ import annotations

import contextlib
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import MagicMock, patch

import openai
import pytest
from sqlalchemy.orm import Session

from app.services import report_generator as rg
from app.services.macro_detector import MacroSignals, ThemeHit
from app.services.news_fetcher import NewsItem
from app.services.portfolio_calculator import (
    Concentration,
    HoldingValue,
    PortfolioSnapshot,
)
from app.services.price_anomaly_detector import PriceAnomaly

_USER = uuid.UUID("00000000-0000-0000-0000-000000000099")
_NOW = datetime(2026, 6, 4, 20, 0, tzinfo=UTC)
_TODAY = date(2026, 6, 4)


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


# ---------------------------------------------------------------------------
# Tests: _stale_ticker_hint (unit, no DB)
# ---------------------------------------------------------------------------


def test_pass1_prompt_excludes_holdings_derived_anomalies() -> None:
    """DATA ISOLATION: Pass 1 runs without data_collection=deny, so it must not
    carry holdings-derived identifiers. Price anomalies (name/ticker = a held
    position) belong only in Pass 2."""
    signals = _macro_hit()
    news = [_news_item("Fed raises rates")]
    prompt = rg._build_pass1_prompt(signals, news)

    # Anomaly identifiers from a user's holdings must never appear in Pass 1.
    assert "NVDA" not in prompt
    assert "NVIDIA" not in prompt
    assert "PRICE ANOMALIES" not in prompt
    # Public signal/news content is still present.
    assert "MACRO SIGNAL THEMES" in prompt
    assert "TOP HEADLINES" in prompt


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


# ---------------------------------------------------------------------------
# Tests: compliance output backstop
# ---------------------------------------------------------------------------


def test_scan_forbidden_output_flags_advisory_language() -> None:
    assert rg._scan_forbidden_output("You should buy more AAPL.") == ["should buy"]
    assert rg._scan_forbidden_output("We recommend reducing exposure.")  # non-empty
    assert rg._scan_forbidden_output("Set a stop-loss near 100.")
    assert rg._scan_forbidden_output("止损位在 100。")


def test_scan_forbidden_output_no_false_positives() -> None:
    """Factual prose with substrings of forbidden words must stay clean."""
    clean = (
        "## §3 Holdings Intelligence\n"
        "The company announced a buyback; households increased savings. "
        "AAPL exits the index. Threshold breached. "
        "[For information only — not investment advice]"
    )
    assert rg._scan_forbidden_output(clean) == []


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


def test_build_data_window_states_interval() -> None:
    news = [
        {"published_at": "2026-06-01T08:00:00+00:00"},
        {"published_at": "2026-06-03T20:00:00+00:00"},
    ]
    portfolio = {
        "fx_date": "2026-06-03",
        "holdings": [{"price_as_of": "2026-06-03T20:00:00+00:00"}],
    }
    w = rg._build_data_window(
        news, portfolio, "2026-06-01T16:00:00+00:00", "2026-06-04T20:30:00+00:00", 3
    )
    assert "Data window" in w
    assert "2026-06-01 16:00 to 2026-06-04 20:30" in w
    assert "3 trading day(s)" in w
    assert "FX as of 2026-06-03" in w
    assert "baseline close" in w


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


def test_stale_ticker_hint_fund_code() -> None:
    assert "CN mutual fund" in rg._stale_ticker_hint("005827")
    assert "天天基金" in rg._stale_ticker_hint("005827")
    assert "005827" in rg._stale_ticker_hint("005827")


def test_stale_ticker_hint_a_share_ss() -> None:
    result = rg._stale_ticker_hint("600519.SS")
    assert "A-share" in result
    assert "Shanghai" in result


def test_stale_ticker_hint_a_share_sz() -> None:
    result = rg._stale_ticker_hint("000858.SZ")
    assert "A-share" in result
    assert "Shenzhen" in result


def test_stale_ticker_hint_hk() -> None:
    result = rg._stale_ticker_hint("0700.HK")
    assert "HK-listed" in result


def test_stale_ticker_hint_us_stock() -> None:
    result = rg._stale_ticker_hint("AAPL")
    assert "stock ticker" in result


def test_stale_ticker_hint_in_pass2_prompt() -> None:
    """Fund code in stale_tickers must appear with CN hint in the Pass 2 prompt."""
    portfolio = rg._serialize_portfolio(_portfolio_snap())
    portfolio["stale_tickers"] = ["005827", "AAPL", "0700.HK"]
    prompt = rg._build_pass2_prompt(portfolio, {}, [], [])
    assert "CN mutual fund" in prompt
    assert "stock ticker" in prompt
    assert "HK-listed" in prompt


# ---------------------------------------------------------------------------
# Tests: section builders (unit, no DB)
# ---------------------------------------------------------------------------


def test_build_section1_contains_required_rows() -> None:
    portfolio = rg._serialize_portfolio(_portfolio_snap())
    md = rg._build_section1(portfolio)
    assert "§1 Portfolio Snapshot" in md
    assert "Apple Inc." in md
    assert "AAPL" in md
    assert "10,000" in md or "10000" in md


def test_build_section1_groups_by_broker_in_upload_order_with_subtotals() -> None:
    portfolio = {
        "base_currency": "USD",
        "fx_date": "2026-06-06",
        "total_base": 300.0,
        "by_market": {"US": 200.0, "HK": 100.0},
        "by_currency": {},
        "by_asset_type": {},
        "holdings": [
            # Deliberately out of position order; IBKR appears first in the file.
            # Echo (cash) has no broker → falls into the "Other" group.
            {
                "name": "Alpha",
                "broker": "IBKR",
                "market_value": 100,
                "market_value_base": 100.0,
                "position": 0,
            },
            {
                "name": "Bravo",
                "broker": "Futu",
                "market_value": 100,
                "market_value_base": 100.0,
                "position": 1,
            },
            {
                "name": "Charlie",
                "broker": "IBKR",
                "market_value": 100,
                "market_value_base": 100.0,
                "position": 2,
            },
            {
                "name": "Echo",
                "broker": None,
                "market_value": 0,
                "market_value_base": 0.0,
                "position": 3,
            },
        ],
    }
    md = rg._build_section1(portfolio)
    # IBKR group (Alpha, Charlie) before Futu group (Bravo); each group subtotaled.
    assert md.index("Alpha") < md.index("Charlie") < md.index("Bravo")
    assert "**IBKR subtotal**" in md
    assert "**Futu subtotal**" in md
    assert "**Other subtotal**" in md  # broker-less holding bucketed into Other
    assert md.index("IBKR subtotal") < md.index("Bravo")  # IBKR block closes before Futu
    assert "Custodian" in md  # column header renamed from Market


# ---------------------------------------------------------------------------
# Tests: §4.2 price-anomaly table (#3 — code-built, no LLM)
# ---------------------------------------------------------------------------


def _anomaly_dict() -> dict[str, object]:
    """A fully populated serialized anomaly (window net + worst day + arc)."""
    return {
        "name": "NVIDIA",
        "identifier": "NVDA",
        "asset_type": "stock",
        "market": "US",
        "trigger": "single_day",
        "window_net_pct": 0.085,
        "max_day_pct": -0.062,
        "max_day_date": "2026-06-06",
        "baseline_date": "2026-06-01",
        "latest_date": "2026-06-06",
        "prev_close": 110.0,
        "day_open": 116.0,
        "day_high": 121.0,
        "day_low": 113.0,
        "day_close": 120.0,
        "after_hours": 122.0,
    }


def test_build_section42_table_renders_numbers_no_hallucination() -> None:
    md = rg._build_section42_table([_anomaly_dict()])
    assert "| Holding |" in md and "Trigger" in md  # header row
    assert "NVIDIA (NVDA)" in md
    assert "+8.50%" in md  # window net
    assert "-6.20% (2026-06-06)" in md  # worst day with date
    assert "116 (+5.5%)" in md  # open with gap vs prev close
    assert "113-121" in md  # intraday range
    assert "122 (+1.7%)" in md  # after-hours move vs close
    assert "single_day" in md


def test_build_section42_table_handles_missing_arc_fields() -> None:
    # Anomaly with only the net move (no session arc) must not crash.
    md = rg._build_section42_table(
        [{"name": "X", "identifier": "X", "trigger": "cumulative", "window_net_pct": 0.04}]
    )
    assert "X (X)" in md
    assert "—" in md  # missing cells rendered as em-dash placeholder


def test_inject_section42_table_inserts_after_heading() -> None:
    body = "## §4 Risk Radar\n### 4.2 Price anomalies\nNVDA — chip-cycle optimism.\n"
    out = rg._inject_section42_table(body, "TABLE_ROWS")
    assert out.index("TABLE_ROWS") < out.index("NVDA — chip-cycle optimism")
    assert out.index("### 4.2") < out.index("TABLE_ROWS")  # table sits under the heading


def test_inject_section42_table_fallback_appends_when_heading_absent() -> None:
    body = "## §4 Risk Radar\n### 4.1 Concentration\nflagged.\n"
    out = rg._inject_section42_table(body, "TABLE_ROWS")
    assert "### 4.2 Price anomalies" in out
    assert "TABLE_ROWS" in out


def test_pass2_prompt_default_requests_all_three_narrative_sections() -> None:
    prompt = rg._build_pass2_prompt(rg._serialize_portfolio(_portfolio_snap()), {}, [], [])
    assert "## §2 Macro Signals" in prompt
    assert "## §3 Holdings Analysis" in prompt
    assert "## §4 Risk Radar" in prompt


def test_pass2_prompt_enabled_sections_restricts_instructions() -> None:
    """Ring 1 prep: a report type that only wants §2 must not get §3/§4 instructions."""
    prompt = rg._build_pass2_prompt(
        rg._serialize_portfolio(_portfolio_snap()),
        {},
        [],
        [],
        enabled_sections=frozenset({"§2"}),
    )
    assert "## §2 Macro Signals" in prompt
    assert "## §3 Holdings Analysis" not in prompt
    assert "## §4 Risk Radar" not in prompt


def test_pass2_prompt_42_asks_for_drivers_not_restated_numbers() -> None:
    prompt = rg._build_pass2_prompt(rg._serialize_portfolio(_portfolio_snap()), {}, [], [])
    # The numeric table is code-built; the model must not restate the arc numbers.
    assert "do NOT restate those numbers" in prompt
    assert "IDENTIFIER — <driver> [Label]" in prompt


# ---------------------------------------------------------------------------
# Tests: confidence labels (#2 — evidence-ordinal, not numeric)
# ---------------------------------------------------------------------------


def test_pass2_prompt_defines_evidence_ordinal_labels() -> None:
    prompt = rg._build_pass2_prompt(rg._serialize_portfolio(_portfolio_snap()), {}, [], [])
    assert "CONFIDENCE LABELS" in prompt
    for label in ("[Established]", "[Probable]", "[Speculative]"):
        assert label in prompt
    # Calibrated honesty, not manufactured certainty: never a numeric percentage,
    # and a large unexplained move is kept (labelled), not dropped.
    assert "NEVER a numeric percentage" in prompt
    assert "do not drop or downgrade a large unexplained move" in prompt.lower()


def test_translate_glossary_maps_confidence_labels_to_chinese() -> None:
    captured: dict[str, str] = {}

    def _fake_call(_client: object, _model: str, system: str, user: str, **_kw: object) -> str:
        captured["system"] = system
        return user

    with (
        patch.object(rg, "_openrouter_client", return_value=MagicMock()),
        patch.object(rg, "_call_llm", side_effect=_fake_call),
    ):
        rg._translate_md("## §4\nNVDA — chip optimism [Established].\n", "zh")
    assert '"[Established]" -> "[确定]"' in captured["system"]
    assert '"[Probable]" -> "[较可能]"' in captured["system"]
    assert '"[Speculative]" -> "[推测]"' in captured["system"]


# ---------------------------------------------------------------------------
# Tests: §4.4 technical position (#4 — code-built, descriptive-only)
# ---------------------------------------------------------------------------


def _tech_dict(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "ticker": "NVDA",
        "name": "NVIDIA",
        "last_close": 120.0,
        "bars": 252,
        "pct_vs_sma50": 0.06,
        "pct_vs_sma200": 0.18,
        "range_52w_low": 80.0,
        "range_52w_high": 140.0,
        "pct_in_52w_range": 0.667,
        "vol_20d_annualized": 0.42,
    }
    base.update(over)
    return base


def test_build_section44_renders_descriptive_metrics() -> None:
    md = rg._build_section44_technical([_tech_dict()])
    assert "### 4.4 Technical position" in md
    assert "NVIDIA (NVDA)" in md
    assert "+6.0%" in md  # vs 50-day avg
    assert "+18.0%" in md  # vs 200-day avg
    assert "67%" in md  # 52-week range position
    assert "+42.0%" in md  # annualized volatility


def test_build_section44_insufficient_history_message_no_table() -> None:
    sparse = _tech_dict(
        pct_vs_sma50=None, pct_vs_sma200=None, pct_in_52w_range=None, vol_20d_annualized=None
    )
    md = rg._build_section44_technical([sparse])
    assert "Insufficient captured price history" in md
    assert "| Holding |" not in md  # no table rendered


def test_build_section44_partial_metrics_render_dash() -> None:
    md = rg._build_section44_technical([_tech_dict(pct_vs_sma200=None)])
    assert "| Holding |" in md  # table rendered (some metric present)
    assert "—" in md  # the missing 200-day cell


def test_scan_flags_advisory_action_language() -> None:
    # Direct advisory/action language must trip the backstop.
    for phrase in ("stop-loss", "target price", "strong buy", "entry point", "止损", "目标价"):
        assert rg._scan_forbidden_output(f"set a {phrase} near 100") != []


def test_scan_allows_ta_observation_vocabulary() -> None:
    # Descriptive TA terms (where price sits) are observation language, not advice.
    # The Layer-3 prompt and disclaimer cover the advisory boundary — the scan
    # backstop is reserved for direct action/recommendation language only.
    for phrase in (
        "support level",
        "resistance level",
        "golden cross",
        "breakout",
        "支撑位",
        "阻力位",
        "金叉",
        "死叉",
    ):
        assert rg._scan_forbidden_output(f"the {phrase} held") == [], (
            f"unexpectedly flagged: {phrase!r}"
        )


def test_scan_allows_descriptive_price_structure() -> None:
    body = (
        "NVDA closed 6% below its 50-day moving average and sits in the lower third "
        "of its 52-week range; 20-day annualized volatility is 42%."
    )
    assert rg._scan_forbidden_output(body) == []


# ---------------------------------------------------------------------------
# Tests: §2.5 forward calendar (#1 — code-built, observation-framed)
# ---------------------------------------------------------------------------


def _fwd_holdings() -> list[dict[str, object]]:
    return [
        {
            "name": "NVIDIA",
            "ticker": "NVDA",
            "market": "US",
            "asset_type": "stock",
            "sector": "Technology",
        },
        {
            "name": "SPDR Gold",
            "ticker": "GLD",
            "market": "US",
            "asset_type": "etf",
            "sector": "Other",
        },
        {
            "name": "Costco",
            "ticker": "COST",
            "market": "US",
            "asset_type": "stock",
            "sector": "Consumer Staples",
        },
        {
            "name": "Tencent",
            "ticker": "0700.HK",
            "market": "HK",
            "asset_type": "stock",
            "sector": "Technology",
        },
    ]


def test_forward_exposure_cpi_maps_rate_sensitive_and_gold() -> None:
    exposed, watch = rg._forward_exposure(
        {"event_type": "macro", "name": "Consumer Price Index (CPI)"}, _fwd_holdings()
    )
    assert "NVIDIA" in exposed and "SPDR Gold" in exposed  # tech + gold
    assert "Tencent" not in exposed  # non-US excluded
    assert "inflation" in watch and "rise" not in watch  # observation, not forecast


def test_forward_exposure_earnings_maps_exact_ticker() -> None:
    exposed, _ = rg._forward_exposure(
        {"event_type": "earnings", "name": "NVDA", "ticker": "NVDA"}, _fwd_holdings()
    )
    assert exposed == ["NVIDIA"]


def test_forward_exposure_retail_maps_consumer_only() -> None:
    exposed, _ = rg._forward_exposure(
        {"event_type": "macro", "name": "Retail Sales"}, _fwd_holdings()
    )
    assert exposed == ["Costco"]


def test_forward_delay_risk_detects_funding_lapse() -> None:
    assert rg._forward_delay_risk([{"title": "Government shutdown looms", "summary": ""}]) is True
    assert (
        rg._forward_delay_risk([{"title": "Tech stocks rally", "summary": "strong demand"}])
        is False
    )


def test_build_forward_block_renders_table_and_delay_caveat() -> None:
    events = [
        {"event_type": "macro", "name": "FOMC Statement", "scheduled_date": "2026-06-17"},
        {
            "event_type": "earnings",
            "name": "NVDA",
            "ticker": "NVDA",
            "scheduled_date": "2026-06-15",
        },
    ]
    news = [{"title": "Congress funding lapse risk", "summary": ""}]
    md = rg._build_forward_block(events, _fwd_holdings(), news)
    assert "## §2.5 Forward Calendar" in md
    assert "2026-06-17" in md and "FOMC Statement" in md
    assert "calendar facts, not forecasts" in md
    assert "delay scheduled BLS/BEA releases" in md  # caveat appended


def test_inject_forward_block_inserts_before_section3() -> None:
    body = "## §2 Macro Signals\nstuff\n## §3 Holdings Analysis\nmore"
    out = rg._inject_forward_block(body, "## §2.5 Forward Calendar\nX")
    assert out.index("§2.5") < out.index("## §3")
    assert out.index("## §2 ") < out.index("§2.5")


def test_pass2_system_forbids_forecasting_scheduled_events() -> None:
    assert "FORWARD EVENTS" in rg._PASS2_SYSTEM
    assert "NEVER predict its outcome" in rg._PASS2_SYSTEM


def test_pass2_system_restricts_section42_cross_reference() -> None:
    # R-8: 'see §4.2' may only point at holdings actually in the anomaly table.
    assert "§4.2 CROSS-REFERENCES" in rg._PASS2_SYSTEM
    assert "did not cross" in rg._PASS2_SYSTEM


# --- R-5: price-data cutoff + FX-stale flag -------------------------------


def test_data_window_states_price_cutoff_and_no_intraday() -> None:
    w = rg._build_data_window(
        [],
        {"fx_date": "2026-06-09"},
        "2026-06-09T12:00:00+00:00",
        "2026-06-10T12:38:00+00:00",
        1,
        price_data_through="2026-06-09",
    )
    assert "Price data through the 2026-06-09 close" in w
    assert "no premarket or intraday quotes" in w


def test_data_window_flags_stale_fx() -> None:
    # FX dated 2026-06-04 against a 2026-06-10 cutoff → >1 day → flagged.
    w = rg._build_data_window(
        [],
        {"fx_date": "2026-06-04"},
        "2026-06-09T12:00:00+00:00",
        "2026-06-10T20:30:00+00:00",
        1,
    )
    assert "FX rate is stale" in w


def test_data_window_no_stale_flag_when_fx_current() -> None:
    w = rg._build_data_window(
        [],
        {"fx_date": "2026-06-10"},
        "2026-06-09T12:00:00+00:00",
        "2026-06-10T20:30:00+00:00",
        1,
    )
    assert "FX rate is stale" not in w


# --- R-6: T+0 calendar promotion ------------------------------------------


def test_today_events_block_promotes_same_day_event() -> None:
    events = [
        {
            "event_type": "macro",
            "name": "Consumer Price Index (CPI)",
            "scheduled_date": "2026-06-10",
        },
        {"event_type": "macro", "name": "FOMC Statement", "scheduled_date": "2026-06-17"},
    ]
    block = rg._build_today_events_block(events, _fwd_holdings(), "2026-06-10")
    assert "Today's scheduled events" in block
    assert "Consumer Price Index" in block
    assert "FOMC" not in block  # future event stays in §2.5 only
    assert "results not yet in this report's data" in block


def test_today_events_block_empty_when_none_today() -> None:
    events = [{"event_type": "macro", "name": "FOMC", "scheduled_date": "2026-06-17"}]
    assert rg._build_today_events_block(events, _fwd_holdings(), "2026-06-10") == ""


def test_inject_today_events_lands_under_section2_heading() -> None:
    body = "## §2 Macro Signals\nprose\n## §3 Holdings\nmore"
    out = rg._inject_today_events(body, "**Today's scheduled events**: CPI")
    assert out.index("## §2 Macro Signals") < out.index("Today's scheduled events")
    assert out.index("Today's scheduled events") < out.index("## §3")


def test_forward_block_tags_today_row() -> None:
    events = [{"event_type": "macro", "name": "CPI", "scheduled_date": "2026-06-10"}]
    md = rg._build_forward_block(events, _fwd_holdings(), [], report_date_str="2026-06-10")
    assert "2026-06-10 (today)" in md


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


def test_serialize_anomalies_float_conversion() -> None:
    anomalies = [_anomaly()]
    result = rg._serialize_anomalies(anomalies)
    assert len(result) == 1
    a = result[0]
    assert a["identifier"] == "NVDA"
    assert isinstance(a["pct_change"], float)
    assert abs(a["pct_change"] - 0.0909) < 0.001


def test_report_context_to_jsonb_serialisable() -> None:
    ctx = rg.ReportContext(
        pass1_model="deepseek/test",
        search_queries=["foo"],
    )
    import json

    data = ctx.to_jsonb()
    assert data["pass1_model"] == "deepseek/test"
    json.dumps(data)  # must not raise


# ---------------------------------------------------------------------------
# Tests: _strip_markers (unit, no DB) — #9: inline tags/citations are removed
# ---------------------------------------------------------------------------


def test_strip_markers_removes_news_citations() -> None:
    text = "The Fed raised rates [S1] and markets reacted [S12]."
    result = rg._strip_markers(text)
    assert "[S1]" not in result
    assert "[S12]" not in result
    assert "新闻" not in result  # no replacement marker is introduced either
    assert "The Fed raised rates and markets reacted." in result


def test_strip_markers_removes_consecutive_citation_run() -> None:
    text = "Markets moved [S6][S7][S8] [S9][S10] sharply."
    result = rg._strip_markers(text)
    assert "S6" not in result and "S10" not in result
    assert "Markets moved sharply." in result


def test_strip_markers_removes_compliance_suffix() -> None:
    text = f"Rates may pressure valuations. {rg._COMPLIANCE_MARKER}"
    result = rg._strip_markers(text)
    assert "For information only" not in result
    assert result.strip() == "Rates may pressure valuations."


def test_strip_markers_removes_provenance_tags() -> None:
    text = "AAPL fell 9% [行情] on weak demand [新闻]; rates may pressure it [分析]."
    result = rg._strip_markers(text)
    for tag in ("[行情]", "[新闻]", "[分析]"):
        assert tag not in result
    assert "AAPL fell 9% on weak demand; rates may pressure it." in result


def test_strip_markers_removes_macro_theme_tag() -> None:
    text = "Cerebras rose [宏观主题数据] on chip-strategy support."
    result = rg._strip_markers(text)
    assert "宏观主题数据" not in result
    assert "Cerebras rose on chip-strategy support." in result


def test_strip_markers_noop_on_clean_text() -> None:
    text = "Rates rose and tech sold off."
    assert rg._strip_markers(text) == text


def test_strip_markers_removes_model_emitted_disclaimer() -> None:
    """The model sometimes appends its own disclaimer paragraph despite the system
    prompt; it must be dropped (the footer owns the single disclaimer) — otherwise
    its '投资建议' / 'investment advice' wording false-trips the compliance scan."""
    body = (
        "## §4 Risk Radar\n\n"
        "USD exposure is 68.7%.\n\n"
        "---\n\n"
        "*本报告仅供信息参考，不构成任何投资建议或买卖指令。This report is for "  # noqa: RUF001
        "informational purposes only and does not constitute investment advice.*"
    )
    result = rg._strip_markers(body)
    assert "投资建议" not in result
    assert "investment advice" not in result
    assert "USD exposure is 68.7%." in result  # real content kept
    assert rg._scan_forbidden_output(result) == []  # no longer trips the scan
    assert not result.rstrip().endswith("---")  # orphaned rule trimmed


def test_split_sections_chunks_at_section_and_subsection_headings_and_roundtrips() -> None:
    md = (
        "# Title\n\n> data window\n\n## §1 Snapshot\n\n| a | b |\n\n"
        "## §4 Risk\n\n### 4.1 Concentration\n\nflagged\n\n### 4.2 Anomalies\n\n| t |\n"
    )
    chunks = rg._split_sections(md)
    # preamble + §1 + §4 header + 4.1 + 4.2  → subsections split out
    assert [c.split("\n", 1)[0] for c in chunks] == [
        "# Title",
        "## §1 Snapshot",
        "## §4 Risk",
        "### 4.1 Concentration",
        "### 4.2 Anomalies",
    ]
    # joining with a single newline reproduces the original document exactly
    assert "\n".join(chunks) == md


def _fake_llm_response(content: str | None) -> MagicMock:
    """Shape a minimal OpenAI-style chat completion response."""
    resp = MagicMock()
    resp.model = "fake/model"
    if content is None:
        resp.choices = None
        return resp
    choice = MagicMock()
    choice.finish_reason = "stop"
    choice.message.content = content
    resp.choices = [choice]
    resp.usage = MagicMock(prompt_tokens=1, completion_tokens=1, total_tokens=2, cost=0.0)
    return resp


def test_call_llm_raises_on_empty_choices() -> None:
    client = MagicMock()
    client.chat.completions.create.return_value = _fake_llm_response(None)
    with pytest.raises(rg.LLMEmptyResponseError):
        rg._call_llm(client, "m", "sys", "user")


def test_call_llm_retries_on_rate_limit_then_succeeds() -> None:
    client = MagicMock()
    err = openai.RateLimitError("rate limited", response=MagicMock(status_code=429), body=None)
    client.chat.completions.create.side_effect = [err, _fake_llm_response("ok")]
    with patch("app.services.report_generator.time.sleep") as sleep:
        out = rg._call_llm(client, "m", "sys", "user")
    assert out == "ok"
    assert client.chat.completions.create.call_count == 2
    sleep.assert_called_once()  # one backoff before the successful retry


def test_call_llm_reraises_after_exhausting_rate_limit_retries() -> None:
    client = MagicMock()
    err = openai.RateLimitError("rate limited", response=MagicMock(status_code=429), body=None)
    client.chat.completions.create.side_effect = err
    with patch("app.services.report_generator.time.sleep"), pytest.raises(openai.RateLimitError):
        rg._call_llm(client, "m", "sys", "user")
    assert client.chat.completions.create.call_count == 3  # initial + 2 backoff retries


def test_translate_chunk_falls_back_to_source_when_truncated() -> None:
    source = "## §3 Holdings Analysis\n\n" + ("This holding matters because " * 20)
    calls = {"n": 0}

    def _truncating(_c: object, _m: str, _s: str, _u: str, **_k: object) -> str:
        calls["n"] += 1
        return "持仓"  # always far too short → simulates a dropped/truncated 200

    with patch.object(rg, "_call_llm", side_effect=_truncating):
        out = rg._translate_chunk(MagicMock(), "m", "sys", source)
    assert calls["n"] == 2  # tried once, retried once
    assert out == source  # kept the English source rather than dropping the section


def test_translate_chunk_keeps_good_translation() -> None:
    source = "## §3 Holdings Analysis\n\n" + ("This holding matters. " * 20)

    def _ok(_c: object, _m: str, _s: str, _u: str, **_k: object) -> str:
        return "## §3 持仓分析\n\n" + ("该持仓非常重要并值得密切关注。" * 20)

    with patch.object(rg, "_call_llm", side_effect=_ok):
        out = rg._translate_chunk(MagicMock(), "m", "sys", source)
    assert out.startswith("## §3 持仓分析")


def test_strip_body_disclaimer_runs_post_translation() -> None:
    """A disclaimer the translator re-adds (after the pre-translation strip) must
    still be removed by the standalone post-translation pass."""
    translated = (
        "## §4 风险雷达\n\n美元敞口为 68.7%。\n\n---\n\n"
        "*本报告仅供参考，不构成投资建议。*"  # noqa: RUF001
    )
    out = rg._strip_body_disclaimer(translated)
    assert "投资建议" not in out
    assert "美元敞口为 68.7%。" in out
    assert rg._scan_forbidden_output(out) == []


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
# Tests: F3 footer (unit + integration)
# ---------------------------------------------------------------------------


def test_build_footer_contains_fx_date() -> None:
    portfolio = {"base_currency": "USD", "fx_date": "2026-06-04", "holdings": []}
    footer = rg._build_footer(portfolio)
    assert "2026-06-04" in footer
    assert "USD" in footer


def test_build_footer_contains_bilingual_disclaimer() -> None:
    portfolio = {"base_currency": "USD", "fx_date": "2026-06-04", "holdings": []}
    footer = rg._build_footer(portfolio)
    assert "Disclaimer" in footer
    assert "免责声明" in footer
    assert "investment advice" in footer
    assert "投资建议" in footer


def test_build_footer_starts_with_separator() -> None:
    portfolio = {"base_currency": "USD", "fx_date": "2026-06-04", "holdings": []}
    footer = rg._build_footer(portfolio)
    assert "---" in footer


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
