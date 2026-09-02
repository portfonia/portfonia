"""Closed enums for the B6 investment-style questionnaire (Ring 1-B design.md
§8.3 — issue #129 checkpoint B6).

Same three-layer pattern as `asset_class_config.VALID_ASSET_CLASSES` /
`app/schemas/holdings.py`'s `VALID_CURRENCIES`: constants defined here (the
single source of truth), mirrored as a frozen-snapshot DB CHECK constraint in
the migration that creates `user_investment_context`, and validated again at
the Pydantic boundary (`app/schemas/questionnaire.py`) so a bad value 422s
instead of surfacing as a raw `IntegrityError` / 500.

`sectors_of_interest` deliberately has NO constant of its own here — it reads
`sector_taxonomy.VALID_SECTORS` directly, per §8.3's explicit instruction not
to start a second sector vocabulary.

`asset_scale` buckets are USD-equivalent, currency-agnostic ranges (the
questionnaire has no dedicated currency field of its own) — chosen because
§8.3 also flags this dimension as the future Ring 2 free-tier threshold
input, so the bucket boundaries need to stay meaningful independent of which
base_currency a given user reports in.
"""

from __future__ import annotations

from app.services.sector_taxonomy import VALID_SECTORS

QUESTIONNAIRE_VERSION = "v1"

VALID_ASSET_SCALES: frozenset[str] = frozenset({"UNDER_100K", "100K_500K", "500K_2M", "OVER_2M"})

# Matches the four ORIGINAL `Holding.market` labels (app/schemas/holdings.py)
# for wording consistency where they overlap. Holdings capture itself has
# since widened to 7 buckets + Other (issue #311); this stays the coarser,
# original 4-value set by design — a questionnaire market *preference* is a
# different, deliberately coarser taxonomy than a capture bucket, not the
# same vocabulary re-used (issue #313 item 6).
VALID_MARKETS: frozenset[str] = frozenset({"US", "HK", "A-Share", "Other"})

VALID_STYLES: frozenset[str] = frozenset({"VALUE", "GROWTH", "INDEX", "MIXED"})

VALID_HORIZONS: frozenset[str] = frozenset({"SHORT", "MEDIUM", "LONG"})

VALID_RISK_APPETITES: frozenset[str] = frozenset({"CONSERVATIVE", "BALANCED", "AGGRESSIVE"})

VALID_OBJECTIVES: frozenset[str] = frozenset({"PRESERVATION", "GROWTH", "INCOME"})

VALID_INTEL_FOCUSES: frozenset[str] = frozenset(
    {"MACRO", "FUNDAMENTALS", "GEOPOLITICS", "BALANCED"}
)

# English prose for each questionnaire value, used only when composing the
# INVESTOR PREFERENCES block (report_prompts.py) for Pass 2 AND assembly —
# never shown to the user (§8.4: no system-inference readback endpoint
# exists at all). All 8 dimensions are injected as of the 2026-08-25
# correction to decision point 6 (Ring 1-B design.md §8.5): the original
# 2026-08-21 decision to exclude risk_appetite/objective entirely was a
# misreading of the product owner's actual intent — every stated preference
# matters and should be used, with the Layer-3/4 boundary held by explicit
# prompt scoping (below) and the output-side `_scan_forbidden_output`
# backstop, not by discarding user input.
ASSET_SCALE_PROMPT_TEXT: dict[str, str] = {
    "UNDER_100K": "under $100K investable assets",
    "100K_500K": "$100K-$500K investable assets",
    "500K_2M": "$500K-$2M investable assets",
    "OVER_2M": "over $2M investable assets",
}

MARKET_PROMPT_TEXT: dict[str, str] = {
    "US": "US",
    "HK": "Hong Kong",
    "A-Share": "mainland China A-share",
    "Other": "other markets",
}

STYLE_PROMPT_TEXT: dict[str, str] = {
    "VALUE": "value investing",
    "GROWTH": "growth investing",
    "INDEX": "index investing",
    "MIXED": "a mixed value/growth/index approach",
}

HORIZON_PROMPT_TEXT: dict[str, str] = {
    "SHORT": "short-term (under 1 year)",
    "MEDIUM": "medium-term (1-3 years)",
    "LONG": "long-term (3+ years)",
}

# Deliberately worded as "how the investor already frames risk", not as a
# label the model could restate as license for a sizing suggestion — see
# the SCOPE guardrail in report_prompts.py's
# _build_investor_preferences_block, which this text feeds into.
RISK_APPETITE_PROMPT_TEXT: dict[str, str] = {
    "CONSERVATIVE": "conservative — prioritizes capital stability over upside",
    "BALANCED": "balanced — accepts moderate volatility for moderate growth",
    "AGGRESSIVE": "aggressive — accepts high volatility for higher potential growth",
}

OBJECTIVE_PROMPT_TEXT: dict[str, str] = {
    "PRESERVATION": "capital preservation",
    "GROWTH": "capital growth",
    "INCOME": "income (dividends/yield)",
}

INTEL_FOCUS_PROMPT_TEXT: dict[str, str] = {
    "MACRO": "macro signals over stock-specific fundamentals",
    "FUNDAMENTALS": "individual-holding fundamentals over macro/geopolitical signals",
    "GEOPOLITICS": "geopolitical developments and their transmission to holdings",
    "BALANCED": "no particular tilt — weigh macro, fundamentals, and geopolitical signals evenly",
}

__all__ = [
    "ASSET_SCALE_PROMPT_TEXT",
    "HORIZON_PROMPT_TEXT",
    "INTEL_FOCUS_PROMPT_TEXT",
    "MARKET_PROMPT_TEXT",
    "OBJECTIVE_PROMPT_TEXT",
    "QUESTIONNAIRE_VERSION",
    "RISK_APPETITE_PROMPT_TEXT",
    "STYLE_PROMPT_TEXT",
    "VALID_ASSET_SCALES",
    "VALID_HORIZONS",
    "VALID_INTEL_FOCUSES",
    "VALID_MARKETS",
    "VALID_OBJECTIVES",
    "VALID_RISK_APPETITES",
    "VALID_SECTORS",
    "VALID_STYLES",
]
