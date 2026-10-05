"""Large-holding selection (moved from test_ticker_intel.py, issue #640)."""

from __future__ import annotations

from typing import Any

from app.services import portfolio_weights as pw


def test_holding_identifier_normalizes_known_collision_ticker() -> None:
    """issue #204 PR #253 review: _holding_identifier fed
    large_weight_identifiers/_weighted_identifiers, which must key a raw
    un-padded HK holding ("700.HK") under '0700.HK' — the same identifier
    compute_global_moves and select_user_anomalies use — or top-weight
    selection silently loses it."""
    assert pw._holding_identifier({"ticker": "700.HK"}) == "0700.HK"


def _split_lot_holdings() -> list[dict[str, Any]]:
    """VOO split across two lots (140k + 130k = 270k combined, this book's
    largest single exposure) alongside five other distinct single-lot
    holdings. Sized so the bug is unambiguous either way `top_k=5` picks
    without per-identifier aggregation: each VOO lot (140k/130k) individually
    outranks BBB (120k)/CCC (110k), so an UNaggregated top-5 by raw weight
    is [AAA, VOO, VOO, BBB, CCC] — VOO occupies two slots (a duplicate
    identifier) and DDD (100k), the fifth genuinely distinct holding, is
    wrongly pushed out. Aggregated, VOO's combined 270k is the single largest
    identifier and the correct top-5 is [VOO, AAA, BBB, CCC, DDD]."""
    return [
        {"ticker": "VOO", "market_value_base": 140_000.0, "asset_class": "EQUITY_US_BROAD"},
        {"ticker": "AAA", "market_value_base": 150_000.0, "asset_class": "STOCK"},
        {"ticker": "VOO", "market_value_base": 130_000.0, "asset_class": "EQUITY_US_BROAD"},
        {"ticker": "BBB", "market_value_base": 120_000.0, "asset_class": "STOCK"},
        {"ticker": "CCC", "market_value_base": 110_000.0, "asset_class": "STOCK"},
        {"ticker": "DDD", "market_value_base": 100_000.0, "asset_class": "STOCK"},
    ]


def test_large_weight_identifiers_aggregates_split_lots_of_same_identifier() -> None:
    """PR #168 review round 1 suggestion: a holding split across two lots
    (this product preserves upload order, so the same ticker legitimately
    appears as more than one `Holding` row — VOO is the worked example in
    CLAUDE.md) must be combined by identifier BEFORE ranking, not ranked as
    two separate half-sized entries. Un-aggregated, the two VOO rows can each
    independently qualify for `top_k` and take two of its slots, silently
    evicting a genuinely distinct 5th holding (DDD) that should have made the
    cut on its own combined weight ranking."""
    holdings = _split_lot_holdings()
    result = pw.large_weight_identifiers(holdings, portfolio_total=840_000.0)
    assert result == ["VOO", "AAA", "BBB", "CCC", "DDD"]
    assert result.count("VOO") == 1, f"VOO must occupy exactly one slot, got {result}"
    assert "DDD" in result, "DDD must not be evicted by VOO's un-aggregated duplicate slot"
    assert len(result) == len(set(result)), f"no identifier should repeat: {result}"
