"""Admin-configurable watch_tier target-weight mapping (#421).

Loads ``config/watch_tier_weights.yml`` — the config-driven §3
proportionality-check weight (issue #173's weight-as-explicit-parameter
interface) a watched holding is scored against instead of its real
position weight. Mirrors `asset_class_config.py` (#35): a closed taxonomy
(`VALID_WATCH_TIERS`) enforced in code, hot-reloaded from disk on every
call (no process-level caching), fails loudly on drift rather than
silently defaulting.
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

from app.core.config import get_settings
from app.schemas.holdings import VALID_WATCH_TIERS as VALID_WATCH_TIERS

logger = logging.getLogger(__name__)

# backend/ = two levels above this file (services/watch_tier_config.py → app/ → backend/)
_BACKEND_DIR = Path(__file__).resolve().parents[2]
_DEFAULT_CONFIG_FILE = _BACKEND_DIR / "config" / "watch_tier_weights.yml"

# VALID_WATCH_TIERS (re-exported above from schemas/holdings.py's `WatchTier`
# Literal — the canonical source, same pattern as PricingMode/AssetTypeValue)
# is a tuple there; this module only ever needs it as a set for diffing.
_VALID_WATCH_TIERS_SET: frozenset[str] = frozenset(VALID_WATCH_TIERS)


def _get_config_path() -> Path:
    override = get_settings().WATCH_TIER_WEIGHTS_CONFIG_PATH
    return Path(override) if override else _DEFAULT_CONFIG_FILE


def load_watch_tier_weights(path: Path | None = None) -> dict[str, float]:
    """Load + validate the watch_tier weight config.

    Raises ValueError if the file's top-level keys don't exactly match
    `VALID_WATCH_TIERS` (missing or unrecognized key).
    """
    actual = path or _get_config_path()
    with actual.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}

    found = set(data)
    missing = _VALID_WATCH_TIERS_SET - found
    unknown = found - _VALID_WATCH_TIERS_SET
    if missing or unknown:
        raise ValueError(
            f"watch_tier_weights config at {actual} does not match the closed "
            f"taxonomy — missing: {sorted(missing) or 'none'}, "
            f"unrecognized: {sorted(unknown) or 'none'}"
        )

    return {tier: float(weight) for tier, weight in data.items()}
