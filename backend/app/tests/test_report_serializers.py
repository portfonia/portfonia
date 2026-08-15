"""Tests for report_serializers.py.

Split out of test_report_generator.py (#37).
"""

from __future__ import annotations

from decimal import Decimal

from app.services import report_serializers as rs
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
