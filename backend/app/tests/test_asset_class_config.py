"""Tests for the admin-editable asset_class threshold config (#35)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from app.services.asset_class_config import VALID_ASSET_CLASSES, load_asset_class_config

_VALID_YAML = """
asset_classes:
  STOCK: {anomaly: {per_day: 0.05, cumulative_cap: 0.10}, concentration: {watch: 0.10, high: 0.20}}
  EQUITY_US_BROAD: {anomaly: {per_day: 0.05, cumulative_cap: 0.40}, concentration: {watch: 0.30, high: 0.45}}
  EQUITY_US_TECH: {anomaly: {per_day: 0.05, cumulative_cap: 0.35}, concentration: {watch: 0.20, high: 0.35}}
  EQUITY_DM: {anomaly: {per_day: 0.05, cumulative_cap: 0.30}, concentration: {watch: 0.20, high: 0.35}}
  EQUITY_CN: {anomaly: {per_day: 0.05, cumulative_cap: 0.20}, concentration: {watch: 0.20, high: 0.35}}
  EQUITY_EM: {anomaly: {per_day: 0.05, cumulative_cap: 0.25}, concentration: {watch: 0.20, high: 0.35}}
  EQUITY_BROAD: {anomaly: {per_day: 0.05, cumulative_cap: 0.40}, concentration: {watch: 0.30, high: 0.45}}
  REIT: {anomaly: {per_day: 0.05, cumulative_cap: 0.25}, concentration: {watch: 0.20, high: 0.35}}
  PRECIOUS_METALS: {anomaly: {per_day: 0.04, cumulative_cap: 0.20}, concentration: {watch: 0.15, high: 0.25}}
  ENERGY: {anomaly: {per_day: 0.06, cumulative_cap: 0.35}, concentration: {watch: 0.10, high: 0.15}}
  COMMODITY: {anomaly: {per_day: 0.05, cumulative_cap: 0.25}, concentration: {watch: 0.12, high: 0.20}}
  BOND_FUND: {anomaly: {per_day: 0.02, cumulative_cap: 0.20}, concentration: {watch: 0.25, high: 0.40}}
  CASH_EQUIV: {anomaly: {per_day: 0.01, cumulative_cap: 0.20}, concentration: {watch: 0.50, high: 0.70}}
concentration_global:
  top3_watch: 0.50
  asset_class_bucket_watch: 0.50
  asset_class_bucket_high: 0.65
"""


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "asset_class_thresholds.yml"
    p.write_text(text, encoding="utf-8")
    return p


def test_loads_valid_config(tmp_path: Path) -> None:
    config = load_asset_class_config(_write(tmp_path, _VALID_YAML))
    assert set(config.by_class) == VALID_ASSET_CLASSES
    stock = config.by_class["STOCK"]
    assert stock.anomaly_per_day == Decimal("0.05")
    assert stock.anomaly_cumulative_cap == Decimal("0.10")
    assert stock.concentration_watch == Decimal("0.10")
    assert stock.concentration_high == Decimal("0.20")
    assert config.global_concentration.top3_watch == Decimal("0.50")
    assert config.global_concentration.asset_class_bucket_high == Decimal("0.65")


def test_rejects_missing_class(tmp_path: Path) -> None:
    broken = _VALID_YAML.replace(
        "  CASH_EQUIV: {anomaly: {per_day: 0.01, cumulative_cap: 0.20}, "
        "concentration: {watch: 0.50, high: 0.70}}\n",
        "",
    )
    with pytest.raises(ValueError, match=r"missing.*CASH_EQUIV"):
        load_asset_class_config(_write(tmp_path, broken))


def test_rejects_unknown_class(tmp_path: Path) -> None:
    extra_line = (
        "  EQUITY_REGION: {anomaly: {per_day: 0.04, cumulative_cap: 0.20}, "
        "concentration: {watch: 0.15, high: 0.25}}\n"
    )
    broken = _VALID_YAML.replace("concentration_global:", extra_line + "concentration_global:")
    with pytest.raises(ValueError, match=r"unrecognized.*EQUITY_REGION"):
        load_asset_class_config(_write(tmp_path, broken))


def test_default_config_file_matches_closed_taxonomy() -> None:
    """The shipped config/asset_class_thresholds.yml must stay in sync with
    VALID_ASSET_CLASSES — this is the regression guard against drift."""
    config = load_asset_class_config()
    assert set(config.by_class) == VALID_ASSET_CLASSES
