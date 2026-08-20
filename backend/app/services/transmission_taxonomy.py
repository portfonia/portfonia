"""Closed set of macro transmission channels (issue #128 quality gate).

A "transmission" names the mechanism through which a macro development
reaches a holding — the vocabulary in which "these names moved together"
can be stated at all. It is deliberately a CLOSED enum, code-built from
`asset_class`, for the same reason `sector_taxonomy.VALID_SECTORS` and
`asset_class_config.VALID_ASSET_CLASSES` are (Concept & Design §7.1.2's
second gate): an LLM allowed to name its own channel produces synonyms
("rate sensitivity", "discount rates", "higher-for-longer"), and every
downstream consumer that groups or intersects on that label then silently
misses. Dropping an out-of-taxonomy label is a visible, logged miss; a
synonym is an invisible one.

This lives in its own module, not in `report_assembly.py` where it was
first written, because two consumers now need it and they sit on opposite
sides of the shared/per-user line (design doc §4.8):

  * `report_assembly.py` — per-user, reads it to label this book's exposure.
  * `cross_name_intel.py` — a cross-user SHARED cache, validates the
    synthesis model's chosen mechanism against it.

A shared-cache module importing from the per-user assembly module would
invert that dependency and put every per-user helper in `report_assembly`
one import statement away from a shared writer. A3 created
`sector_taxonomy.py` for exactly this reason; this is the same move.
"""

from __future__ import annotations

VALID_TRANSMISSIONS: frozenset[str] = frozenset(
    {
        "discount_rate",
        "ai_capex_stack",
        "growth_inflation",
        "oil_freight_demand",
        "safe_haven",
        "currency_usd",
        "rates_duration",
    }
)

_CLASS_TRANSMISSION: dict[str, tuple[str, ...]] = {
    "EQUITY_US_TECH": ("discount_rate", "ai_capex_stack"),
    "EQUITY_US_BROAD": ("discount_rate", "growth_inflation"),
    "STOCK": ("discount_rate",),
    "EQUITY_CN": ("currency_usd", "growth_inflation"),
    "EQUITY_DM": ("discount_rate", "growth_inflation"),
    "EQUITY_EM": ("currency_usd", "growth_inflation"),
    "EQUITY_BROAD": ("discount_rate", "growth_inflation"),
    "PRECIOUS_METALS": ("safe_haven",),
    "ENERGY": ("oil_freight_demand",),
    "COMMODITY": ("oil_freight_demand", "growth_inflation"),
    "BOND_FUND": ("rates_duration", "discount_rate"),
    "CASH_EQUIV": ("rates_duration",),
    "REIT": ("discount_rate", "rates_duration"),
}


def transmissions_for_classes(asset_classes: list[str]) -> list[str]:
    """Closed-enum mechanisms for a set of asset classes. Unknown classes drop."""
    out: list[str] = []
    seen: set[str] = set()
    for cls in asset_classes:
        for mech in _CLASS_TRANSMISSION.get(cls, ()):
            if mech in VALID_TRANSMISSIONS and mech not in seen:
                seen.add(mech)
                out.append(mech)
    return out
