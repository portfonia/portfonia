"""Parse free-form holdings text (CSV / Markdown / plain text) via LLM."""

import io
import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from string import Template
from typing import Any

import openai
import yaml
from pydantic import ValidationError

from app.core.config import OR_ATTRIBUTION_HEADERS, get_settings
from app.core.llm import structured_provider
from app.schemas.holdings import (
    KNOWN_ISSUE_CODES,
    VALID_ASSET_TYPES,
    VALID_CURRENCIES,
    BrokerGroup,
    CurrencySubtotal,
    IssueRow,
    ParsedRow,
    UploadPreview,
)
from app.services._yfinance import _TICKER_SYMBOL_OVERRIDE
from app.services.asset_class_config import VALID_ASSET_CLASSES
from app.services.holdings_export import DIALECT_TAG_KEYS, MANUAL_LISTED_PLACEHOLDER
from app.services.llm_errors import (
    LLMCallError,
    LLMEmptyResponseError,
    LLMErrorCode,
    LLMInvalidJSONError,
    classify,
    is_retryable,
)
from app.services.markets import (
    SUPPORTED_CAPTURE_MARKETS,
    VALID_HOLDING_MARKETS,
    market_from_ticker,
    resolve_holding_market,
)

_SUPPORTED_EXTENSIONS = {".md", ".txt", ".csv", ".xlsx", ".xls"}

# backend/ = two levels above this file (services/holding_parser.py → app/ → backend/)
_BACKEND_DIR = Path(__file__).resolve().parents[2]
_DEFAULT_VOCAB_FILE = _BACKEND_DIR / "config" / "holding_parser_vocab.yml"


@dataclass(frozen=True)
class _HoldingParserVocab:
    cny_institutions: tuple[str, ...]
    common_cn_platforms: tuple[str, ...]
    futu: str
    stock_connect: str
    cash: str
    margin: str
    deposit: str
    money_market: str
    index_fund: str
    wmp_terms: str
    a_share_terms: str
    us_market_zh: str
    hk_market_zh: str
    market_aliases_zh: dict[str, str]


def _get_holding_parser_vocab_path() -> Path:
    override = get_settings().HOLDING_PARSER_VOCAB_PATH
    return Path(override) if override else _DEFAULT_VOCAB_FILE


def _load_holding_parser_vocab(path: Path | None = None) -> _HoldingParserVocab:
    """Load the Chinese-language example data for the extraction system prompt."""
    target = path or _get_holding_parser_vocab_path()
    with target.open(encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    return _HoldingParserVocab(
        cny_institutions=tuple(raw["cny_institutions"]),
        common_cn_platforms=tuple(raw["common_cn_platforms"]),
        futu=raw["futu"],
        stock_connect=raw["stock_connect"],
        cash=raw["cash"],
        margin=raw["margin"],
        deposit=raw["deposit"],
        money_market=raw["money_market"],
        index_fund=raw["index_fund"],
        wmp_terms=raw["wmp_terms"],
        a_share_terms=raw["a_share_terms"],
        us_market_zh=raw["us_market_zh"],
        hk_market_zh=raw["hk_market_zh"],
        market_aliases_zh=dict(raw["market_aliases_zh"]),
    )


# Loaded once at import — shared by the system-prompt template and _MARKET_ALIASES.
_VOCAB = _load_holding_parser_vocab()


# string.Template ($identifier substitution) rather than str.format(): the
# prompt's JSON schema examples below are full of literal { } braces that
# str.format() would try (and fail) to parse as fields.
_SYSTEM_PROMPT_TEMPLATE = Template("""\
You are a structured data extractor for an investment portfolio service.

The user uploads a free-form text file listing their holdings. Extract each holding
into a structured record. The file may be CSV, Markdown, plain text, or a mix.
Column order and naming are flexible; the user may write in Chinese or English.

Output a JSON object with exactly two keys:
  "valid_rows"  - list of successfully parsed holdings
  "issue_rows"  - list of rows you could not reliably parse

--- valid_rows item schema ---
{
  "name":          string  (asset display name, required),
  "ticker":        string | null  (market symbol, e.g. AAPL, 0700.HK, 600519.SS),
  "fund_code":     string | null  (6-digit Chinese public fund code, e.g. 005827),
  "currency":      string  (ISO 4217, required — infer if not stated; see rules below),
  "shares":        number | null  (units held; null for manual-valuation assets),
  "avg_cost":      number | null  (cost per unit; null for manual-valuation assets),
  "current_value": number | null  (total value supplied by user for manual assets),
  "pricing_mode":  "auto" | "manual"  (inferred — see rules below),
  "asset_type":    "stock" | "etf" | "fund" | "cash" | "wmf" | "other" | null,
  "market":        "US" | "HK" | "A-Share" | "UK" | "Europe" | "Japan" | "Korea" | "Other" | null,
  "broker":        string | null,
  "account":       string | null,
  "portfolio":     string | null,
  "notes":         string | null,
  "confidence":    number  (0.0-1.0; < 0.7 means the field values are uncertain)
}

Do not emit an "issues" array. The server attaches structured notes after
extraction. When a field is uncertain, lower confidence instead of writing
a free-text note. Trailing tags on a line (account: / portfolio: / notes: /
asset_type: / market: / pricing_mode:) are structured fields — copy them
onto the matching keys, unquoting values.

--- issue_rows item schema ---
{
  "raw":    string  (the original line or row text),
  "reason": string  (why it could not be parsed — user-facing, bilingual EN/CN preferred)
}

--- pricing_mode inference ---
Set "auto" when:
  - A ticker or fund_code is present AND shares + avg_cost are provided, OR
  - A ticker or fund_code is present and price can be fetched externally.
Set "manual" when:
  - No ticker and no fund_code (e.g. cash, bank WMP, Alipay products), OR
  - Only a total current_value is given with no per-unit cost.
When a row has both a ticker and a current_value but no shares: prefer "auto" and
lower confidence if the split is ambiguous.

--- currency inference (apply in priority order) ---
1. User explicitly states a currency code (USD, HKD, CNY, GBP, …) → use it.
2. ticker ends in .HK → HKD
3. ticker ends in .SS or .SZ → CNY
4. ticker ends in .L → GBP
5. ticker ends in .AS / .PA / .DE → EUR
6. ticker ends in .T → JPY
7. ticker ends in .KS or .KQ → KRW
8. identifier is all digits (6-digit fund code or A-share code) → CNY
9. name or broker contains a mainland Chinese institution
   ($cny_institutions) → CNY
10. ticker is pure ASCII letters with no suffix AND no other CNY/HKD/EUR/JPY/KRW signals → USD
11. asset is cash/$cash/$margin/margin/deposit: infer from broker context;
   if broker is foreign (IBKR, Schwab, Fidelity, TD, Futu USD account) → USD;
   if broker is mainland Chinese → CNY;
   if broker is Hong Kong platform ($futu/Futu HKD, $stock_connect) → HKD;
   if cannot determine → set confidence < 0.7.
12. Otherwise: make best guess and lower confidence if uncertain.

--- asset_type inference ---
- Has ticker with exchange suffix (.HK, .SS, .SZ) or well-known US ticker → "stock"
- Name contains ETF/$index_fund/index fund keywords or ticker starts with common ETF pattern → "etf"
- Has a 6-digit fund_code → "fund"
- Name contains $cash/cash/$deposit/$money_market/$margin/margin/deposit → "cash"
- Bank-sold WMP ($wmp_terms) → "wmf"
- Cannot determine → unknown (omit the key — see output compactness below)

--- market inference (the user groups capital by market; preserve their intent) ---
1. The user explicitly gives a market/exchange column (US, HK, A-Share/$a_share_terms,
   UK, Europe, Japan, Korea, $us_market_zh, $hk_market_zh, etc.) → map it to one of
   US / HK / A-Share / UK / Europe / Japan / Korea / Other and use it.
2. ticker ends in .HK → HK
3. ticker ends in .SS or .SZ, OR a 6-digit fund_code, OR a 6-digit A-share code → A-Share
4. ticker ends in .L → UK; .AS / .PA / .DE → Europe; .T → Japan; .KS / .KQ → Korea
5. plain US-listed ticker (no suffix, or a one-letter share class like BRK.B) → US
6. cash / WMP / deposit with no ticker: market is listing venue, not custodian.
   There is no listed instrument → Other. Do NOT infer A-Share from a mainland
   bank broker (a USD deposit at CMB / China Merchants Bank is Other, not A-Share).
7. cannot determine → Other. Do not drop the row.

--- quality bar for valid_rows ---
A row is valid if it has at minimum: name + currency + (shares OR current_value).
A row is an issue_row if: name cannot be determined, OR both shares/current_value
are missing, OR the format is completely unintelligible.

Do not hallucinate tickers or fund codes — if uncertain, treat the field as
unknown (see output compactness below for how an unknown field is rendered)
and lower confidence rather than guessing.

--- output compactness (issue #84) ---
"name", "currency", and "pricing_mode" are always required and always
present. Every other key is optional: when a field is unknown, not
applicable, or would otherwise be null/an empty string/an empty list, OMIT
that key from the object entirely rather than writing it as null/[]/"". This
keeps output size proportional to actual content, not the full schema.
""")


def _build_system_prompt(v: _HoldingParserVocab) -> str:
    return _SYSTEM_PROMPT_TEMPLATE.substitute(
        cny_institutions=", ".join(v.cny_institutions),
        common_cn_platforms=" / ".join(v.common_cn_platforms),
        futu=v.futu,
        stock_connect=v.stock_connect,
        cash=v.cash,
        margin=v.margin,
        deposit=v.deposit,
        money_market=v.money_market,
        index_fund=v.index_fund,
        wmp_terms=v.wmp_terms,
        a_share_terms=v.a_share_terms,
        us_market_zh=v.us_market_zh,
        hk_market_zh=v.hk_market_zh,
    )


_SYSTEM_PROMPT = _build_system_prompt(_VOCAB)


def _strip_comments(text: str) -> str:
    """Remove comment lines before sending to the LLM.

    A comment line is any line whose first non-whitespace character is '#'.
    This covers both single '#' and multi-char markers like '#####'.
    Stripped deterministically — the LLM never sees these lines, so they
    can never bleed into valid_rows or issue_rows regardless of model behaviour.
    """
    lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
    return "\n".join(lines)


_TAG_TOKEN_RE = re.compile(
    r"\s+(" + "|".join(DIALECT_TAG_KEYS) + r'):(?:"(?P<quoted>(?:\\.|[^"\\])*)"|(?P<bare>\S+))\s*$'
)

_ASSET_TYPE_ALIASES: dict[str, str] = {
    "wealth-management": "wmf",
    "wealth_management": "wmf",
    "wmp": "wmf",
    "wmf": "wmf",
}


def _unescape_tag(value: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(value):
        if value[i] == "\\" and i + 1 < len(value):
            out.append(value[i + 1])
            i += 2
        else:
            out.append(value[i])
            i += 1
    return "".join(out)


def split_tagged_fields(line: str) -> tuple[str, dict[str, str]]:
    """Peel trailing key:value tags off a dialect line, right to left."""
    tags: dict[str, str] = {}
    rest = line.rstrip()
    while True:
        match = _TAG_TOKEN_RE.search(rest)
        if not match:
            break
        key = match.group(1)
        if match.group("quoted") is not None:
            tags[key] = _unescape_tag(match.group("quoted"))
        else:
            tags[key] = match.group("bare") or ""
        rest = rest[: match.start()]
    return rest, tags


def _is_num(token: str) -> bool:
    try:
        float(token.replace(",", ""))
        return True
    except ValueError:
        return False


def _parse_listed_tokens(tokens: list[str]) -> dict[str, Any] | None:
    for i, tok in enumerate(tokens):
        if tok.upper() not in VALID_CURRENCIES:
            continue
        if i < 1 or i + 2 >= len(tokens):
            continue
        if not (_is_num(tokens[i + 1]) and _is_num(tokens[i + 2])):
            continue
        ident = tokens[i - 1]
        name = " ".join(tokens[: i - 1]).strip()
        if not name:
            continue
        broker = " ".join(tokens[i + 3 :]).strip() or None
        row: dict[str, Any] = {
            "name": name,
            "currency": tok.upper(),
            "shares": float(tokens[i + 1].replace(",", "")),
            "avg_cost": float(tokens[i + 2].replace(",", "")),
            "broker": broker,
            "pricing_mode": "auto",
        }
        if ident.isdigit() and len(ident) == 6:
            row["fund_code"] = ident
            row["ticker"] = None
        else:
            row["ticker"] = ident
            row["fund_code"] = None
        return row
    return None


def _ident_from_token(ident: str) -> dict[str, Any]:
    if ident.isdigit() and len(ident) == 6:
        return {"fund_code": ident, "ticker": None}
    return {"ticker": ident, "fund_code": None}


def _manual_match(tokens: list[str], n_nums: int) -> dict[str, Any] | None:
    """Last currency token followed by exactly n_nums numerics.

    Walking last-match avoids a currency code embedded in the name
    (e.g. "My iShares USD 500 Bond ETF") stealing the parse.
    """
    last: dict[str, Any] | None = None
    for i, tok in enumerate(tokens):
        if tok.upper() not in VALID_CURRENCIES:
            continue
        if i < 1 or i + n_nums >= len(tokens):
            continue
        nums = tokens[i + 1 : i + 1 + n_nums]
        if not all(_is_num(t) for t in nums):
            continue
        # A longer numeric run belongs to a richer manual shape.
        if i + 1 + n_nums < len(tokens) and _is_num(tokens[i + 1 + n_nums]):
            continue
        ident = tokens[i - 1]
        name = " ".join(tokens[: i - 1]).strip()
        if not name:
            continue
        broker = " ".join(tokens[i + 1 + n_nums :]).strip() or None
        row: dict[str, Any] = {
            "name": name,
            "currency": tok.upper(),
            "broker": broker,
            "pricing_mode": "manual",
            "shares": None,
            "avg_cost": None,
            "current_value": None,
            **_ident_from_token(ident),
        }
        parsed_nums = [float(t.replace(",", "")) for t in nums]
        if n_nums == 3:
            row["shares"], row["avg_cost"], row["current_value"] = parsed_nums
        elif n_nums == 2:
            row["shares"], row["avg_cost"] = parsed_nums
        else:
            row["current_value"] = parsed_nums[0]
        last = row
    return last


def _is_num_or_placeholder(token: str) -> bool:
    return token == MANUAL_LISTED_PLACEHOLDER or _is_num(token)


def _manual_match_explicit(tokens: list[str]) -> dict[str, Any] | None:
    """shares / avg_cost / current_value, each a number or the placeholder.

    Unambiguous by construction — this is what `render_holding_line` emits
    for pricing_mode:manual listed rows (always all three slots), so cost
    basis / valuation survive an export -> edit -> replace-all round trip
    even when the middle slot is missing. Blind positional counting alone
    cannot tell "shares + current_value, no avg_cost" apart from "shares +
    avg_cost, no current_value" — both are two numeric tokens (PR #310
    round 5, the round-4 bug this replaces for the export-generated shape).
    Hand-typed shorthand without the placeholder still falls back to
    `_manual_match`'s count-based heuristic below.
    """
    last: dict[str, Any] | None = None
    for i, tok in enumerate(tokens):
        if tok.upper() not in VALID_CURRENCIES:
            continue
        if i < 1 or i + 3 >= len(tokens):
            continue
        nums = tokens[i + 1 : i + 4]
        if not all(_is_num_or_placeholder(t) for t in nums):
            continue
        if all(t == MANUAL_LISTED_PLACEHOLDER for t in nums):
            continue  # nothing recoverable; let the flexible fallback try
        if i + 4 < len(tokens) and _is_num(tokens[i + 4]):
            continue
        ident = tokens[i - 1]
        name = " ".join(tokens[: i - 1]).strip()
        if not name:
            continue
        broker = " ".join(tokens[i + 4 :]).strip() or None
        shares, avg_cost, current_value = (
            None if t == MANUAL_LISTED_PLACEHOLDER else float(t.replace(",", "")) for t in nums
        )
        last = {
            "name": name,
            "currency": tok.upper(),
            "broker": broker,
            "pricing_mode": "manual",
            "shares": shares,
            "avg_cost": avg_cost,
            "current_value": current_value,
            **_ident_from_token(ident),
        }
    return last


def _parse_manual_listed_tokens(tokens: list[str]) -> dict[str, Any] | None:
    """pricing_mode:manual listed shape: shares / avg_cost / current_value.

    Tries the unambiguous placeholder-marked shape first (what export always
    produces), then falls back to count-based heuristics for hand-typed
    shorthand without placeholders: three numerics after currency are
    shares / avg_cost / current_value, two are shares / avg_cost, one is
    current_value only.
    """
    return (
        _manual_match_explicit(tokens)
        or _manual_match(tokens, 3)
        or _manual_match(tokens, 2)
        or _manual_match(tokens, 1)
    )


def _parse_cash_tokens(tokens: list[str]) -> dict[str, Any] | None:
    for i in range(len(tokens) - 1, 0, -1):
        if tokens[i].upper() not in VALID_CURRENCIES:
            continue
        if not _is_num(tokens[i - 1]):
            continue
        name = " ".join(tokens[: i - 1]).strip()
        if not name:
            continue
        broker = " ".join(tokens[i + 1 :]).strip() or None
        return {
            "name": name,
            "ticker": None,
            "fund_code": None,
            "currency": tokens[i].upper(),
            "shares": None,
            "avg_cost": None,
            "current_value": float(tokens[i - 1].replace(",", "")),
            "broker": broker,
            "pricing_mode": "manual",
        }
    return None


def parse_dialect_line(line: str) -> dict[str, Any] | None:
    """Parse one export-dialect line. Returns None if the line is not ours."""
    stripped = line.strip()
    if not stripped or stripped.lstrip().startswith("#"):
        return None
    rest, tags = split_tagged_fields(stripped)
    tokens = rest.split()
    if len(tokens) < 3:
        return None
    asset_type = tags.get("asset_type")
    if asset_type is not None:
        asset_type = _ASSET_TYPE_ALIASES.get(asset_type, asset_type)
        tags["asset_type"] = asset_type
    if asset_type in ("cash", "wmf"):
        parsed = _parse_cash_tokens(tokens)
    else:
        # Try the placeholder-marked manual 3-slot shape first, regardless
        # of any pricing_mode tag: it is unambiguous by construction (see
        # _manual_match_explicit's docstring — a 2-slot auto row's tokens
        # can't accidentally satisfy it in the normal case), and pricing_
        # mode is no longer an export tag (issue #319 item 8, dropped
        # precisely because it's the one tag every Holding always has —
        # keeping it would have meant the dialect fast path never actually
        # retires). A manual row can still reach this function tagged only
        # by a surviving account/portfolio/notes tag, with no pricing_mode
        # tag to route on, so detecting the shape itself, not the tag, is
        # what keeps that case parsing correctly instead of silently
        # misrouting current_value into the broker field.
        parsed = _manual_match_explicit(tokens)
        if parsed is None:
            if tags.get("pricing_mode") == "manual":
                parsed = _parse_manual_listed_tokens(tokens)
            else:
                parsed = _parse_listed_tokens(tokens)
                if parsed is None and asset_type is None:
                    parsed = _parse_cash_tokens(tokens)
    if parsed is None:
        return None
    for key, value in tags.items():
        if value == "":
            continue
        parsed[key] = value
    if "pricing_mode" not in parsed:
        parsed["pricing_mode"] = "manual" if parsed.get("asset_type") in ("cash", "wmf") else "auto"
    parsed.setdefault("issues", [])
    parsed.setdefault("confidence", 1.0)
    return parsed


def try_parse_dialect(text: str) -> list[dict[str, Any]] | None:
    """If every data line carries at least one trailing tag and parses, skip the LLM.

    Untagged free-form uploads still go through the model. An export from
    GET /holdings/export always includes tags, so re-import is deterministic.
    A mixed file (only some lines tagged) must not divert the whole upload
    onto positional parsing (PR #310 round 2).
    """
    lines = [ln.strip() for ln in _strip_comments(text).splitlines() if ln.strip()]
    if not lines:
        return None
    rows: list[dict[str, Any]] = []
    for ln in lines:
        _rest, tags = split_tagged_fields(ln)
        if not tags:
            return None
        parsed = parse_dialect_line(ln)
        if parsed is None:
            return None
        parsed["_source_line"] = ln
        rows.append(parsed)
    return rows


def _extract_text(file_bytes: bytes, filename: str) -> str:
    """Convert uploaded file bytes to plain text for LLM ingestion."""
    suffix = Path(filename).suffix.lower()
    if suffix not in _SUPPORTED_EXTENSIONS:
        raise ValueError(
            f"Unsupported file type '{suffix}'. Please upload .md, .txt, .csv, .xlsx, or .xls."
        )
    if suffix in (".md", ".txt", ".csv"):
        # Mainland Chinese broker/fund-platform exports commonly use GBK/GB2312,
        # not UTF-8. Try UTF-8 first, then fall back to gb18030 (a superset of
        # both) so a legitimately-encoded CN export doesn't 500 on decode.
        try:
            return _strip_comments(file_bytes.decode("utf-8"))
        except UnicodeDecodeError:
            return _strip_comments(file_bytes.decode("gb18030"))
    # Excel path
    try:
        import pandas as pd
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("pandas is required for Excel support") from exc

    xf = pd.ExcelFile(io.BytesIO(file_bytes))
    if len(xf.sheet_names) > 1:
        names = ", ".join(str(n) for n in xf.sheet_names)
        raise ValueError(
            f"Excel file contains {len(xf.sheet_names)} sheets ({names}). "
            f"Please keep only one sheet and re-upload."
        )
    # dtype=str preserves leading zeros in identifier columns: without it pandas
    # reads e.g. 00700 / 02333 as ints (700 / 2333) before the LLM ever sees the
    # cell, an unrecoverable loss. Same failure family as the HK-ticker fix
    # (#49); this guards the pre-LLM xlsx path. (#53)
    df = xf.parse(xf.sheet_names[0], dtype=str)
    return str(df.to_csv(index=False))


_HK_TICKER_RE = re.compile(r"^0*(\d+)\.HK$", re.IGNORECASE)


def _normalize_hk_ticker(ticker: str) -> str:
    """Canonicalize a Hong Kong ticker to yfinance's 4-digit form.

    HKEX moved equity codes to a 5-digit scheme (leading-zero padded), and users
    commonly write either form (02333.HK vs 2333.HK). yfinance expects the
    4-digit form (0700.HK, 2333.HK), so a stray leading zero makes the price
    lookup miss silently. Strip leading zeros, then left-pad numeric codes below
    10000 back to 4 digits. Genuine 5-digit codes (>=10000, e.g. derivatives)
    are left as their bare numeric form. Non-HK or unrecognized tickers pass
    through unchanged. (issue #49)
    """
    m = _HK_TICKER_RE.match(ticker)
    if not m:
        return ticker
    num = int(m.group(1))
    digits = f"{num:04d}" if num < 10000 else str(num)
    return f"{digits}.HK"


_TICKER_CURRENCY_MAP = {
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


def normalize_ticker_and_currency(row: dict[str, Any], *, emit_note: bool = True) -> None:
    """Canonicalize a ticker (HK 4-digit form) and correct currency from its suffix.

    Three callers share this: `_postprocess` runs it twice — once before
    force-suffix (canonicalizes a ticker the user already suffixed, e.g.
    02333.HK -> 2333.HK) and once after (canonicalizes a suffix
    `apply_confirmed_exchange_suffix` just added, e.g. 700 -> 700.HK ->
    0700.HK) — and the non-LLM write path (`POST`/`PATCH /holdings` via
    `_apply_write_defaults`) runs it too. Before this was extracted, the
    router never ran this step: an API write of ticker "700" + HKD
    force-suffixed to "700.HK" while the identical input via file-import
    produced "0700.HK" — a stored-form divergence that silently misses
    `ticker_themes`/config-YAML lookups keyed on the canonical form (PR
    #310 round 5 review).
    """
    ticker = row.get("ticker")
    if not ticker:
        return
    normalized = _normalize_hk_ticker(ticker)
    if normalized != ticker:
        if emit_note:
            _append_issue(row, "ticker_normalized_hk", {"ticker": normalized})
        row["ticker"] = ticker = normalized
    for suffix, currency in _TICKER_CURRENCY_MAP.items():
        if ticker.lower().endswith(suffix):
            if row.get("currency") != currency:
                if emit_note:
                    _append_issue(
                        row,
                        "currency_corrected",
                        {"currency": currency, "suffix": suffix.upper()},
                    )
                row["currency"] = currency
            break


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
_CAPTURE_MARKETS = SUPPORTED_CAPTURE_MARKETS

# Currencies worth a ticker_no_suffix hint when a suffix cannot be applied
# (market undetermined, or determined but ambiguous — Europe/Korea each
# have multiple listing suffixes). Every currency this module resolves to a
# live capture market via _confirmed_market, so the set must track that
# resolution, not stop at the original GBP/HKD/CNY set from before #312
# widened capture to UK/Europe/Japan/Korea (PR #310 round 5 review — EUR
# and KRW silently got no hint after that widening).
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


def _confirmed_market(row: dict[str, Any]) -> str | None:
    """User-set market, else a confident derivation. None = do not guess a suffix."""
    mkt = row.get("market")
    if mkt in VALID_HOLDING_MARKETS:
        return str(mkt)
    ticker = row.get("ticker") or ""
    upper = ticker.upper()
    for suf in _FORCE_EXCHANGE_SUFFIXES:
        if upper.endswith(suf):
            inferred = market_from_ticker(ticker)
            if inferred is not None:
                return inferred
            break
    if row.get("fund_code"):
        return "A-Share"
    base = _ticker_base(ticker).upper()
    if base in _TICKER_SYMBOL_OVERRIDE:
        # Override target is .L (PSH -> PSH.L); UK is a real capture market.
        return "UK"
    currency = row.get("currency")
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


def apply_confirmed_exchange_suffix(row: dict[str, Any], *, emit_note: bool = True) -> None:
    """Force exchange suffix once market is determined.

    Gate: user-set or confidently derived market in a live capture slice
    (the 7 scheduled buckets). When market cannot be determined at all, do
    not guess; `ticker_no_suffix` fires on preview. When market IS determined
    but the specific suffix is ambiguous (Europe/Korea have several venues)
    or unplaceable (an A-share code outside the recognized digit ranges),
    the market is still persisted onto the row and `row["_suffix_ambiguous"]`
    is set so the caller (`_postprocess` / `_apply_write_defaults`) forces
    `capture_supported=False` after `resolve_holding_market` runs — a
    still-bare ticker would otherwise fall through `market_from_ticker`'s
    "no suffix = US" default and get speculatively fetched as an unrelated
    US security under the real holding's identity (the PSH-class silent
    wrong-security failure; reproduced with a bare EUR/KRW ticker and an
    unplaceable A-share code — PR #310 round 6 review). Applying `.L`
    persists UK (PSH.L is a London listing; UK captures). Bare-PSH lookup
    still uses `_TICKER_SYMBOL_OVERRIDE` PSH -> PSH.L. Cash/wmf have no
    ticker. Dotted share-class tickers (BRK.B) are left unsuffixed.
    Idempotent if the suffix is already present. A manual-priced row's
    "ticker" is a free-text label, not a market symbol — capture never reads
    pricing_mode:manual rows (`_market_tickers` only selects "auto"), so
    forcing a suffix serves no purpose there and risks corrupting a label
    that only looks like a real ticker (e.g. HOME -> HOME.HK) (PR #310
    round 5 review).
    """
    if row.get("asset_type") in ("cash", "wmf") or row.get("pricing_mode") == "manual":
        return
    ticker = row.get("ticker")
    if not isinstance(ticker, str) or not ticker.strip():
        return
    ticker = ticker.strip()
    market = _confirmed_market(row)
    already = _known_exchange_suffix(ticker) is not None or "." in ticker
    currency = row.get("currency")
    currency_s = str(currency) if currency is not None else ""

    if market not in _CAPTURE_MARKETS:
        if emit_note and not already and market is None and currency_s in _SUFFIX_HINT_CURRENCIES:
            _append_issue(
                row,
                "ticker_no_suffix",
                {"ticker": ticker, "currency": currency_s},
                severity="warning",
            )
        return

    if already:
        if not row.get("market"):
            row["market"] = market
        return

    suffix = _exchange_suffix_to_apply(market, ticker, currency_s or None)
    if suffix is None:
        if not row.get("market"):
            row["market"] = market
        if market not in _AMBIGUOUS_SUFFIX_OPTIONS:
            # US (the only capture market with no suffix at all) legitimately
            # falls through here on every plain US ticker — a bare AAPL is
            # already its correct stored form, not an unresolved one, so it
            # must not be flagged ambiguous or forced capture_supported=False.
            return
        row["_suffix_ambiguous"] = True
        if emit_note and currency_s in _SUFFIX_HINT_CURRENCIES:
            _append_issue(
                row,
                "ticker_suffix_ambiguous",
                {
                    "ticker": ticker,
                    "currency": currency_s,
                    "market": market,
                    "suffixes": _AMBIGUOUS_SUFFIX_OPTIONS.get(market, ""),
                },
                severity="warning",
            )
        return

    row["ticker"] = f"{ticker}{suffix}"
    if not row.get("market"):
        row["market"] = market


_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*\n(.*?)\n```\s*$", re.DOTALL | re.IGNORECASE)


def _strip_code_fence(content: str) -> str:
    """Unwrap a markdown ```json ... ``` fence if present.

    Anthropic models on OpenRouter ignore ``response_format=json_object`` and
    wrap JSON in markdown fences. Strip them before parsing. Returns the inner
    payload, or the original string trimmed when no fence is found.
    """
    match = _FENCE_RE.match(content)
    return match.group(1) if match else content.strip()


# backend/ = two levels above this file (services/holding_parser.py → app/ → backend/)
_DEFAULT_TICKER_ASSET_CLASS_FILE = _BACKEND_DIR / "config" / "ticker_asset_class.yml"


# The ticker/fund_code → asset_class mapping moved out of this module into
# config/ticker_asset_class.yml (issue #296): an admin can add a real
# production fund_code without a code deploy. Re-read on every classification
# (no cache — mirroring asset_class_config.load_asset_class_config and
# i18n_glossary.load_i18n_glossary), so a newly added entry is live on the
# next parse with no process restart (the #35 live-reload property).
def _get_ticker_asset_class_path() -> Path:
    override = get_settings().TICKER_ASSET_CLASS_CONFIG_PATH
    return Path(override) if override else _DEFAULT_TICKER_ASSET_CLASS_FILE


def _load_ticker_asset_class() -> dict[str, str]:
    """Return the ticker/fund_code → asset_class mapping from YAML.

    No cache: re-opens the file on every call so an in-place admin edit takes
    effect on the next parse (issue #296 — the #35 live-reload property;
    sibling loaders behave the same). Fails loudly on drift, matching
    asset_class_thresholds.yml's closed-taxonomy discipline: an unknown
    asset_class VALUE is a config typo, not a silent catch-all.
    """
    target = _get_ticker_asset_class_path()
    with target.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    raw = data.get("ticker_asset_class")
    if not isinstance(raw, dict):
        raise ValueError(
            f"ticker_asset_class config at {target} is missing the "
            f"'ticker_asset_class' top-level map"
        )
    # yaml.safe_load parses bare numeric keys like 513100 as int; canonicalize
    # every key to str so the lookups (which use str ticker/fund_code) hit.
    mapping = {str(k): str(v) for k, v in raw.items()}
    unknown = set(mapping.values()) - VALID_ASSET_CLASSES
    if unknown:
        raise ValueError(
            f"ticker_asset_class config at {target} maps to unknown "
            f"asset_class value(s): {sorted(unknown)} — the taxonomy is "
            f"closed (VALID_ASSET_CLASSES)"
        )
    return mapping


_ASSET_TYPE_CLASS: dict[str, str] = {
    "stock": "STOCK",
    "etf": "EQUITY_BROAD",  # unknown ETF: global catch-all; ticker lookup overrides
    "fund": "EQUITY_BROAD",  # unknown fund: global catch-all; ticker/fund_code lookup overrides
    "cash": "CASH_EQUIV",
    "wmf": "CASH_EQUIV",
    "other": "STOCK",
}


def _classify_asset_class(row: dict[str, Any]) -> str:
    mapping = _load_ticker_asset_class()
    ticker = (row.get("ticker") or "").upper()
    if ticker and ticker in mapping:
        return mapping[ticker]
    fund_code = row.get("fund_code") or ""
    if fund_code and fund_code in mapping:
        return mapping[fund_code]
    return _ASSET_TYPE_CLASS.get(row.get("asset_type") or "", "STOCK")


# Map common free-text market labels the model may emit onto the canonical
# bucket. EN aliases here; Chinese-language aliases load from
# holding_parser_vocab.yml's market_aliases_zh (issue #90).
_MARKET_ALIASES: dict[str, str] = {
    "us": "US",
    "usa": "US",
    "hk": "HK",
    "hkex": "HK",
    "a-share": "A-Share",
    "ashare": "A-Share",
    "cn": "A-Share",
    "china": "A-Share",
    "uk": "UK",
    "lse": "UK",
    "gb": "UK",
    "europe": "Europe",
    "eu": "Europe",
    "euronext": "Europe",
    "xetra": "Europe",
    "japan": "Japan",
    "jp": "Japan",
    "tse": "Japan",
    "korea": "Korea",
    "kr": "Korea",
    "krx": "Korea",
    **_VOCAB.market_aliases_zh,
}


def _coerce_issue_list(raw: object) -> list[dict[str, Any]]:
    """Keep only deterministic postprocess codes; drop LLM free-text notes."""
    if not raw:
        return []
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or "")
        if code not in KNOWN_ISSUE_CODES:
            continue
        params = item.get("params") or {}
        if not isinstance(params, dict):
            params = {}
        str_params = {str(k): str(v) for k, v in params.items()}
        severity = item.get("severity") or "info"
        if severity not in ("info", "warning"):
            severity = "info"
        out.append({"code": code, "params": str_params, "severity": severity})
    return out


def _append_issue(
    row: dict[str, Any],
    code: str,
    params: dict[str, str] | None = None,
    *,
    severity: str = "info",
) -> None:
    issues = _coerce_issue_list(row.get("issues"))
    issues.append({"code": code, "params": params or {}, "severity": severity})
    row["issues"] = issues


def _postprocess(
    raw_rows: list[dict[str, Any]],
    on_invalid_row: Callable[[dict[str, Any], str], None] | None = None,
    *,
    dedup: bool = True,
) -> list[ParsedRow]:
    """Apply deterministic post-processing on top of LLM output.

    `on_invalid_row`, if given, is invoked for any row that still fails
    ParsedRow validation after normalization (e.g. a currency the LLM
    hallucinated that isn't in VALID_CURRENCIES) — the row is dropped from
    the returned list rather than raising, so one bad row can't fail the
    whole upload (issue #25/PR #114 review: currency validation used to
    propagate a bare ValidationError out of parse(), killing every other
    valid row in the same file).

    Dedup only collapses byte-identical rows (an LLM emitting the same holding
    twice). Skip it for dialect-parsed rows: the exporter does not duplicate,
    and #92 treats identical lots as a second lot, never a silent merge
    (PR #310 round 2).
    """
    result: list[ParsedRow] = []
    # The key includes broker/account/quantity so two genuinely distinct
    # lots — e.g. the same ETF at two brokers — are both preserved. (issue #50)
    seen: set[tuple[str | None, ...]] = set()

    for row in raw_rows:
        row["issues"] = _coerce_issue_list(row.get("issues"))

        # Normalize asset_type to the known set BEFORE validation so an off-list
        # value from the model is coerced to null (with a note) rather than
        # either crashing a strict Literal or silently persisting garbage.
        at = row.get("asset_type")
        if at is not None and at not in VALID_ASSET_TYPES:
            _append_issue(
                row,
                "unrecognized_asset_type",
                {"asset_type": str(at)},
                severity="warning",
            )
            row["asset_type"] = None

        # Normalize market to the closed set; an unmappable non-null value
        # becomes "Other" rather than tripping the Literal or being lost.
        # Then resolve capture_supported from ticker/market (issue #311) —
        # unresolvable tickers stay in valid_rows as Other + not-processed.
        mkt = row.get("market")
        if mkt is not None and mkt not in VALID_HOLDING_MARKETS:
            row["market"] = _MARKET_ALIASES.get(str(mkt).strip().lower(), "Other")
        # Capture resolution runs after force-suffix so PSH.L maps to UK
        # rather than a bare-PSH US stamp (issue #311 / PR #312).

        # Normalize currency case/whitespace before validation — an LLM
        # emitting "usd" instead of "USD" shouldn't trip the exact-match
        # VALID_CURRENCIES check below (PR #114 review: this was previously
        # case/alias-strict with no normalization pass, unlike asset_type
        # and market above). Only case/whitespace normalization happens
        # here — the VALID_CURRENCIES membership check itself runs LAST,
        # after ticker-suffix correction, so a wrong-but-fixable value
        # (e.g. "RMB" on a .HK ticker) doesn't leave a stale "unrecognized"
        # issue note on a row whose final currency is actually valid
        # (PR #114 review round 2 finding).
        cur = row.get("currency")
        if isinstance(cur, str):
            normalized_cur = cur.strip().upper()
            if normalized_cur != cur:
                _append_issue(row, "currency_normalized", {"currency": normalized_cur})
                row["currency"] = normalized_cur

        # Canonicalize HK tickers to yfinance's 4-digit form (02333.HK -> 2333.HK)
        # and correct currency from the ticker's suffix, so price lookups
        # don't miss on a leading-zero variant. (issue #49)
        normalize_ticker_and_currency(row)

        # Cash/wmf rows carry no real instrument identifier (issue #120): the
        # model has been observed both fabricating a ticker like "CASH" that
        # isn't even in the source text, and echoing a stray "CASH"-shaped
        # token from the source into the ticker/fund_code field. Either way,
        # a leftover ticker/fund_code plus a missing current_value silently
        # drops the row out of every report — compute_portfolio()'s
        # manual-pricing branch only ever reads current_value, never shares.
        # Coerce deterministically rather than trust the prompt's "no
        # ticker, amount in current_value" rule to always be followed.
        if row.get("asset_type") in ("cash", "wmf"):
            bogus_id = row.get("ticker") or row.get("fund_code")
            if bogus_id:
                _append_issue(row, "dropped_spurious_id", {"identifier": str(bogus_id)})
                row["ticker"] = None
                row["fund_code"] = None
            # Only moves the amount from shares when current_value is still
            # None — a row where the model populated BOTH fields (e.g. a
            # bogus current_value alongside the real balance in shares)
            # keeps current_value as originally given rather than guessing
            # which of two conflicting numbers is real.
            if row.get("current_value") is None and row.get("shares") is not None:
                _append_issue(row, "cash_amount_moved")
                row["current_value"] = row["shares"]
                row["shares"] = None
                row["avg_cost"] = None
            # Once current_value is settled as the source of truth, always
            # clear any residual shares/avg_cost — leaving them isn't inert
            # (round-2 finding on PR #121): _row_cost_basis() prefers
            # shares*avg_cost over current_value whenever both are
            # non-null, so a stray pair would surface a wrong number in
            # the upload-preview broker cost-basis subtotal even though
            # compute_portfolio()'s report valuation stays correct (it
            # never reads shares).
            elif row.get("current_value") is not None and (
                row.get("shares") is not None or row.get("avg_cost") is not None
            ):
                _append_issue(row, "cleared_residual_shares")
                row["shares"] = None
                row["avg_cost"] = None
            row["pricing_mode"] = "manual"
            # Listing venue, not custodian (issue #92). Override a model that
            # inferred A-Share from a mainland bank broker (CMB USD cash is
            # Other). Do not reclassify listed auto tickers into Other.
            if not row.get("ticker") and not row.get("fund_code"):
                row["market"] = "Other"

        # Force exchange suffix once market is determined (issue #92). A newly
        # applied .HK/.SS/.SZ then goes through the same HK-normalize +
        # suffix-currency correction as an already-suffixed ticker.
        apply_confirmed_exchange_suffix(row, emit_note=True)
        resolved_market, capture_ok = resolve_holding_market(
            ticker=row.get("ticker"),
            declared_market=row.get("market"),
            fund_code=row.get("fund_code"),
            asset_type=row.get("asset_type"),
            pricing_mode=row.get("pricing_mode") or "auto",
        )
        row["market"] = resolved_market
        # A row apply_confirmed_exchange_suffix left ambiguously-suffixed is
        # still a bare ticker as far as resolve_holding_market's own
        # ticker-based inference is concerned, so its "no suffix = US"
        # default would otherwise win here regardless of the real
        # (persisted) market — never capture-ready without a real suffix
        # (PR #310 round 6 review).
        if row.pop("_suffix_ambiguous", False):
            capture_ok = False
        row["capture_supported"] = capture_ok
        normalize_ticker_and_currency(row)

        # Unrecognized-currency check runs last (after suffix force-apply and
        # all corrections) so the note reflects the row's final currency.
        if row.get("currency") not in VALID_CURRENCIES:
            _append_issue(
                row,
                "unrecognized_currency",
                {"currency": str(row.get("currency"))},
                severity="warning",
            )

        # Coerce optional string fields: LLM occasionally emits [] instead of null.
        for str_field in ("notes", "account", "portfolio", "broker"):
            v = row.get(str_field)
            if isinstance(v, list):
                row[str_field] = " ".join(v) if v else None

        # Classify economic exposure (not the LLM's product-form asset_type).
        row["asset_class"] = _classify_asset_class(row)

        # Deduplicate: collapse only fully-identical rows (see comment above).
        if dedup:
            key = (
                row.get("ticker"),
                row.get("fund_code"),
                str(row.get("name", "")),
                str(row.get("broker") or ""),
                str(row.get("account") or ""),
                str(row.get("shares")),
                str(row.get("avg_cost")),
                str(row.get("current_value")),
            )
            if key in seen:
                continue
            seen.add(key)

        try:
            parsed = ParsedRow.model_validate(row)
        except ValidationError as exc:
            logging.getLogger(__name__).warning(
                "Dropping row that failed ParsedRow validation: %s (row=%r)", exc, row
            )
            if on_invalid_row is not None:
                on_invalid_row(row, str(exc))
            continue
        result.append(parsed)
    return result


def _row_cost_basis(row: ParsedRow) -> float | None:
    """Best-effort cost basis for a parsed row, in its own currency.

    shares*avg_cost when both are present, else the user-supplied current_value
    (manual/cash rows). None when neither is computable — the holding still
    counts toward holding_count but contributes nothing to the subtotal.
    """
    if row.shares is not None and row.avg_cost is not None:
        return row.shares * row.avg_cost
    return row.current_value


def _summarize(rows: list[ParsedRow]) -> list[BrokerGroup]:
    """Per-broker (Custodian) cross-check summary in upload order.

    Mirrors §1's grouping: brokers appear in first-seen order, broker-less rows
    fall under "Other". Cost basis is split by currency so a mixed-currency
    institution never sums incomparable figures. Deterministic and price-free.
    (issue #51)
    """
    # broker -> currency -> [cost_basis_sum, holding_count]
    groups: dict[str, dict[str, list[float]]] = {}
    counts: dict[str, int] = {}
    order: list[str] = []
    for row in rows:
        broker = (row.broker or "").strip() or "Other"
        if broker not in groups:
            groups[broker] = {}
            counts[broker] = 0
            order.append(broker)
        counts[broker] += 1
        basis = _row_cost_basis(row)
        if basis is None:
            continue
        bucket = groups[broker].setdefault(row.currency, [0.0, 0])
        bucket[0] += basis
        bucket[1] += 1

    result: list[BrokerGroup] = []
    for broker in order:
        subtotals = [
            CurrencySubtotal(currency=cur, cost_basis=total, holding_count=n)
            for cur, (total, n) in groups[broker].items()
        ]
        result.append(
            BrokerGroup(
                broker=broker,
                holding_count=counts[broker],
                subtotals=subtotals,
            )
        )
    return result


# Hardcoded to STRUCTURED_LLM_MODEL's current value (openai/gpt-5.6-luna),
# NOT a generic setting — issue #84. That model defaults reasoning to
# "medium", which is wasted cost/latency for mechanical structured
# extraction (there is nothing to reason through: the schema and inference
# rules are fully spelled out in _SYSTEM_PROMPT). This assumes
# STRUCTURED_LLM_MODEL supports a "none" reasoning-effort tier
# (openai/gpt-5.6-luna's `reasoning.supported_efforts` includes it, per
# OpenRouter's /api/v1/models). Changing STRUCTURED_LLM_MODEL to a model
# without one requires either dropping this or replacing it with that
# model's equivalent (e.g. some models only support `reasoning.enabled`,
# not graduated effort levels — see LOW_COST_LLM_MODEL's disable_reasoning
# in report_generator.py for that shape).
_STRUCTURED_REASONING_EFFORT = "none"


def _parse_attempt(
    client: openai.OpenAI, model: str, provider: dict[str, object] | None, text: str
) -> str:
    extra_body: dict[str, object] = {"reasoning": {"effort": _STRUCTURED_REASONING_EFFORT}}
    if provider is not None:
        extra_body["provider"] = provider
    response = client.chat.completions.create(
        model=model,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        temperature=0,
        extra_body=extra_body,
    )
    # Same malformed-200 guard report_llm._call_llm has carried since I-DEBT-2
    # — OpenRouter has been observed returning a 200 with choices=None. Indexing
    # it raised a bare TypeError/IndexError here, which (not being an
    # openai.OpenAIError) escaped parse()'s retry loop entirely and failed the
    # upload on what the taxonomy classifies as a retryable provider fault (#55).
    if not response.choices:
        raise LLMEmptyResponseError(
            f"model={model} resp_model={getattr(response, 'model', '?')} returned no choices"
        )
    content = response.choices[0].message.content
    # An empty body is not "an empty portfolio" — before #55 this coerced to
    # "{}" and surfaced as a successful upload with zero rows parsed, instead
    # of a retryable failure.
    if not content or not content.strip():
        raise LLMEmptyResponseError(f"model={model} returned an empty message body")
    return content


# Bounds each attempt so parse() reliably finishes in tens of seconds, not
# minutes (issue #77: a synchronous /holdings/upload request observed taking
# ~5 minutes, with the client's connection dropping before it ever saw the
# 200 the backend eventually returned). Also drives max_retries=0 on the
# client below — the openai SDK's own default (max_retries=2, read
# timeout=600s) would otherwise retry each attempt internally with its own
# backoff, stacking on top of parse()'s own 2-attempt loop (issue #84) and
# multiplying worst-case latency unpredictably. parse()'s loop already owns
# retry behavior, so the SDK doesn't need to also retry.
_PARSE_ATTEMPT_TIMEOUT_SECONDS = 20.0

# Two identical attempts (issue #84). Deliberately NOT paired with any backoff
# sleep, unlike report_llm._call_llm: this runs inside an interactive upload
# under holdings_tasks' 45s SLA, and 2 x 20s already spends most of it — see
# llm_errors.py's module docstring on why the taxonomy is shared but the two
# retry loops are not.
_PARSE_MAX_ATTEMPTS = 2


def parse(text: str) -> UploadPreview:
    """Call LLM to parse free-form holdings text into a structured preview.

    Structured (JSON) extraction routes uniformly to STRUCTURED_LLM_MODEL
    (issue #78). Two identical attempts (issue #84: STRUCTURED_LLM_MODEL no
    longer pins a precision tier to escalate away from — see
    app/core/llm.py:structured_provider — so a plain retry-on-transient-
    error is the whole story; open provider selection already applies from
    the first attempt). Each attempt is bounded to
    _PARSE_ATTEMPT_TIMEOUT_SECONDS and timed (issue #77) so a slow/hung
    provider is visible in logs and can't stall the whole call for minutes.

    A file that is already in the export dialect (tagged trailing fields on
    every data line) is parsed deterministically and never calls the LLM
    (PR #310). Invalid dialect rows surface as issue_rows; identical lots
    are kept (dedup skipped). Non-cash manual rows round-trip shares /
    avg_cost / current_value; cash/wmf round-trip current_value only.
    """
    dialect_rows = try_parse_dialect(text)
    if dialect_rows is not None:
        dialect_rejected: list[IssueRow] = []
        valid_rows = _postprocess(
            dialect_rows,
            on_invalid_row=lambda row, reason: dialect_rejected.append(
                IssueRow(
                    raw=str(row.get("_source_line") or json.dumps(row, default=str)),
                    reason=f"Rejected during validation: {reason}",
                )
            ),
            dedup=False,
        )
        return UploadPreview(
            valid_rows=valid_rows,
            issue_rows=dialect_rejected,
            broker_groups=_summarize(valid_rows),
        )

    settings = get_settings()
    client = openai.OpenAI(
        api_key=settings.OPENROUTER_API_KEY.get_secret_value(),
        base_url=settings.OPENROUTER_BASE_URL,
        default_headers=OR_ATTRIBUTION_HEADERS,
        timeout=_PARSE_ATTEMPT_TIMEOUT_SECONDS,
        max_retries=0,
    )
    logger = logging.getLogger(__name__)
    model = settings.STRUCTURED_LLM_MODEL
    provider = structured_provider()

    payload: dict[str, Any] | None = None
    last_exc: Exception | None = None
    for i in range(1, _PARSE_MAX_ATTEMPTS + 1):
        started = time.monotonic()
        try:
            content = _parse_attempt(client, model, provider, text)
            raw_payload = json.loads(_strip_code_fence(content))
            # A model that returns `[]`, `"..."`, or `null` did not honour the
            # requested object shape — the same "wrong shape" miss as a
            # decode error, and JSON-legal enough that json.loads alone
            # can't catch it. Left unchecked, `null` slips past the loop as
            # a false "success" (payload=None is later misread as "every
            # attempt failed"), and a list/string dies on `.get()` as an
            # AttributeError that `holdings_tasks` records via its
            # unexpected-Exception path instead of the RuntimeError
            # parse-failure path (PR #161 review finding).
            if not isinstance(raw_payload, dict):
                raise LLMInvalidJSONError(
                    f"model={model} returned valid JSON of the wrong shape "
                    f"({type(raw_payload).__name__}, expected an object)"
                )
            payload = raw_payload
            logger.info(
                "holding_parser: attempt %d/%d model=%s succeeded in %.1fs",
                i,
                _PARSE_MAX_ATTEMPTS,
                model,
                time.monotonic() - started,
            )
            break
        except (openai.OpenAIError, json.JSONDecodeError, LLMCallError) as exc:
            # Narrowed to exactly what this try block can raise (PR #161
            # review, round 2): the SDK call, json.loads, and the isinstance
            # guard above. A blanket `except Exception` here would also
            # launder Celery's SoftTimeLimitExceeded (this task runs under a
            # soft_time_limit, holdings_tasks.py) and genuine programming
            # bugs into a classified, retryable-looking RuntimeError —
            # routing them through holdings_tasks' normal parse-failure path
            # (a `logger.warning`) instead of its unexpected-exception path
            # (`logger.exception`, full traceback), which is the outer
            # handler this loop is not meant to preempt.
            code = classify(exc)
            last_exc = exc
            if not is_retryable(exc):
                # A bad key or a malformed request reproduces identically on
                # the next attempt — before #55 the blanket `except
                # openai.OpenAIError` retried these too, burning one of only
                # two attempts (and up to 20s of a 45s SLA) to reach the same
                # failure with the real cause buried a level deeper.
                logger.warning(
                    "holding_parser: attempt %d/%d model=%s failed after %.1fs "
                    "with code=%s (not retryable), giving up: %s",
                    i,
                    _PARSE_MAX_ATTEMPTS,
                    model,
                    time.monotonic() - started,
                    code,
                    exc,
                )
                break
            if i < _PARSE_MAX_ATTEMPTS:
                logger.warning(
                    "holding_parser: attempt %d/%d model=%s failed after %.1fs "
                    "with code=%s (retryable), trying next: %s",
                    i,
                    _PARSE_MAX_ATTEMPTS,
                    model,
                    time.monotonic() - started,
                    code,
                    exc,
                )
            else:
                # This was the last attempt — "trying next" would be a lie;
                # the loop exits and payload stays None (PR #161 review nit).
                logger.warning(
                    "holding_parser: attempt %d/%d model=%s failed after %.1fs "
                    "with code=%s (retryable), retry budget exhausted: %s",
                    i,
                    _PARSE_MAX_ATTEMPTS,
                    model,
                    time.monotonic() - started,
                    code,
                    exc,
                )
    if payload is None:
        code = classify(last_exc) if last_exc is not None else LLMErrorCode.UNKNOWN
        raise RuntimeError(f"LLM call failed ({code}): {last_exc}") from last_exc

    rejected_rows: list[IssueRow] = []
    valid_rows = _postprocess(
        payload.get("valid_rows") or [],
        on_invalid_row=lambda row, reason: rejected_rows.append(
            IssueRow(
                raw=json.dumps(row, default=str), reason=f"Rejected during validation: {reason}"
            )
        ),
    )
    issue_rows = [
        IssueRow.model_validate(r) for r in (payload.get("issue_rows") or [])
    ] + rejected_rows
    return UploadPreview(
        valid_rows=valid_rows,
        issue_rows=issue_rows,
        broker_groups=_summarize(valid_rows),
        unsupported_capture_count=sum(1 for r in valid_rows if not r.capture_supported),
    )
