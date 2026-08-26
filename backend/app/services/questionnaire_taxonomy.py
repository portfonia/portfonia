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

# Matches the wording already used for `Holding.market` (app/schemas/holdings.py)
# so the same four labels mean the same thing everywhere in the product.
VALID_MARKETS: frozenset[str] = frozenset({"US", "HK", "A-Share", "Other"})

VALID_STYLES: frozenset[str] = frozenset({"VALUE", "GROWTH", "INDEX", "MIXED"})

VALID_HORIZONS: frozenset[str] = frozenset({"SHORT", "MEDIUM", "LONG"})

VALID_RISK_APPETITES: frozenset[str] = frozenset({"CONSERVATIVE", "BALANCED", "AGGRESSIVE"})

VALID_OBJECTIVES: frozenset[str] = frozenset({"PRESERVATION", "GROWTH", "INCOME"})

VALID_INTEL_FOCUSES: frozenset[str] = frozenset(
    {"MACRO", "FUNDAMENTALS", "GEOPOLITICS", "BALANCED"}
)

# English prose for each intel_focus value, used only when composing the
# Pass 2 INVESTOR PREFERENCES block (report_prompts.py) — never shown to the
# user (§8.4: no system-inference readback endpoint exists at all).
INTEL_FOCUS_PROMPT_TEXT: dict[str, str] = {
    "MACRO": "macro signals over stock-specific fundamentals",
    "FUNDAMENTALS": "individual-holding fundamentals over macro/geopolitical signals",
    "GEOPOLITICS": "geopolitical developments and their transmission to holdings",
    "BALANCED": "no particular tilt — weigh macro, fundamentals, and geopolitical signals evenly",
}

__all__ = [
    "INTEL_FOCUS_PROMPT_TEXT",
    "QUESTIONNAIRE_VERSION",
    "VALID_ASSET_SCALES",
    "VALID_HORIZONS",
    "VALID_INTEL_FOCUSES",
    "VALID_MARKETS",
    "VALID_OBJECTIVES",
    "VALID_RISK_APPETITES",
    "VALID_SECTORS",
    "VALID_STYLES",
]
