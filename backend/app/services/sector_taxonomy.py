"""Unified 12-class sector taxonomy (design §6.4).

Maps raw yfinance sector strings onto a stable set of classes so the
Dashboard sector distribution is consistent across markets. Ring 0 strategy:
yfinance sector + US mapping ships first; A-share / HK that yfinance returns
empty fall through to OTHER. The full 申万→unified table is a later phase.
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


def map_yf_sector(yf_sector: str | None) -> str:
    """Map a raw yfinance sector string to a unified class, OTHER on miss."""
    if not yf_sector:
        return OTHER
    return _YF_SECTOR_MAP.get(yf_sector.strip().lower(), OTHER)
