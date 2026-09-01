"""Tests for report_sections.py (code-built report sections).

Split out of test_report_generator.py (#37).
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from app.services import report_sections as sec
from app.services import report_serializers as rs
from app.services.portfolio_calculator import Concentration, HoldingValue, PortfolioSnapshot

_NOW = datetime(2026, 6, 4, 20, 0, tzinfo=UTC)
_TODAY = date(2026, 6, 4)


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


# ---------------------------------------------------------------------------
# Tests: _build_data_window
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
    w = sec._build_data_window(
        news, portfolio, "2026-06-01T16:00:00+00:00", "2026-06-04T20:30:00+00:00", 3
    )
    assert "Data window" in w
    assert "2026-06-01 12:00 to 2026-06-04 16:30 ET" in w
    assert "3 trading day(s)" in w
    assert "FX as of 2026-06-03" in w
    assert "baseline close" in w


# ---------------------------------------------------------------------------
# Tests: §1 Portfolio Snapshot
# ---------------------------------------------------------------------------


def test_build_section1_contains_required_rows() -> None:
    portfolio = rs._serialize_portfolio(_portfolio_snap())
    md = sec._build_section1(portfolio)
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
    md = sec._build_section1(portfolio)
    # IBKR group (Alpha, Charlie) before Futu group (Bravo); each group subtotaled.
    assert md.index("Alpha") < md.index("Charlie") < md.index("Bravo")
    assert "**IBKR subtotal**" in md
    assert "**Futu subtotal**" in md
    assert "**Other subtotal**" in md  # broker-less holding bucketed into Other
    assert md.index("IBKR subtotal") < md.index("Bravo")  # IBKR block closes before Futu
    assert "Custodian" in md  # column header renamed from Market


# ---------------------------------------------------------------------------
# Tests: §4.2 Price anomalies table
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
    md = sec._build_section42_table([_anomaly_dict()])
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
    md = sec._build_section42_table(
        [{"name": "X", "identifier": "X", "trigger": "cumulative", "window_net_pct": 0.04}]
    )
    assert "X (X)" in md
    assert "—" in md  # missing cells rendered as em-dash placeholder


def test_inject_section42_table_inserts_after_heading() -> None:
    body = "## §4 Risk Radar\n### 4.2 Price anomalies\nNVDA — chip-cycle optimism.\n"
    out = sec._inject_section42_table(body, "TABLE_ROWS")
    assert out.index("TABLE_ROWS") < out.index("NVDA — chip-cycle optimism")
    assert out.index("### 4.2") < out.index("TABLE_ROWS")  # table sits under the heading


def test_inject_section42_table_fallback_appends_when_heading_absent() -> None:
    body = "## §4 Risk Radar\n### 4.1 Concentration\nflagged.\n"
    out = sec._inject_section42_table(body, "TABLE_ROWS")
    assert "### 4.2 Price anomalies" in out
    assert "TABLE_ROWS" in out


# ---------------------------------------------------------------------------
# Tests: §4.4 Technical position
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
    md = sec._build_section44_technical([_tech_dict()])
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
    md = sec._build_section44_technical([sparse])
    assert "Insufficient captured price history" in md
    assert "| Holding |" not in md  # no table rendered


def test_build_section44_partial_metrics_render_dash() -> None:
    md = sec._build_section44_technical([_tech_dict(pct_vs_sma200=None)])
    assert "| Holding |" in md  # table rendered (some metric present)
    assert "—" in md  # the missing 200-day cell


# ---------------------------------------------------------------------------
# Tests: §2.5 Forward Calendar
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
    exposed, watch = sec._forward_exposure(
        {"event_type": "macro", "name": "Consumer Price Index (CPI)"}, _fwd_holdings()
    )
    assert "NVIDIA" in exposed and "SPDR Gold" in exposed  # tech + gold
    assert "Tencent" not in exposed  # non-US excluded
    assert "inflation" in watch and "rise" not in watch  # observation, not forecast


def test_forward_exposure_earnings_maps_exact_ticker() -> None:
    exposed, _ = sec._forward_exposure(
        {"event_type": "earnings", "name": "NVDA", "ticker": "NVDA"}, _fwd_holdings()
    )
    assert exposed == ["NVIDIA"]


def test_forward_exposure_retail_maps_consumer_only() -> None:
    exposed, _ = sec._forward_exposure(
        {"event_type": "macro", "name": "Retail Sales"}, _fwd_holdings()
    )
    assert exposed == ["Costco"]


def test_forward_delay_risk_detects_funding_lapse() -> None:
    assert sec._forward_delay_risk([{"title": "Government shutdown looms", "summary": ""}]) is True
    assert (
        sec._forward_delay_risk([{"title": "Tech stocks rally", "summary": "strong demand"}])
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
    md = sec._build_forward_block(events, _fwd_holdings(), news)
    assert "## §2.5 Forward Calendar" in md
    assert "2026-06-17" in md and "FOMC Statement" in md
    assert "calendar facts, not forecasts" in md
    assert "delay scheduled BLS/BEA releases" in md  # caveat appended


def test_inject_forward_block_inserts_before_section3() -> None:
    body = "## §2 Macro Signals\nstuff\n## §3 Holdings Analysis\nmore"
    out = sec._inject_forward_block(body, "## §2.5 Forward Calendar\nX")
    assert out.index("§2.5") < out.index("## §3")
    assert out.index("## §2 ") < out.index("§2.5")


# ---------------------------------------------------------------------------
# Tests: R-5 price-data cutoff + FX-stale flag
# ---------------------------------------------------------------------------


def test_data_window_states_price_cutoff_and_no_intraday() -> None:
    w = sec._build_data_window(
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
    # FX dated 2026-06-04 against a 2026-06-10 cutoff → 6 days → flagged.
    w = sec._build_data_window(
        [],
        {"fx_date": "2026-06-04"},
        "2026-06-09T12:00:00+00:00",
        "2026-06-10T20:30:00+00:00",
        1,
    )
    assert "FX rate is stale" in w


def test_data_window_no_stale_flag_when_fx_current() -> None:
    w = sec._build_data_window(
        [],
        {"fx_date": "2026-06-10"},
        "2026-06-09T12:00:00+00:00",
        "2026-06-10T20:30:00+00:00",
        1,
    )
    assert "FX rate is stale" not in w


def test_data_window_no_stale_flag_when_fx_gap_is_normal_weekend() -> None:
    # Fri 2026-06-05 rate read on a Tue 2026-06-09 cutoff = 4 calendar days —
    # the normal weekend cadence (issue #299). A >1-day threshold used to
    # false-positive on essentially every Monday/holiday-adjacent report.
    w = sec._build_data_window(
        [],
        {"fx_date": "2026-06-05"},
        "2026-06-08T12:00:00+00:00",
        "2026-06-09T20:30:00+00:00",
        1,
    )
    assert "FX rate is stale" not in w


def test_data_window_flags_stale_fx_gap_of_five_days() -> None:
    # 5+ calendar days means the capture pipeline itself is suspect, not the
    # calendar (issue #299) — the R-4 "rates frozen 6 days" case still alerts.
    w = sec._build_data_window(
        [],
        {"fx_date": "2026-06-04"},
        "2026-06-08T12:00:00+00:00",
        "2026-06-09T20:30:00+00:00",
        1,
    )
    assert "FX rate is stale" in w


# ---------------------------------------------------------------------------
# Tests: R-6 T+0 calendar promotion
# ---------------------------------------------------------------------------


def test_today_events_block_promotes_same_day_event() -> None:
    events = [
        {
            "event_type": "macro",
            "name": "Consumer Price Index (CPI)",
            "scheduled_date": "2026-06-10",
        },
        {"event_type": "macro", "name": "FOMC Statement", "scheduled_date": "2026-06-17"},
    ]
    block = sec._build_today_events_block(events, _fwd_holdings(), "2026-06-10")
    assert "Today's scheduled events" in block
    assert "Consumer Price Index" in block
    assert "FOMC" not in block  # future event stays in §2.5 only
    assert "results not yet in this report's data" in block


def test_today_events_block_empty_when_none_today() -> None:
    events = [{"event_type": "macro", "name": "FOMC", "scheduled_date": "2026-06-17"}]
    assert sec._build_today_events_block(events, _fwd_holdings(), "2026-06-10") == ""


def test_inject_today_events_lands_under_section2_heading() -> None:
    body = "## §2 Macro Signals\nprose\n## §3 Holdings\nmore"
    out = sec._inject_today_events(body, "**Today's scheduled events**: CPI")
    assert out.index("## §2 Macro Signals") < out.index("Today's scheduled events")
    assert out.index("Today's scheduled events") < out.index("## §3")


def test_forward_block_tags_today_row() -> None:
    events = [{"event_type": "macro", "name": "CPI", "scheduled_date": "2026-06-10"}]
    md = sec._build_forward_block(events, _fwd_holdings(), [], report_date_str="2026-06-10")
    assert "2026-06-10 (today)" in md


# ---------------------------------------------------------------------------
# Tests: F3 fixed footer
# ---------------------------------------------------------------------------


def test_build_footer_contains_fx_date() -> None:
    portfolio = {"base_currency": "USD", "fx_date": "2026-06-04", "holdings": []}
    footer = sec._build_footer(portfolio)
    assert "2026-06-04" in footer
    assert "USD" in footer


def test_build_footer_contains_bilingual_disclaimer() -> None:
    portfolio = {"base_currency": "USD", "fx_date": "2026-06-04", "holdings": []}
    footer = sec._build_footer(portfolio)
    assert "Disclaimer" in footer
    assert "免责声明" in footer
    assert "investment advice" in footer
    assert "投资建议" in footer


def test_build_footer_starts_with_separator() -> None:
    portfolio = {"base_currency": "USD", "fx_date": "2026-06-04", "holdings": []}
    footer = sec._build_footer(portfolio)
    assert "---" in footer
