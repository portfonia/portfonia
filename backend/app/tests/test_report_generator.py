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
        concentration=Concentration(
            top_holding_name="Apple Inc.",
            top_holding_ratio=Decimal("1.0"),
            top3_ratio=Decimal("1.0"),
            top_sector_name="Technology",
            top_sector_ratio=Decimal("1.0"),
            single_holding_watch=True,
            single_holding_high=True,
            top3_watch=True,
            sector_watch=True,
        ),
        stale_tickers=[],
    )


_FAKE_LLM_PASS1 = (
    '{"queries": ["Federal Reserve rate decision impact", "NVIDIA earnings semiconductor"]}'
)
_FAKE_LLM_PASS2 = (
    "## §2 Macro Signals\n\nFed raised rates. [For information only — not investment advice]\n\n"
    "## §3 Holdings Intelligence\n\nNVIDIA up 9%. [For information only — not investment advice]\n\n"
    "## §4 Risk Radar\n\nConcentration watch. [For information only — not investment advice]"
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
    "Concentration above thresholds. [For information only — not investment advice]"
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
    client: object, model: str, system: str, user: str, *, with_holdings: bool = False
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
            "app.services.report_generator.fetch_news",
            return_value=[_news_item("Fed raises rates")],
        ),
        patch("app.services.report_generator.detect_macro_signals", return_value=_macro_hit()),
        patch("app.services.report_generator.detect_price_anomalies", return_value=[_anomaly()]),
        patch("app.services.report_generator._openrouter_client", return_value=MagicMock()),
        patch("app.services.report_generator._call_llm", side_effect=_mock_llm),
        patch(
            "app.services.report_generator._run_tavily_search", return_value=_FAKE_TAVILY_RESULTS
        ),
    ):
        report = rg.generate_report(db_session, report_date=_TODAY)

    assert report.status == "success"
    assert report.report_date == _TODAY
    assert report.report_type == "weekly"
    assert report.report_md is not None
    assert "§1 Portfolio Snapshot" in report.report_md
    assert "§2 Macro Signals" in report.report_md
    assert "§3 Holdings Intelligence" in report.report_md
    assert "§4 Risk Radar" in report.report_md
    assert report.generated_at is not None
    assert report.report_inputs is not None
    assert report.report_inputs["pass2_model"] != ""
    assert len(report.report_inputs["search_queries"]) == 2
    assert len(report.report_inputs["search_results"]) == 1


# ---------------------------------------------------------------------------
# Tests: quiet-day skip
# ---------------------------------------------------------------------------


def test_generate_report_quiet_day_returns_skipped(db_session: Session) -> None:
    """No signals and no anomalies → status=skipped, no LLM call."""
    with (
        patch("app.services.report_generator.get_current_user_id", return_value=_USER),
        patch("app.services.report_generator.compute_portfolio", return_value=_portfolio_snap()),
        patch("app.services.report_generator.fetch_news", return_value=[]),
        patch("app.services.report_generator.detect_macro_signals", return_value=_quiet_signals()),
        patch("app.services.report_generator.detect_price_anomalies", return_value=[]),
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
            "app.services.report_generator.fetch_news",
            return_value=[_news_item("Fed raises rates")],
        ),
        patch("app.services.report_generator.detect_macro_signals", return_value=_macro_hit()),
        patch("app.services.report_generator.detect_price_anomalies", return_value=[]),
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
        client: object, model: str, system: str, user: str, *, with_holdings: bool = False
    ) -> str:
        if not with_holdings:
            return "not valid json at all"
        return _FAKE_LLM_PASS2

    with (
        patch("app.services.report_generator.get_current_user_id", return_value=_USER),
        patch("app.services.report_generator.compute_portfolio", return_value=_portfolio_snap()),
        patch("app.services.report_generator.fetch_news", return_value=[_news_item("Fed")]),
        patch("app.services.report_generator.detect_macro_signals", return_value=_macro_hit()),
        patch("app.services.report_generator.detect_price_anomalies", return_value=[]),
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
        patch("app.services.report_generator.fetch_news", return_value=[_news_item("Fed")]),
        patch("app.services.report_generator.detect_macro_signals", return_value=_macro_hit()),
        patch("app.services.report_generator.detect_price_anomalies", return_value=[]),
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
        client: object, model: str, system: str, user: str, *, with_holdings: bool = False
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
            "app.services.report_generator.fetch_news",
            return_value=[_news_item("Fed raises rates")],
        ),
        patch("app.services.report_generator.detect_macro_signals", return_value=_macro_hit()),
        patch(
            "app.services.report_generator.detect_price_anomalies",
            return_value=[aapl_anomaly],
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
    client: object, model: str, system: str, user: str, *, with_holdings: bool = False
) -> str:
    if with_holdings:
        return (
            "## §2 Macro Signals\n\nYou should buy more semiconductors now. "
            "[For information only — not investment advice]"
        )
    return _FAKE_LLM_PASS1


def test_generate_report_blocks_noncompliant_body(
    db_session: Session, _no_email: MagicMock
) -> None:
    """A body that trips the blacklist is held as needs_review and never emailed."""
    with (
        patch("app.services.report_generator.get_current_user_id", return_value=_USER),
        patch("app.services.report_generator.compute_portfolio", return_value=_portfolio_snap()),
        patch("app.services.report_generator.fetch_news", return_value=[_news_item("Fed")]),
        patch("app.services.report_generator.detect_macro_signals", return_value=_macro_hit()),
        patch("app.services.report_generator.detect_price_anomalies", return_value=[_anomaly()]),
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
        patch("app.services.report_generator.fetch_news", return_value=[]),
        patch("app.services.report_generator.detect_macro_signals", return_value=_quiet_signals()),
        patch("app.services.report_generator.detect_price_anomalies", return_value=[]),
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
    w = rg._build_data_window(news, portfolio, "2026-06-04")
    assert "Data window" in w
    assert "2026-06-01 08:00" in w
    assert "FX as of 2026-06-03" in w
    assert "prior close" in w


def _normal_path_patches() -> list[object]:
    return [
        patch("app.services.report_generator.get_current_user_id", return_value=_USER),
        patch("app.services.report_generator.compute_portfolio", return_value=_portfolio_snap()),
        patch("app.services.report_generator.fetch_news", return_value=[_news_item("Fed")]),
        patch("app.services.report_generator.detect_macro_signals", return_value=_macro_hit()),
        patch("app.services.report_generator.detect_price_anomalies", return_value=[_anomaly()]),
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
            "app.services.report_generator.fetch_news",
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
        "## §2 Macro Signals\n\nReanalyzed view. [For information only — not investment advice]"
    )
    with (
        patch("app.services.report_generator.get_current_user_id", return_value=_USER),
        patch("app.services.report_generator._openrouter_client", return_value=MagicMock()),
        patch("app.services.report_generator._call_llm", return_value=new_body),
        patch(
            "app.services.report_generator.fetch_news",
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


def test_build_section1_groups_by_market_in_upload_order_with_subtotals() -> None:
    portfolio = {
        "base_currency": "USD",
        "fx_date": "2026-06-06",
        "total_base": 300.0,
        "by_market": {"US": 200.0, "HK": 100.0},
        "by_currency": {},
        "by_asset_type": {},
        "holdings": [
            # Deliberately out of position order; US appears first in the file.
            {
                "name": "Alpha",
                "market": "US",
                "market_value": 100,
                "market_value_base": 100.0,
                "position": 0,
            },
            {
                "name": "Bravo",
                "market": "HK",
                "market_value": 100,
                "market_value_base": 100.0,
                "position": 1,
            },
            {
                "name": "Charlie",
                "market": "US",
                "market_value": 100,
                "market_value_base": 100.0,
                "position": 2,
            },
        ],
    }
    md = rg._build_section1(portfolio)
    # US group (Alpha, Charlie) before HK group (Bravo); each group subtotaled.
    assert md.index("Alpha") < md.index("Charlie") < md.index("Bravo")
    assert "**US subtotal**" in md
    assert "**HK subtotal**" in md
    assert md.index("US subtotal") < md.index("Bravo")  # US block closes before HK


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
# Tests: F2 _annotate_sources (unit, no DB)
# ---------------------------------------------------------------------------


def _simple_portfolio(tickers: list[str]) -> dict[str, object]:
    return {
        "holdings": [{"ticker": t, "fund_code": None} for t in tickers],
        "total_base": 10000,
    }


def test_annotate_sources_collapses_news_citations() -> None:
    text = "The Fed raised rates [S1] and markets reacted [S12]."
    result = rg._annotate_sources(text, _simple_portfolio([]))
    assert result.count("[新闻]") == 2
    assert "[S1]" not in result
    assert "[S12]" not in result
    # Whitespace around citations is preserved (no word-joining).
    assert "rates [新闻] and" in result


def test_annotate_sources_collapses_consecutive_citation_run() -> None:
    text = "Markets moved [S6][S7][S8] [S9][S10] sharply."
    result = rg._annotate_sources(text, _simple_portfolio([]))
    assert result.count("[新闻]") == 1  # the whole run becomes one marker
    assert "S6" not in result and "S10" not in result
    assert "moved [新闻] sharply" in result


def test_annotate_sources_injects_xingqing_on_ticker_line() -> None:
    text = "AAPL represents 100.0% of the portfolio."
    result = rg._annotate_sources(text, _simple_portfolio(["AAPL"]))
    assert "[行情]" in result


def test_annotate_sources_skips_xingqing_when_news_cited() -> None:
    # After step 1, [S1] becomes [新闻] — so [行情] must not be added.
    text = "AAPL declined 9% according to reports [S1]."
    result = rg._annotate_sources(text, _simple_portfolio(["AAPL"]))
    assert "[行情]" not in result
    assert "[新闻]" in result


def test_annotate_sources_no_xingqing_without_ticker() -> None:
    text = "Concentration is above the watch threshold."
    result = rg._annotate_sources(text, _simple_portfolio(["AAPL"]))
    assert "[行情]" not in result


def test_annotate_sources_injects_fenxi_before_compliance_marker() -> None:
    marker = rg._COMPLIANCE_MARKER
    text = f"Rates may pressure valuations. {marker}"
    result = rg._annotate_sources(text, _simple_portfolio([]))
    assert f"[分析] {marker}" in result
    assert marker in result


def test_annotate_sources_no_duplicate_fenxi() -> None:
    marker = rg._COMPLIANCE_MARKER
    text = f"Rates may pressure valuations. {marker}"
    result = rg._annotate_sources(text, _simple_portfolio([]))
    # Idempotency: running twice must not double-inject.
    result2 = rg._annotate_sources(result, _simple_portfolio([]))
    assert result2.count("[分析]") == result.count("[分析]")


def test_annotate_sources_combined_ticker_and_compliance() -> None:
    marker = rg._COMPLIANCE_MARKER
    text = f"AAPL makes up the entire portfolio and warrants monitoring. {marker}"
    result = rg._annotate_sources(text, _simple_portfolio(["AAPL"]))
    assert "[行情]" in result
    assert "[分析]" in result
    assert f"[分析] {marker}" in result


def test_annotate_sources_fund_code_triggers_xingqing() -> None:
    portfolio = {"holdings": [{"ticker": None, "fund_code": "005827"}], "total_base": 5000}
    text = "Fund 005827 represents a significant allocation."
    result = rg._annotate_sources(text, portfolio)
    assert "[行情]" in result


def test_annotate_sources_empty_portfolio_no_xingqing() -> None:
    text = "AAPL declined 9%. [For information only — not investment advice]"
    result = rg._annotate_sources(text, {"holdings": [], "total_base": 0})
    assert "[行情]" not in result
    assert "[分析]" in result


# ---------------------------------------------------------------------------
# Tests: F2 integration — annotations present in generated report
# ---------------------------------------------------------------------------


def _mock_llm_f2(
    client: object, model: str, system: str, user: str, *, with_holdings: bool = False
) -> str:
    if with_holdings:
        return _FAKE_LLM_PASS2_F2
    return _FAKE_LLM_PASS1


def test_generate_report_f2_news_annotations_in_output(db_session: Session) -> None:
    """Generated report must collapse LLM [S#] citations into bare [新闻] markers."""
    with (
        patch("app.services.report_generator.get_current_user_id", return_value=_USER),
        patch("app.services.report_generator.compute_portfolio", return_value=_portfolio_snap()),
        patch(
            "app.services.report_generator.fetch_news",
            return_value=[_news_item("Fed raises rates")],
        ),
        patch("app.services.report_generator.detect_macro_signals", return_value=_macro_hit()),
        patch("app.services.report_generator.detect_price_anomalies", return_value=[_anomaly()]),
        patch("app.services.report_generator._openrouter_client", return_value=MagicMock()),
        patch("app.services.report_generator._call_llm", side_effect=_mock_llm_f2),
        patch(
            "app.services.report_generator._run_tavily_search", return_value=_FAKE_TAVILY_RESULTS
        ),
    ):
        report = rg.generate_report(db_session, report_date=_TODAY)

    assert report.status == "success"
    assert report.report_md is not None
    # [S1] from LLM must be collapsed to a bare [新闻] marker
    assert "[新闻]" in report.report_md
    assert "[S1]" not in report.report_md


def test_generate_report_f2_xingqing_annotation_in_output(db_session: Session) -> None:
    """Lines referencing portfolio tickers without news citations must carry [行情]."""
    with (
        patch("app.services.report_generator.get_current_user_id", return_value=_USER),
        patch("app.services.report_generator.compute_portfolio", return_value=_portfolio_snap()),
        patch(
            "app.services.report_generator.fetch_news",
            return_value=[_news_item("Fed raises rates")],
        ),
        patch("app.services.report_generator.detect_macro_signals", return_value=_macro_hit()),
        patch("app.services.report_generator.detect_price_anomalies", return_value=[_anomaly()]),
        patch("app.services.report_generator._openrouter_client", return_value=MagicMock()),
        patch("app.services.report_generator._call_llm", side_effect=_mock_llm_f2),
        patch(
            "app.services.report_generator._run_tavily_search", return_value=_FAKE_TAVILY_RESULTS
        ),
    ):
        report = rg.generate_report(db_session, report_date=_TODAY)

    # AAPL line in _FAKE_LLM_PASS2_F2 has no [S#] → must get [行情]
    assert report.report_md is not None
    assert "[行情]" in report.report_md


def test_generate_report_f2_fenxi_annotation_in_output(db_session: Session) -> None:
    """Analytical conclusions must carry [分析] before the compliance marker."""
    with (
        patch("app.services.report_generator.get_current_user_id", return_value=_USER),
        patch("app.services.report_generator.compute_portfolio", return_value=_portfolio_snap()),
        patch(
            "app.services.report_generator.fetch_news",
            return_value=[_news_item("Fed raises rates")],
        ),
        patch("app.services.report_generator.detect_macro_signals", return_value=_macro_hit()),
        patch("app.services.report_generator.detect_price_anomalies", return_value=[_anomaly()]),
        patch("app.services.report_generator._openrouter_client", return_value=MagicMock()),
        patch("app.services.report_generator._call_llm", side_effect=_mock_llm_f2),
        patch(
            "app.services.report_generator._run_tavily_search", return_value=_FAKE_TAVILY_RESULTS
        ),
    ):
        report = rg.generate_report(db_session, report_date=_TODAY)

    assert report.report_md is not None
    assert "[分析]" in report.report_md
    assert f"[分析] {rg._COMPLIANCE_MARKER}" in report.report_md


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
            "app.services.report_generator.fetch_news",
            return_value=[_news_item("Fed raises rates")],
        ),
        patch("app.services.report_generator.detect_macro_signals", return_value=_macro_hit()),
        patch("app.services.report_generator.detect_price_anomalies", return_value=[_anomaly()]),
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
        patch("app.services.report_generator.fetch_news", return_value=[]),
        patch("app.services.report_generator.detect_macro_signals", return_value=_quiet_signals()),
        patch("app.services.report_generator.detect_price_anomalies", return_value=[]),
        patch("app.services.report_generator._call_llm") as mock_llm,
    ):
        report = rg.generate_report(db_session, report_date=_TODAY)

    assert report.status == "skipped"
    mock_llm.assert_not_called()
    assert report.report_md is not None
    assert "免责声明" in report.report_md
    assert "Data Sources & Disclaimer" in report.report_md
