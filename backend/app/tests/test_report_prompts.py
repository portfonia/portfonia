"""Tests for report_prompts.py (Pass 1 / Pass 2 prompt text).

Split out of test_report_generator.py (#37).
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from app.services import report_prompts as rp
from app.services import report_serializers as rs
from app.services.i18n_glossary import load_i18n_glossary
from app.services.macro_detector import MacroSignals, ThemeHit
from app.services.news_fetcher import NewsItem
from app.services.portfolio_calculator import Concentration, HoldingValue, PortfolioSnapshot

_NOW = datetime(2026, 6, 4, 20, 0, tzinfo=UTC)
_TODAY = date(2026, 6, 4)


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


def test_pass1_prompt_excludes_holdings_derived_anomalies() -> None:
    """DATA ISOLATION: Pass 1 runs without data_collection=deny, so it must not
    carry holdings-derived identifiers. Price anomalies (name/ticker = a held
    position) belong only in Pass 2."""
    signals = _macro_hit()
    news = [_news_item("Fed raises rates")]
    prompt = rp._build_pass1_prompt(signals, news)

    # Anomaly identifiers from a user's holdings must never appear in Pass 1.
    assert "NVDA" not in prompt
    assert "NVIDIA" not in prompt
    assert "PRICE ANOMALIES" not in prompt
    # Public signal/news content is still present.
    assert "MACRO SIGNAL THEMES" in prompt
    assert "TOP HEADLINES" in prompt


def _vendor_zh() -> str:
    return load_i18n_glossary().vendor_names["Tiantian Fund"]["zh-Hans"]


def test_stale_ticker_hint_fund_code() -> None:
    vendor_zh = _vendor_zh()
    assert "CN mutual fund" in rp._stale_ticker_hint("005827", vendor_zh)
    assert vendor_zh in rp._stale_ticker_hint("005827", vendor_zh)
    assert "005827" in rp._stale_ticker_hint("005827", vendor_zh)


def test_stale_ticker_hint_a_share_ss() -> None:
    result = rp._stale_ticker_hint("600519.SS", _vendor_zh())
    assert "A-share" in result
    assert "Shanghai" in result


def test_stale_ticker_hint_a_share_sz() -> None:
    result = rp._stale_ticker_hint("000858.SZ", _vendor_zh())
    assert "A-share" in result
    assert "Shenzhen" in result


def test_stale_ticker_hint_hk() -> None:
    result = rp._stale_ticker_hint("0700.HK", _vendor_zh())
    assert "HK-listed" in result


def test_stale_ticker_hint_us_stock() -> None:
    result = rp._stale_ticker_hint("AAPL", _vendor_zh())
    assert "stock ticker" in result


def test_stale_ticker_hint_in_pass2_prompt() -> None:
    """Fund code in stale_tickers must appear with CN hint in the Pass 2 prompt."""
    portfolio = rs._serialize_portfolio(_portfolio_snap())
    portfolio["stale_tickers"] = ["005827", "AAPL", "0700.HK"]
    prompt = rp._build_pass2_prompt(portfolio, {}, [], [])
    assert "CN mutual fund" in prompt
    assert "stock ticker" in prompt
    assert "HK-listed" in prompt


def test_pass2_prompt_default_requests_all_three_narrative_sections() -> None:
    prompt = rp._build_pass2_prompt(rs._serialize_portfolio(_portfolio_snap()), {}, [], [])
    assert "## §2 Macro Signals" in prompt
    assert "## §3 Holdings Analysis" in prompt
    assert "## §4 Risk Radar" in prompt


def test_pass2_prompt_enabled_sections_restricts_instructions() -> None:
    """Ring 1 prep: a report type that only wants §2 must not get §3/§4 instructions."""
    prompt = rp._build_pass2_prompt(
        rs._serialize_portfolio(_portfolio_snap()),
        {},
        [],
        [],
        enabled_sections=frozenset({"§2"}),
    )
    assert "## §2 Macro Signals" in prompt
    assert "## §3 Holdings Analysis" not in prompt
    assert "## §4 Risk Radar" not in prompt


def test_pass2_prompt_42_asks_for_drivers_not_restated_numbers() -> None:
    prompt = rp._build_pass2_prompt(rs._serialize_portfolio(_portfolio_snap()), {}, [], [])
    # The numeric table is code-built; the model must not restate the arc numbers.
    assert "do NOT restate those numbers" in prompt
    assert "IDENTIFIER — <driver> [Label]" in prompt


def test_pass2_prompt_defines_evidence_ordinal_labels() -> None:
    prompt = rp._build_pass2_prompt(rs._serialize_portfolio(_portfolio_snap()), {}, [], [])
    assert "CONFIDENCE LABELS" in prompt
    for label in ("[Established]", "[Probable]", "[Speculative]"):
        assert label in prompt
    # Calibrated honesty, not manufactured certainty: never a numeric percentage,
    # and a large unexplained move is kept (labelled), not dropped.
    assert "NEVER a numeric percentage" in prompt
    assert "do not drop or downgrade a large unexplained move" in prompt.lower()


def test_pass2_system_forbids_forecasting_scheduled_events() -> None:
    assert "FORWARD EVENTS" in rp._PASS2_SYSTEM
    assert "NEVER predict its outcome" in rp._PASS2_SYSTEM


def test_pass2_system_restricts_section42_cross_reference() -> None:
    # R-8: 'see §4.2' may only point at holdings actually in the anomaly table.
    assert "§4.2 CROSS-REFERENCES" in rp._PASS2_SYSTEM
    assert "did not cross" in rp._PASS2_SYSTEM


def test_pass2_system_requires_the_causal_chain_not_just_names() -> None:
    """Issue #128 narrative-layer redesign, 2026-08-20 design amendment
    ("make Pass 2 write the connection again, not just name it"): the v5
    compare's TSM section named
    Apple/Nvidia/Taiwan without ever writing how Anthropic's capex reaches
    TSM's own process nodes — naming a related entity is not the same as
    stating the transmission. This locks the hardened instruction that makes
    that distinction explicit and un-skippable."""
    assert "NAMING IS NOT ANALYSIS" in rp._PASS2_SYSTEM
    assert "signal -> transmission channel -> this specific holding" in rp._PASS2_SYSTEM
    assert "does not satisfy the mechanism requirement" in rp._PASS2_SYSTEM


def test_pass2_prompt_includes_large_holding_window_price() -> None:
    """Design amendment item 3: a large holding below the anomaly threshold
    (e.g. TSM at 22.5% weight, +1.22% on 2026-08-17) previously had NO price
    fact anywhere in the prompt. `large_holding_moves` supplies exactly that
    number in its own section, separate from PRICE ANOMALIES."""
    prompt = rp._build_pass2_prompt(
        rs._serialize_portfolio(_portfolio_snap()),
        {},
        [],
        [],
        large_holding_moves={"TSM": 0.0122},
    )
    assert "LARGE HOLDINGS WINDOW PRICE" in prompt
    assert "TSM: +1.22% this report period" in prompt


def test_pass2_prompt_omits_large_holding_block_when_empty() -> None:
    prompt = rp._build_pass2_prompt(
        rs._serialize_portfolio(_portfolio_snap()), {}, [], [], large_holding_moves={}
    )
    assert "LARGE HOLDINGS WINDOW PRICE" not in prompt


def test_direction_requires_evidence_accepts_large_holding_price_as_grounding() -> None:
    # The grounding sources list must include the new section, not just the
    # pre-existing two — otherwise a strict reading of the rule would still
    # forbid stating TSM's own supplied window move.
    assert "LARGE HOLDINGS WINDOW PRICE" in rp._RULE_DIRECTION_REQUIRES_EVIDENCE
