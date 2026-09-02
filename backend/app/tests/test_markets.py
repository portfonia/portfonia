"""Suffix classification and two-way market resolution (issue #311)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.markets import (
    CAPTURE_MARKET_ORDER,
    SUPPORTED_CAPTURE_MARKETS,
    VALID_HOLDING_MARKETS,
    is_capture_supported,
    market_from_ticker,
    resolve_holding_market,
    yf_batch_key,
)


@pytest.mark.parametrize(
    "ticker,expected",
    [
        ("VOD.L", "UK"),
        ("BARC.L", "UK"),
        ("psh.l", "UK"),
        ("ASML.AS", "Europe"),
        ("MC.PA", "Europe"),
        ("SAP.DE", "Europe"),
        ("7203.T", "Japan"),
        ("005930.KS", "Korea"),
        ("035420.KQ", "Korea"),
        ("0700.HK", "HK"),
        ("600519.SS", "A-Share"),
        ("000858.SZ", "A-Share"),
        ("AAPL", "US"),
        ("BRK.B", "US"),
        ("BF.A", "US"),
        ("BHP.AX", None),
        ("SHOP.TO", None),
        ("NESN.SW", None),
        ("", None),
        (None, None),
    ],
)
def test_market_from_ticker_classifies_supported_suffixes(
    ticker: str | None, expected: str | None
) -> None:
    assert market_from_ticker(ticker) == expected


def test_closed_sets_are_the_seven_plus_other() -> None:
    assert CAPTURE_MARKET_ORDER == (
        "US",
        "HK",
        "A-Share",
        "UK",
        "Europe",
        "Japan",
        "Korea",
    )
    assert frozenset(CAPTURE_MARKET_ORDER) == SUPPORTED_CAPTURE_MARKETS
    assert SUPPORTED_CAPTURE_MARKETS | {"Other"} == VALID_HOLDING_MARKETS
    assert "Other" not in SUPPORTED_CAPTURE_MARKETS


def test_yf_batch_key_groups_new_markets_away_from_us() -> None:
    assert yf_batch_key("VOD.L") == "uk"
    assert yf_batch_key("ASML.AS") == "europe"
    assert yf_batch_key("7203.T") == "japan"
    assert yf_batch_key("005930.KS") == "korea"
    assert yf_batch_key("AAPL") == "us"
    assert yf_batch_key("BHP.AX") == "other"


def test_resolve_unresolvable_ticker_is_other_not_processed() -> None:
    market, supported = resolve_holding_market(ticker="BHP.AX", declared_market="US")
    assert market == "Other"
    assert supported is False


def test_resolve_lse_ticker_is_uk_and_capture_supported() -> None:
    market, supported = resolve_holding_market(ticker="VOD.L", declared_market=None)
    assert market == "UK"
    assert supported is True


def test_resolve_declared_supported_market_wins_over_us_ticker() -> None:
    market, supported = resolve_holding_market(ticker="AAPL", declared_market="HK")
    assert market == "HK"
    assert supported is True


def test_resolve_declared_other_does_not_override_resolvable_ticker() -> None:
    market, supported = resolve_holding_market(ticker="VOD.L", declared_market="Other")
    assert market == "UK"
    assert supported is True


def test_resolve_cash_is_other_but_capture_supported() -> None:
    market, supported = resolve_holding_market(
        ticker=None,
        declared_market=None,
        asset_type="cash",
        pricing_mode="manual",
    )
    assert market == "Other"
    assert supported is True


def test_resolve_fund_code_is_a_share_and_supported() -> None:
    market, supported = resolve_holding_market(
        ticker=None, declared_market=None, fund_code="110011"
    )
    assert market == "A-Share"
    assert supported is True


def test_is_capture_supported_keys_off_flag_not_other() -> None:
    assert is_capture_supported(SimpleNamespace(capture_supported=True, market="Other"))
    assert not is_capture_supported(SimpleNamespace(capture_supported=False, market="UK"))
