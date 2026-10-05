"""Large-holding selection by portfolio weight.

Moved out of the removed `ticker_intel` module (issue #640): the normal report
path still uses it to pick large holdings for window prices and headline
recall, independent of any anomaly threshold.
"""

from __future__ import annotations

from typing import Any

from app.services.instrument_symbols import InstrumentKey, intelligence_identifier

_TOP_K_BY_WEIGHT = 5
_MIN_WEIGHT = 0.05


def _weight(holding: dict[str, Any], total: float) -> float:
    if total <= 0:
        return 0.0
    value = holding.get("market_value_base") or 0.0
    return float(value) / total


def _holding_identifier(holding: dict[str, Any]) -> str:
    """The key a holding is looked up under elsewhere in the report pipeline:
    the `intelligence_identifier(...)`'d ticker, or fund_code for a fund-only
    row (`intelligence_identifier` passes an unrecognized string, including a
    plain numeric fund code, through unchanged). Matches
    `select_user_anomalies`/`compute_global_moves`'s key convention."""
    raw = holding.get("ticker") or holding.get("fund_code") or ""
    if not raw:
        return ""
    return intelligence_identifier(InstrumentKey("ticker", str(raw)))


def _weighted_identifiers(
    holdings: list[dict[str, Any]], portfolio_total: float | None
) -> list[tuple[float, str]]:
    """(weight, identifier) pairs, ONE per distinct identifier, weight
    descending.

    Aggregates `market_value_base` by identifier BEFORE ranking (PR #168
    review round 1 suggestion) — this product preserves upload order, so the
    same ticker legitimately appears as more than one `Holding` row (a
    position split across two lots). Ranking un-aggregated rows let a single
    identifier occupy more than one `top_k` slot (a literal duplicate in the
    output) and let its two half-sized rows evict a genuinely distinct
    holding that would have made the cut under the identifier's true
    combined weight.
    """
    rows = list(holdings or [])
    total = float(portfolio_total or 0.0)
    if total <= 0:
        total = sum(float(h.get("market_value_base") or 0.0) for h in rows)
    if total <= 0:
        return []
    combined: dict[str, float] = {}
    for holding in rows:
        ident = _holding_identifier(holding)
        if not ident:
            continue
        combined[ident] = combined.get(ident, 0.0) + float(holding.get("market_value_base") or 0.0)
    weighted = [(value / total, ident) for ident, value in combined.items()]
    weighted.sort(key=lambda item: item[0], reverse=True)
    return weighted


def large_weight_identifiers(
    holdings: list[dict[str, Any]],
    portfolio_total: float | None = None,
    top_k: int = _TOP_K_BY_WEIGHT,
    min_weight: float = _MIN_WEIGHT,
) -> list[str]:
    """Identifiers among `holdings` at or above `min_weight`, capped to the
    top `top_k` by weight — a "big holding, no anomaly required" selection for
    Pass 2's own material-gathering (issue #128 narrative-layer redesign,
    2026-08-20).

    Root cause this closes: on the 2026-08-17 anchor report, TSM (22.5% of
    the portfolio, +1.22% on the day — below its own asset-class anomaly
    threshold) got ZERO recalled news in Pass 2's prompt, because Pass 2's
    material-gathering only ever looked at `ctx.price_anomalies`. A holding
    large enough to matter should not need to cross an anomaly threshold to
    get material at all.
    """
    weighted = _weighted_identifiers(holdings, portfolio_total)
    return [ident for weight, ident in weighted if weight >= min_weight][:top_k]
