"""Tests for report_generator (F1).

Strategy:
- All external calls (LLM, Tavily, news_fetcher, macro_detector,
  price_anomaly_detector) are mocked via patch.
- DB operations use the db_session fixture (real Postgres).
- Tests cover: normal path, quiet-day skip, LLM failure, Tavily failure (degraded).
"""

from __future__ import annotations

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
# Tests: section builders (unit, no DB)
# ---------------------------------------------------------------------------


def test_build_section1_contains_required_rows() -> None:
    portfolio = rg._serialize_portfolio(_portfolio_snap())
    md = rg._build_section1(portfolio)
    assert "§1 Portfolio Snapshot" in md
    assert "Apple Inc." in md
    assert "AAPL" in md
    assert "10,000" in md or "10000" in md


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
