"""Tests for report_serializers.py.

Split out of test_report_generator.py (#37).
"""

from __future__ import annotations

from decimal import Decimal

from app.services import report_serializers as rs
from app.services.portfolio_calculator import HoldingValue, PortfolioSnapshot
from app.services.price_anomaly_detector import PriceAnomaly


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


def test_serialize_anomalies_float_conversion() -> None:
    anomalies = [_anomaly()]
    result = rs._serialize_anomalies(anomalies)
    assert len(result) == 1
    a = result[0]
    assert a["identifier"] == "NVDA"
    assert isinstance(a["pct_change"], float)
    assert abs(a["pct_change"] - 0.0909) < 0.001


def _holding_value(**overrides: object) -> HoldingValue:
    import uuid

    defaults: dict[str, object] = dict(
        holding_id=uuid.uuid4(),
        name="Tracked Startup",
        ticker="TRACK",
        fund_code=None,
        currency="USD",
        asset_type="stock",
        asset_class="STOCK",
        sector=None,
        market="US",
        market_value=Decimal("0.00"),
        market_value_base=Decimal("0.00"),
        price_as_of=None,
    )
    defaults.update(overrides)
    return HoldingValue(**defaults)  # type: ignore[arg-type]


def test_serialize_portfolio_includes_watch_tier() -> None:
    """Issue #421: watch_tier must survive the PortfolioSnapshot -> dict
    round-trip that report_generator._build_holding_check_inputs reads —
    this hand-written serializer (unlike ReportContext.to_jsonb's
    dataclasses.asdict) does not propagate new HoldingValue fields
    automatically."""
    snap = PortfolioSnapshot(
        base_currency="USD",
        holdings=[_holding_value(watch_tier="critical"), _holding_value(watch_tier=None)],
    )
    result = rs._serialize_portfolio(snap)
    watch_tiers = [h["watch_tier"] for h in result["holdings"]]
    assert watch_tiers == ["critical", None]
