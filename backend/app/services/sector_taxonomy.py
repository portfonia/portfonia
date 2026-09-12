"""Closed sector label set for the `sectors_of_interest` questionnaire field.

This module previously also mapped raw yfinance sector strings onto
`Holding.sector` for GICS-style holdings classification; that consumer
(`price_fetcher.backfill_sectors` and everything downstream of it) was
removed in issue #435. The twelve labels below are kept ONLY because
`questionnaire_taxonomy.py` / `schemas/questionnaire.py` validate the user
investment-questionnaire's `sectors_of_interest` field against them
(§8.3's explicit instruction not to start a second sector vocabulary for
that field) — this is a distinct, user-facing preference, not an
auto-derived classification, and is out of scope for #435.
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

VALID_SECTORS: frozenset[str] = frozenset(
    {
        TECHNOLOGY,
        COMMUNICATION,
        FINANCIALS,
        HEALTHCARE,
        CONSUMER_DISCRETIONARY,
        CONSUMER_STAPLES,
        ENERGY,
        MATERIALS,
        INDUSTRIALS,
        REAL_ESTATE,
        UTILITIES,
        OTHER,
    }
)
