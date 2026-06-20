"""Admin-configurable asset_class thresholds (#35).

Loads ``config/asset_class_thresholds.yml`` — the numeric thresholds used by
window-anomaly detection (``window_data.py``) and §4.1 concentration
(``portfolio_calculator.py``) per asset_class, plus a handful of global
(non-per-class) concentration thresholds. The taxonomy itself (the set of
valid asset_class keys) stays closed in code: the loader validates the YAML
declares exactly this set, no more, no less, and fails loudly on drift rather
than silently defaulting — new categories are a Ring 1 decision, not
something a config edit should be able to introduce by accident.

Ring 1 (not built yet): per-user threshold overrides layered on top of this
system default, via a DB table rather than this file.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import yaml

from app.core.config import get_settings

logger = logging.getLogger(__name__)

# backend/ = two levels above this file (services/asset_class_config.py → app/ → backend/)
_BACKEND_DIR = Path(__file__).resolve().parents[2]
_DEFAULT_CONFIG_FILE = _BACKEND_DIR / "config" / "asset_class_thresholds.yml"

# The closed taxonomy. Every key here must appear exactly once in the YAML's
# `asset_classes` map — see the module docstring for why this is enforced.
VALID_ASSET_CLASSES: frozenset[str] = frozenset(
    {
        "STOCK",
        "EQUITY_US_BROAD",
        "EQUITY_US_TECH",
        "EQUITY_DM",
        "EQUITY_CN",
        "EQUITY_EM",
        "EQUITY_BROAD",
        "REIT",
        "COMMODITY",
        "BOND_FUND",
        "CASH_EQUIV",
    }
)


@dataclass(frozen=True)
class AssetClassThresholds:
    anomaly_per_day: Decimal
    anomaly_cumulative_cap: Decimal
    concentration_watch: Decimal
    concentration_high: Decimal


@dataclass(frozen=True)
class GlobalConcentrationThresholds:
    top3_watch: Decimal
    asset_class_bucket_watch: Decimal
    asset_class_bucket_high: Decimal


@dataclass(frozen=True)
class AssetClassConfig:
    by_class: dict[str, AssetClassThresholds]
    global_concentration: GlobalConcentrationThresholds


def _get_config_path() -> Path:
    override = get_settings().ASSET_CLASS_CONFIG_PATH
    return Path(override) if override else _DEFAULT_CONFIG_FILE


def _dec(value: object) -> Decimal:
    return Decimal(str(value))


def load_asset_class_config(path: Path | None = None) -> AssetClassConfig:
    """Load + validate the asset_class threshold config.

    Raises ValueError if the file's `asset_classes` keys don't exactly match
    `VALID_ASSET_CLASSES` (missing or unrecognized key) — a closed taxonomy
    with a loud failure mode, not a silent fallback.
    """
    actual = path or _get_config_path()
    with actual.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}

    classes_raw = data.get("asset_classes", {}) or {}
    found = set(classes_raw)
    missing = VALID_ASSET_CLASSES - found
    unknown = found - VALID_ASSET_CLASSES
    if missing or unknown:
        raise ValueError(
            f"asset_class_thresholds config at {actual} does not match the closed "
            f"taxonomy — missing: {sorted(missing) or 'none'}, "
            f"unrecognized: {sorted(unknown) or 'none'}"
        )

    by_class: dict[str, AssetClassThresholds] = {}
    for class_name, entry in classes_raw.items():
        anomaly = entry["anomaly"]
        concentration = entry["concentration"]
        by_class[class_name] = AssetClassThresholds(
            anomaly_per_day=_dec(anomaly["per_day"]),
            anomaly_cumulative_cap=_dec(anomaly["cumulative_cap"]),
            concentration_watch=_dec(concentration["watch"]),
            concentration_high=_dec(concentration["high"]),
        )

    global_raw = data.get("concentration_global", {}) or {}
    global_concentration = GlobalConcentrationThresholds(
        top3_watch=_dec(global_raw["top3_watch"]),
        asset_class_bucket_watch=_dec(global_raw["asset_class_bucket_watch"]),
        asset_class_bucket_high=_dec(global_raw["asset_class_bucket_high"]),
    )

    return AssetClassConfig(by_class=by_class, global_concentration=global_concentration)
