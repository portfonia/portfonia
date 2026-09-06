"""Closed holding-market set and capture-support resolution (issue #311).

`Holding.market` is a closed set: the 7 independently-scheduled capture
buckets plus `Other` as an explicit fallback. `Other` is a legitimate stored
value — it is NOT a rejection flag. Capture / §1 / Pass 2 key off the
separate `capture_supported` column (see `is_capture_supported`), never off
`market == "Other"`.

Ticker-suffix recognition is the only way a holding *resolves* into a
bucket. An unknown exchange suffix is unresolvable: persist `market="Other"`
and `capture_supported=False`, and never attempt a speculative yfinance
lookup. Bare tickers and US share-class forms (`BRK.B`) resolve to US.

Issue #57 stage 57-2: the actual suffix tables and resolution logic moved to
`app.services.instrument_symbols` (the single shared-rule owner — see that
module's docstring). This module is now a compatibility facade: every name
below is re-exported unchanged so existing `from app.services.markets
import ...` call sites keep working with identical behavior.
`is_capture_supported` stays defined here because it reads an ORM attribute,
which `instrument_symbols` must not do.
"""

from __future__ import annotations

from app.services.instrument_symbols import (
    CAPTURE_MARKET_ORDER as CAPTURE_MARKET_ORDER,
)
from app.services.instrument_symbols import (
    SUPPORTED_CAPTURE_MARKETS as SUPPORTED_CAPTURE_MARKETS,
)
from app.services.instrument_symbols import (
    VALID_HOLDING_MARKETS as VALID_HOLDING_MARKETS,
)
from app.services.instrument_symbols import (
    market_from_ticker as market_from_ticker,
)
from app.services.instrument_symbols import (
    resolve_holding_market as resolve_holding_market,
)
from app.services.instrument_symbols import (
    yf_batch_key as yf_batch_key,
)


def is_capture_supported(holding: object) -> bool:
    """Explicit flag; never infer from `market == "Other"`."""
    return bool(getattr(holding, "capture_supported", True))
