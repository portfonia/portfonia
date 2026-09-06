"""Shared instrument-symbol normalization rules (issue #57, stage 57-1).

This module is the first of three sequential extraction stages that give
ticker/fund-code normalization one authoritative implementation instead of
duplicated copies across `app.services._yfinance` and
`app.services.holding_parser`. Stage 57-1 moves only the existing
`_yfinance._normalize_ticker` behavior here, verbatim, behind a new public
name (`normalize_legacy_ticker`) and leaves `_yfinance._normalize_ticker` as
a one-line forwarding shim so every existing caller keeps working unchanged.

Deliberately NOT in scope for this stage (frozen by the execution-freeze
correction on issue #57, comment
https://github.com/portfonia/portfonia/issues/57#issuecomment-5556040528):
a resolver, `InstrumentInput`/`SymbolResolution`/`InstrumentKey` types,
provider-symbol adapters, or migration of any consumer away from
`_yfinance._normalize_ticker`/`_TICKER_SYMBOL_OVERRIDE`. Those land in
57-2/57-3. `holding_parser.py`'s own separate `_normalize_hk_ticker`
duplicate is also untouched here — it is scheduled for removal only once
its callers are migrated (57-3), not before.
"""

from __future__ import annotations

import re

_HK_TICKER_RE = re.compile(r"^0*(\d+)\.HK$", re.IGNORECASE)


def _normalize_hk_ticker(ticker: str) -> str:
    """Canonicalize an HK ticker to yfinance's 4-digit form (issue #64).

    Strips leading zeros then left-pads codes below 10000 back to 4 digits.
    Genuine 5-digit codes (>=10000) are left as-is. Non-HK tickers pass through.
    """
    m = _HK_TICKER_RE.match(ticker)
    if not m:
        return ticker
    num = int(m.group(1))
    digits = f"{num:04d}" if num < 10000 else str(num)
    return f"{digits}.HK"


# Bare tickers that silently collide with an unrelated US-listed security on
# yfinance and need an explicit exchange suffix to resolve to the intended
# instrument (issue #204: bare "PSH" resolved to an unrelated US ETF instead
# of Pershing Square Holdings, which trades on the LSE as PSH.L).
#
# General bare-ticker suffix-forcing (any bare ticker + a known/confirmed
# market -> the right exchange suffix, not just this one hardcoded entry) is
# out of scope here — PR #310 (issue #92, merged) added
# `holding_parser.apply_confirmed_exchange_suffix`, which forces the suffix
# at parse/confirm time once a market is user-declared or confidently
# derived (e.g. currency == "GBP" -> UK), closing issue #313 item 5's "VOD"
# case for any holding with a declared market or currency hint. A bare
# ticker with NEITHER (no declared market, no currency hint) is still left
# unresolved by design ("do not guess a suffix") — Ring-1-C / issue #204
# territory, not handled at this yfinance-fetch layer either way.
_TICKER_SYMBOL_OVERRIDE: dict[str, str] = {
    "PSH": "PSH.L",
}


def normalize_legacy_ticker(ticker: str) -> str:
    """Canonicalize a ticker to its yfinance-resolvable form.

    Composes the known-collision override table above with HK suffix
    normalization (issue #64) — the two sets are disjoint today, so order
    between them doesn't matter.

    This is a byte-for-byte extraction of the pre-#57 `_yfinance.
    _normalize_ticker` body (golden-fixture-verified in
    `app/tests/fixtures/legacy_ticker_normalization_golden.json`); do not
    change its behavior in this stage.
    """
    overridden = _TICKER_SYMBOL_OVERRIDE.get(ticker.upper(), ticker)
    return _normalize_hk_ticker(overridden)
