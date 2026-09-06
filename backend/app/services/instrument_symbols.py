"""Shared instrument-symbol normalization rules (issue #57).

Stage 57-1 moved the existing `_yfinance._normalize_ticker` behavior here
verbatim (`normalize_legacy_ticker`) and left `_yfinance._normalize_ticker`
as a one-line forwarding shim. Stage 57-2 (this stage) adds the typed
resolver/key/provider-adapter contracts frozen on issue #57
(https://github.com/portfonia/portfonia/issues/57#issuecomment-5551712656)
and routes the holdings write path and the price-consumer set (
`price_capture`, `price_fetcher`, `portfolio_calculator`, `technical_
position`, `routers.holdings` sparse-history lookup, and the yfinance/
Finnhub/Massive provider boundaries) through it — see the module-level
`resolve_instrument`/`to_provider_symbol`/`build_provider_request_plan`
docstrings for what each owns.

Also moved here in this stage (single-suffix-definition requirement, issue
#57 frozen design section 2 "Ownership matrix"): the market/suffix
classification tables that used to live only in `app.services.markets`
(`CAPTURE_MARKET_ORDER`, `market_from_ticker`, `resolve_holding_market`,
`yf_batch_key`) and the exchange-suffix-forcing tables that used to live
only in `app.services.holding_parser` (`_confirmed_market`,
`_exchange_suffix_to_apply`, the A-share/ambiguous-suffix tables).
`app.services.markets` now re-exports the moved names unchanged — every
existing `from app.services.markets import ...` call site keeps working
with identical behavior — and `app.services.holding_parser`'s two
compatibility wrappers (`normalize_ticker_and_currency`,
`apply_confirmed_exchange_suffix`) now delegate to the private helpers
below instead of keeping their own copies of these tables. This module
must not import `app.services.markets`, `app.services.holding_parser`, any
provider module, `app.core.config`, or ORM models — see the ownership
matrix's "must not own" column and the ADR-002/#129-style layering this
mirrors.

Deliberately NOT in scope for #57 at all (frozen design section 1/8): a
security directory, existence/validation checks, a new confirmation flow,
or new Holding/API fields. `status`/`capture_supported` describe format
resolvability, never security existence — that is issue #58.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

logger = logging.getLogger(__name__)

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
# `holding_parser.apply_confirmed_exchange_suffix` (now a compatibility
# wrapper over `_decide_exchange_suffix` below), which forces the suffix at
# parse/confirm time once a market is user-declared or confidently derived
# (e.g. currency == "GBP" -> UK), closing issue #313 item 5's "VOD" case for
# any holding with a declared market or currency hint. A bare ticker with
# NEITHER (no declared market, no currency hint) is still left unresolved by
# design ("do not guess a suffix") — Ring-1-C / issue #204 territory, not
# handled at this yfinance-fetch layer either way.
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
    `app/tests/fixtures/legacy_ticker_normalization_golden.json`); its
    behavior is frozen — this is the LEGACY PRICE LOOKUP identity, used
    verbatim by `intelligence_identifier` and by `resolve_instrument`'s
    `key` field, and is intentionally independent of `market`/declared-
    market precedence (see the historical-PSH row in the ambiguity matrix
    in the module docstring above and in the frozen design comment).
    """
    overridden = _TICKER_SYMBOL_OVERRIDE.get(ticker.upper(), ticker)
    return _normalize_hk_ticker(overridden)


# ---------------------------------------------------------------------------
# Market/suffix classification (moved from app.services.markets, issue #57
# stage 57-2 — see the module docstring's "single suffix definition"
# requirement). `app.services.markets` re-exports every name below
# unchanged; `is_capture_supported` stays there (it reads an ORM attribute,
# which this module must not touch).
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Exchange-suffix forcing (moved from app.services.holding_parser, issue #57
# stage 57-2). `holding_parser.apply_confirmed_exchange_suffix` is now a thin
# compatibility wrapper over `_decide_exchange_suffix` below.
# ---------------------------------------------------------------------------

# Force-applied once market is confirmed (issue #92 / PR #310). Do not guess
# a suffix when market cannot be determined. .AX/.TO stay out of this class.
# Longest suffixes first so .KS is not shadowed by a future overlap.
_FORCE_EXCHANGE_SUFFIXES: tuple[str, ...] = (
    ".HK",
    ".SS",
    ".SZ",
    ".AS",
    ".PA",
    ".DE",
    ".KS",
    ".KQ",
    ".L",
    ".T",
)

# Currencies worth a ticker_no_suffix hint when a suffix cannot be applied
# (market undetermined, or determined but ambiguous — Europe/Korea each
# have multiple listing suffixes). Every currency this module resolves to a
# live capture market via `_confirmed_market`, so the set must track that
# resolution (PR #310 round 5 review — EUR and KRW silently got no hint
# after #312 widened capture to UK/Europe/Japan/Korea).
_SUFFIX_HINT_CURRENCIES = frozenset({"GBP", "HKD", "CNY", "EUR", "JPY", "KRW"})

# Legal suffixes for a market whose exchange cannot be guessed from the
# ticker alone (Europe/Korea have several venues; an A-share code outside
# the recognized digit ranges can't be placed on Shanghai vs Shenzhen).
# Surfaced in the ticker_suffix_ambiguous note so the user knows what to
# type, not just that something was skipped (PR #310 round 6 review).
_AMBIGUOUS_SUFFIX_OPTIONS: dict[str, str] = {
    "Europe": ".AS / .PA / .DE",
    "Korea": ".KS / .KQ",
    "A-Share": ".SS / .SZ",
}


def _known_exchange_suffix(ticker: str) -> str | None:
    upper = ticker.upper()
    for suf in _FORCE_EXCHANGE_SUFFIXES:
        if upper.endswith(suf):
            return suf
    return None


def _ticker_base(ticker: str) -> str:
    suf = _known_exchange_suffix(ticker)
    if suf is None:
        return ticker
    return ticker[: -len(suf)]


def _a_share_suffix(code: str) -> str | None:
    """Shanghai vs Shenzhen from a 6-digit listed code. None if we cannot tell."""
    digits = code.split(".")[0]
    if not (digits.isdigit() and len(digits) == 6):
        return None
    if digits.startswith(("5", "6", "9")):
        return ".SS"
    if digits.startswith(("0", "1", "2", "3")):
        return ".SZ"
    return None


def _confirmed_market(
    *, market: str | None, ticker: str, fund_code: str | None, currency: str | None
) -> str | None:
    """User-set market, else a confident derivation. None = do not guess a suffix."""
    if market in VALID_HOLDING_MARKETS:
        return str(market)
    upper = ticker.upper()
    for suf in _FORCE_EXCHANGE_SUFFIXES:
        if upper.endswith(suf):
            inferred = market_from_ticker(ticker)
            if inferred is not None:
                return inferred
            break
    if fund_code:
        return "A-Share"
    base = _ticker_base(ticker).upper()
    if base in _TICKER_SYMBOL_OVERRIDE:
        # Override target is .L (PSH -> PSH.L); UK is a real capture market.
        return "UK"
    if currency == "HKD":
        return "HK"
    if currency == "CNY":
        ident = ticker.split(".")[0] if ticker else ""
        if ident.isdigit() and len(ident) == 6:
            return "A-Share"
        return None
    if currency == "USD":
        return "US"
    if currency == "GBP":
        return "UK"
    if currency == "EUR":
        return "Europe"
    if currency == "JPY":
        return "Japan"
    if currency == "KRW":
        return "Korea"
    return None


def _exchange_suffix_to_apply(market: str, ticker: str, currency: str | None) -> str | None:
    if market == "HK":
        return ".HK"
    if market == "A-Share":
        return _a_share_suffix(ticker)
    if market == "UK":
        return ".L"
    if market == "Japan":
        return ".T"
    # Europe (.AS/.PA/.DE) and Korea (.KS/.KQ) have multiple suffixes — do not guess.
    return None


@dataclass(frozen=True)
class SymbolNote:
    """One deterministic, machine-checkable symbol-resolution note.

    Mirrors the existing `holding_parser` issue-note shape (`code`/`params`/
    `severity`) so the compatibility wrappers can translate these 1:1 into
    `ParsedRow`/`_apply_write_defaults` issues without inventing a new user-
    facing flow (#57 is not allowed new note codes/localized flows).
    """

    code: str
    params: Mapping[str, str]
    severity: Literal["info", "warning"] = "info"


@dataclass(frozen=True)
class _SuffixDecision:
    ticker: str
    market: str | None  # None means "caller must not write row['market']"
    suffix_ambiguous: bool
    notes: tuple[SymbolNote, ...]


def _decide_exchange_suffix(
    *, market: str | None, ticker: str, fund_code: str | None, currency: str | None
) -> _SuffixDecision:
    """Pure decision port of `apply_confirmed_exchange_suffix`'s suffix logic.

    Gate: user-set or confidently derived market in a live capture slice
    (the 7 scheduled buckets). When market cannot be determined at all, do
    not guess (`ticker_no_suffix` on preview). When market IS determined but
    the specific suffix is ambiguous (Europe/Korea have several venues) or
    unplaceable (an A-share code outside the recognized digit ranges),
    `suffix_ambiguous=True` tells the caller to force `capture_supported=
    False` after `resolve_holding_market` runs — a still-bare ticker would
    otherwise fall through `market_from_ticker`'s "no suffix = US" default
    and get speculatively fetched as an unrelated US security under the
    real holding's identity (PR #310 round 6 review). Idempotent if a
    suffix is already present. Callers gate manual/cash rows themselves
    before calling this (a manual "ticker" is a free-text label, not a
    market symbol).
    """
    notes: list[SymbolNote] = []
    confirmed = _confirmed_market(
        market=market, ticker=ticker, fund_code=fund_code, currency=currency
    )
    already = _known_exchange_suffix(ticker) is not None or "." in ticker
    currency_s = currency or ""

    if confirmed not in SUPPORTED_CAPTURE_MARKETS:
        if not already and confirmed is None and currency_s in _SUFFIX_HINT_CURRENCIES:
            notes.append(
                SymbolNote(
                    "ticker_no_suffix", {"ticker": ticker, "currency": currency_s}, "warning"
                )
            )
        return _SuffixDecision(
            ticker=ticker, market=None, suffix_ambiguous=False, notes=tuple(notes)
        )

    if already:
        return _SuffixDecision(ticker=ticker, market=confirmed, suffix_ambiguous=False, notes=())

    suffix = _exchange_suffix_to_apply(confirmed, ticker, currency_s or None)
    if suffix is None:
        if confirmed not in _AMBIGUOUS_SUFFIX_OPTIONS:
            # US (the only capture market with no suffix at all) legitimately
            # falls through here on every plain US ticker.
            return _SuffixDecision(
                ticker=ticker, market=confirmed, suffix_ambiguous=False, notes=()
            )
        if currency_s in _SUFFIX_HINT_CURRENCIES:
            notes.append(
                SymbolNote(
                    "ticker_suffix_ambiguous",
                    {
                        "ticker": ticker,
                        "currency": currency_s,
                        "market": confirmed,
                        "suffixes": _AMBIGUOUS_SUFFIX_OPTIONS.get(confirmed, ""),
                    },
                    "warning",
                )
            )
        return _SuffixDecision(
            ticker=ticker, market=confirmed, suffix_ambiguous=True, notes=tuple(notes)
        )

    return _SuffixDecision(
        ticker=f"{ticker}{suffix}", market=confirmed, suffix_ambiguous=False, notes=()
    )


# Currency implied by a recognized ticker suffix (issue #57 stage 57-2: moved
# from holding_parser's `_TICKER_CURRENCY_MAP` so `resolve_instrument` and the
# `normalize_ticker_and_currency` wrapper share one table). Distinct from the
# 7-market `_SUFFIX_TO_MARKET` set above: this table also covers non-capture
# suffixes (.ax/.to) purely for currency correction, which is a different
# concern than market bucket classification.
_TICKER_CURRENCY_MAP: dict[str, str] = {
    ".hk": "HKD",
    ".ss": "CNY",
    ".sz": "CNY",
    ".l": "GBP",
    ".as": "EUR",
    ".pa": "EUR",
    ".de": "EUR",
    ".t": "JPY",
    ".ks": "KRW",
    ".kq": "KRW",
    ".ax": "AUD",
    ".to": "CAD",
}


def _normalize_ticker_and_correct_currency(
    ticker: str, currency: str | None
) -> tuple[str, str | None, tuple[SymbolNote, ...]]:
    """Pure decision port of `normalize_ticker_and_currency`'s body.

    HK 4-digit canonicalization, then currency correction from a recognized
    ticker suffix. Applies unconditionally (including to a manual-priced
    label that happens to look HK-shaped or carry a recognized suffix) —
    matches the existing wrapper's behavior, which never checks
    `pricing_mode`/`asset_type`.
    """
    notes: list[SymbolNote] = []
    normalized = _normalize_hk_ticker(ticker)
    if normalized != ticker:
        notes.append(SymbolNote("ticker_normalized_hk", {"ticker": normalized}))
        ticker = normalized
    for suffix, mapped_currency in _TICKER_CURRENCY_MAP.items():
        if ticker.lower().endswith(suffix):
            if currency != mapped_currency:
                notes.append(
                    SymbolNote(
                        "currency_corrected",
                        {"currency": mapped_currency, "suffix": suffix.upper()},
                    )
                )
                currency = mapped_currency
            break
    return ticker, currency, tuple(notes)


# ---------------------------------------------------------------------------
# Frozen public types (issue #57 frozen design section 3).
# ---------------------------------------------------------------------------

Market = Literal["US", "HK", "A-Share", "UK", "Europe", "Japan", "Korea", "Other"]
AssetType = Literal["stock", "etf", "fund", "cash", "wmf", "other"]
PricingMode = Literal["auto", "manual"]
IdentifierKind = Literal["ticker", "fund_code"]
Provider = Literal["yfinance", "finnhub", "massive"]
ResolutionStatus = Literal["resolved", "ambiguous", "not_applicable", "unsupported"]
Exchange = Literal[
    "HKEX",
    "SSE",
    "SZSE",
    "LSE",
    "EURONEXT_AMSTERDAM",
    "EURONEXT_PARIS",
    "XETRA",
    "TSE",
    "KRX_KOSPI",
    "KRX_KOSDAQ",
]
SymbolNoteCode = Literal[
    "ticker_normalized_hk",
    "currency_corrected",
    "ticker_no_suffix",
    "ticker_suffix_ambiguous",
]


@dataclass(frozen=True)
class InstrumentInput:
    """Holdings-boundary snapshot fed to `resolve_instrument`.

    The caller performs existing asset-type coercion, cash/wmf identifier
    stripping, and currency-alias normalization BEFORE constructing this —
    `resolve_instrument` is pure symbol resolution, not general row cleanup.
    """

    ticker: str | None
    fund_code: str | None
    market: Market | None
    currency: str | None
    asset_type: AssetType | None
    pricing_mode: PricingMode = "auto"


@dataclass(frozen=True)
class InstrumentKey:
    """Legacy-compatible lookup identity — the price/cache string, never a
    prefixed `ticker:`/`fund_code:` form. `kind` exists to distinguish
    identities inside shared resolution and the future #58 catalog only."""

    kind: IdentifierKind
    code: str


@dataclass(frozen=True)
class SymbolResolution:
    """Result of `resolve_instrument`. `ticker`/`fund_code`/`currency` are
    the PROPOSED WRITE fields (what a caller should persist onto the row);
    `key` is the separate, possibly-diverging LEGACY LOOKUP identity (see
    the historical-PSH-declared-US case in the module docstring)."""

    ticker: str | None
    fund_code: str | None
    currency: str | None
    key: InstrumentKey | None
    market: Market | None
    exchange: Exchange | None
    status: ResolutionStatus
    capture_supported: bool
    notes: tuple[SymbolNote, ...]


# Suffix on the FINAL lookup key's own code, independent of `market` — a
# historical mismatch row (declared market disagrees with the ticker's
# actual listing suffix) reports the code's real venue here without
# "repairing" the persisted market bucket (frozen design section 5, the
# PSH/declared-US row).
_SUFFIX_TO_EXCHANGE: dict[str, Exchange] = {
    ".HK": "HKEX",
    ".SS": "SSE",
    ".SZ": "SZSE",
    ".AS": "EURONEXT_AMSTERDAM",
    ".PA": "EURONEXT_PARIS",
    ".DE": "XETRA",
    ".KS": "KRX_KOSPI",
    ".KQ": "KRX_KOSDAQ",
    ".L": "LSE",
    ".T": "TSE",
}


def _exchange_for_key_code(code: str) -> Exchange | None:
    upper = code.upper()
    for suffix, exchange in _SUFFIX_TO_EXCHANGE.items():
        if upper.endswith(suffix):
            return exchange
    return None


def intelligence_identifier(key: InstrumentKey) -> str:
    """Existing intelligence-cache convention: legacy-normalize then upper().

    Bare string for either kind — no new kind-prefix casing policy. Owned
    here (not by `resolve_instrument`) because report/cache consumers
    (57-3) apply it to keys built long after resolution, including
    historical rows this refactor does not touch.
    """
    return normalize_legacy_ticker(key.code).upper()


def resolve_instrument(value: InstrumentInput) -> SymbolResolution:
    """Pure composition of the existing symbol write rules (frozen design
    section 2): HK canonicalization, confirmed suffix decision, market/
    support resolution, and the suffix-ambiguity override — in the same
    order `holding_parser._postprocess` and `routers.holdings.
    _apply_write_defaults` already run them. No ORM/network/database
    access; never mutates its input. `resolved` means the format/market is
    resolvable, not that the security exists (that check is issue #58).
    """
    ticker = value.ticker
    fund_code = value.fund_code
    currency = value.currency
    market: str | None = value.market
    manual = value.pricing_mode == "manual"
    cash_like = value.asset_type in ("cash", "wmf")
    notes: list[SymbolNote] = []
    suffix_ambiguous = False

    if ticker:
        ticker, currency, first_pass_notes = _normalize_ticker_and_correct_currency(
            ticker, currency
        )
        notes.extend(first_pass_notes)

        if not manual and not cash_like:
            decision = _decide_exchange_suffix(
                market=market, ticker=ticker, fund_code=fund_code, currency=currency
            )
            if decision.market is not None and market is None:
                market = decision.market
            ticker = decision.ticker
            suffix_ambiguous = decision.suffix_ambiguous
            notes.extend(decision.notes)

            ticker, currency, second_pass_notes = _normalize_ticker_and_correct_currency(
                ticker, currency
            )
            notes.extend(second_pass_notes)

    resolved_market, capture_ok = resolve_holding_market(
        ticker=ticker,
        declared_market=market,
        fund_code=fund_code,
        asset_type=value.asset_type,
        pricing_mode=value.pricing_mode,
    )
    if suffix_ambiguous:
        capture_ok = False

    key: InstrumentKey | None
    status: ResolutionStatus
    if manual or cash_like:
        key = None
        status = "not_applicable"
    elif ticker:
        key = InstrumentKey("ticker", normalize_legacy_ticker(ticker))
        if suffix_ambiguous:
            status = "ambiguous"
        elif resolved_market == "Other" and not capture_ok:
            status = "unsupported"
        else:
            status = "resolved"
    elif fund_code:
        key = InstrumentKey("fund_code", fund_code)
        status = "resolved"
    else:
        key = None
        status = "not_applicable"

    exchange = (
        _exchange_for_key_code(key.code) if key is not None and key.kind == "ticker" else None
    )

    return SymbolResolution(
        ticker=ticker,
        fund_code=fund_code,
        currency=currency,
        key=key,
        market=resolved_market,  # type: ignore[arg-type]
        exchange=exchange,
        status=status,
        capture_supported=capture_ok,
        notes=tuple(notes),
    )


# ---------------------------------------------------------------------------
# Provider boundary contract (frozen design section 4).
# ---------------------------------------------------------------------------


def to_provider_symbol(provider: Provider, key: InstrumentKey) -> str | None:
    """Pure code adaptation only — no row/node/credential gate here (the
    signature deliberately carries neither). The orchestration caller must
    already have enforced auto pricing, `capture_supported`, and its
    existing market/node/credential gates before calling this; never
    reconstruct an ambiguous holding as a bare US key to bypass those gates.

    Freezes current wire behavior (frozen design section 4): yfinance
    accepts any legacy-normalized ticker unchanged; Finnhub/Massive accept
    only a ticker that resolves to US syntax, preserved unchanged as the
    wire symbol. `fund_code` keys and unsupported syntax return None (zero
    network calls) for every provider — fund NAV requests stay on the
    existing NAV path, outside these three adapters.
    """
    if key.kind != "ticker":
        return None
    code = normalize_legacy_ticker(key.code)
    if provider == "yfinance":
        return code
    if provider in ("finnhub", "massive"):
        return code if market_from_ticker(code) == "US" else None
    return None


@dataclass(frozen=True)
class ProviderRequestPlan:
    """A forward-built request plan for one provider call.

    `wire_symbols` is what to actually send (unique, collision-free, in
    input order). `to_internal` maps each wire symbol back to the internal
    code that produced it — the caller must use this for response
    conversion instead of guessing a reverse transformation. `unsupported`
    lists internal codes that produced zero wire symbols (fund_code keys,
    or a ticker `to_provider_symbol` rejected) — these get zero requests.
    `collisions` lists wire symbols excluded because two or more DISTINCT
    internal codes mapped to them; such codes are excluded from both
    `wire_symbols` and `to_internal` rather than silently overwriting one
    another (frozen design section 4). A duplicate `InstrumentKey.code`
    within `keys` is treated as the same alias, not a collision, and is
    deduplicated to a single request.
    """

    wire_symbols: tuple[str, ...]
    to_internal: Mapping[str, str]
    unsupported: tuple[str, ...]
    collisions: Mapping[str, tuple[str, ...]]


def build_provider_request_plan(
    provider: Provider, keys: Sequence[InstrumentKey]
) -> ProviderRequestPlan:
    """Build a collision-safe, alias-deduplicated request plan for `provider`.

    Every `PriceSnapshot` write, fallback-missing comparison, and result
    merge downstream must key off `to_internal`'s values (the internal
    code), never off a guessed reverse-normalization of the provider's
    response key.
    """
    wire_to_codes: dict[str, list[str]] = {}
    unsupported: list[str] = []
    seen_codes: set[str] = set()

    for key in keys:
        if key.code in seen_codes:
            continue
        seen_codes.add(key.code)
        wire = to_provider_symbol(provider, key)
        if wire is None:
            unsupported.append(key.code)
            continue
        wire_to_codes.setdefault(wire, []).append(key.code)

    wire_symbols: list[str] = []
    to_internal: dict[str, str] = {}
    collisions: dict[str, tuple[str, ...]] = {}
    for wire, codes in wire_to_codes.items():
        if len(codes) > 1:
            collisions[wire] = tuple(codes)
            logger.warning(
                "build_provider_request_plan: provider=%s wire_symbol=%s excluded — "
                "distinct internal codes collide: %s",
                provider,
                wire,
                codes,
            )
            continue
        wire_symbols.append(wire)
        to_internal[wire] = codes[0]

    return ProviderRequestPlan(
        wire_symbols=tuple(wire_symbols),
        to_internal=to_internal,
        unsupported=tuple(unsupported),
        collisions=collisions,
    )
