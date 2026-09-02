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
"""

from __future__ import annotations

# Declaration order is the capture/backfill walk order (US/HK/A-Share first
# so existing tests and the historical nodes keep their relative sequence).
CAPTURE_MARKET_ORDER: tuple[str, ...] = (
    "US",
    "HK",
    "A-Share",
    "UK",
    "Europe",
    "Japan",
    "Korea",
)
SUPPORTED_CAPTURE_MARKETS: frozenset[str] = frozenset(CAPTURE_MARKET_ORDER)
VALID_HOLDING_MARKETS: frozenset[str] = SUPPORTED_CAPTURE_MARKETS | {"Other"}

# Longest suffixes first so a future overlapping suffix cannot shadow.
_SUFFIX_TO_MARKET: tuple[tuple[str, str], ...] = (
    (".HK", "HK"),
    (".SS", "A-Share"),
    (".SZ", "A-Share"),
    (".AS", "Europe"),
    (".PA", "Europe"),
    (".DE", "Europe"),
    (".KS", "Korea"),
    (".KQ", "Korea"),
    (".L", "UK"),
    (".T", "Japan"),
)

_YF_BATCH_KEY: dict[str, str] = {
    "US": "us",
    "HK": "hk",
    "A-Share": "cn",
    "UK": "uk",
    "Europe": "europe",
    "Japan": "japan",
    "Korea": "korea",
}


def market_from_ticker(ticker: str | None) -> str | None:
    """Return a supported capture market inferred from `ticker`, or None.

    None means the ticker does not resolve into one of the 7 buckets — the
    caller must store `Other` + `capture_supported=False` rather than
    speculating (e.g. treating `.AX` as US). Bare tickers and one-letter
    US share-class forms (`BRK.B`) map to US. Known one-letter exchange
    suffixes (`.L`, `.T`) are matched before the share-class rule.
    """
    if not ticker:
        return None
    upper = ticker.strip().upper()
    if not upper:
        return None
    for suffix, market in _SUFFIX_TO_MARKET:
        if upper.endswith(suffix):
            return market
    if "." not in upper:
        return "US"
    last = upper.rsplit(".", 1)[-1]
    if len(last) == 1 and last.isalpha():
        return "US"
    return None


def yf_batch_key(ticker: str) -> str:
    """Lowercase grouping key for yfinance batch downloads."""
    inferred = market_from_ticker(ticker)
    if inferred is None:
        return "other"
    return _YF_BATCH_KEY[inferred]


def is_capture_supported(holding: object) -> bool:
    """Explicit flag; never infer from `market == "Other"`."""
    return bool(getattr(holding, "capture_supported", True))


def resolve_holding_market(
    *,
    ticker: str | None,
    declared_market: str | None,
    fund_code: str | None = None,
    asset_type: str | None = None,
    pricing_mode: str = "auto",
) -> tuple[str | None, bool]:
    """Two-way resolution: supported bucket + capture, or Other + not-processed.

    User-declared *supported* markets win over ticker inference (existing
    routing: a US ticker declared HK captures on the HK node). Declared
    `Other` does not win over a resolvable ticker — `Other` is the fallback,
    not a capture assignment. An unresolvable ticker always lands as
    `Other` / `capture_supported=False`, even if the user declared a
    supported market, so we never speculatively fetch it.
    """
    declared = declared_market if declared_market in VALID_HOLDING_MARKETS else None
    inferred = market_from_ticker(ticker)

    if inferred is not None:
        if declared in SUPPORTED_CAPTURE_MARKETS:
            return declared, True
        return inferred, True

    if ticker and ticker.strip():
        return "Other", False

    if fund_code:
        if declared in SUPPORTED_CAPTURE_MARKETS:
            return declared, True
        return "A-Share", True

    if asset_type in ("cash", "wmf") or pricing_mode == "manual":
        return (declared if declared is not None else "Other"), True

    if declared in SUPPORTED_CAPTURE_MARKETS:
        return declared, True
    if declared == "Other":
        return "Other", False
    return declared, True
