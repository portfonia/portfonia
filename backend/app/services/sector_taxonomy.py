"""Unified 12-class sector taxonomy (design §6.4).

Maps raw yfinance sector strings onto a stable set of classes so the
Dashboard sector distribution is consistent across markets. Ring 0 strategy:
yfinance sector + US mapping ships first; A-share / HK that yfinance returns
empty fall through to OTHER. The full Shenwan→unified table is a later phase.
"""

from __future__ import annotations

# The 12 unified classes (plus OTHER fallback).
TECHNOLOGY = "Technology"
COMMUNICATION = "Communication"
FINANCIALS = "Financials"
HEALTHCARE = "Healthcare"
CONSUMER_DISCRETIONARY = "Consumer Discretionary"
CONSUMER_STAPLES = "Consumer Staples"
ENERGY = "Energy"
MATERIALS = "Materials"
INDUSTRIALS = "Industrials"
REAL_ESTATE = "Real Estate"
UTILITIES = "Utilities"
OTHER = "Other"

# yfinance returns Yahoo's own sector labels (roughly GICS). Map lower-cased,
# stripped values so minor formatting drift does not break the lookup.
_YF_SECTOR_MAP: dict[str, str] = {
    "technology": TECHNOLOGY,
    "communication services": COMMUNICATION,
    "financial services": FINANCIALS,
    "financial": FINANCIALS,
    "healthcare": HEALTHCARE,
    "consumer cyclical": CONSUMER_DISCRETIONARY,
    "consumer discretionary": CONSUMER_DISCRETIONARY,
    "consumer defensive": CONSUMER_STAPLES,
    "consumer staples": CONSUMER_STAPLES,
    "energy": ENERGY,
    "basic materials": MATERIALS,
    "materials": MATERIALS,
    "industrials": INDUSTRIALS,
    "real estate": REAL_ESTATE,
    "utilities": UTILITIES,
}


# The closed set every `Holding.sector` value is drawn from — derived from the
# map above plus the OTHER fallback rather than hand-listed, so it cannot drift
# out of sync with `map_yf_sector`'s actual output. Used by the L2 shared
# macro-event cache (issue #128 A3) to reject a sector label the LLM invented:
# an out-of-taxonomy synonym would silently match no holding at all, turning a
# real exposure into a silent miss.
VALID_SECTORS: frozenset[str] = frozenset(_YF_SECTOR_MAP.values()) | {OTHER}


def map_yf_sector(yf_sector: str | None) -> str:
    """Map a raw yfinance sector string to a unified class, OTHER on miss."""
    if not yf_sector:
        return OTHER
    return _YF_SECTOR_MAP.get(yf_sector.strip().lower(), OTHER)
