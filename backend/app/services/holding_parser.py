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
    VALID_ASSET_TYPES,
    VALID_CURRENCIES,
    BrokerGroup,
    CurrencySubtotal,
    IssueRow,
    ParsedRow,
    UploadPreview,
)
from app.services.llm_errors import (
    LLMEmptyResponseError,
    LLMErrorCode,
    LLMInvalidJSONError,
    classify,
    is_retryable,
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
  "market":        "US" | "HK" | "A-Share" | "Other" | null  (capital-location bucket; see rules),
  "broker":        string | null,
  "account":       string | null,
  "portfolio":     string | null,
  "notes":         string | null,
  "issues":        [string]  (list of inference notes or low-confidence warnings),
  "confidence":    number  (0.0-1.0; < 0.7 means the field values are uncertain)
}

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
note the ambiguity in issues.

--- currency inference (apply in priority order) ---
1. User explicitly states a currency code (USD, HKD, CNY, GBP, …) → use it.
2. ticker ends in .HK → HKD
3. ticker ends in .SS or .SZ → CNY
4. ticker ends in .L → GBP
5. identifier is all digits (6-digit fund code or A-share code) → CNY
6. name or broker contains a mainland Chinese institution
   ($cny_institutions) → CNY
7. ticker is pure ASCII letters with no suffix AND no other CNY/HKD signals → USD
8. asset is cash/$cash/$margin/margin/deposit: infer from broker context;
   if broker is foreign (IBKR, Schwab, Fidelity, TD, Futu USD account) → USD;
   if broker is mainland Chinese → CNY;
   if broker is Hong Kong platform ($futu/Futu HKD, $stock_connect) → HKD;
   if cannot determine → add note to issues, set confidence < 0.7.
9. Otherwise: make best guess and add explanation to issues.

--- asset_type inference ---
- Has ticker with exchange suffix (.HK, .SS, .SZ) or well-known US ticker → "stock"
- Name contains ETF/$index_fund/index fund keywords or ticker starts with common ETF pattern → "etf"
- Has a 6-digit fund_code → "fund"
- Name contains $cash/cash/$deposit/$money_market/$margin/margin/deposit → "cash"
- Bank-sold WMP ($wmp_terms) → "wmf"
- Cannot determine → unknown (omit the key — see output compactness below)

--- market inference (the user groups capital by market; preserve their intent) ---
1. The user explicitly gives a market/exchange column (US, HK, A-Share/$a_share_terms,
   $us_market_zh, $hk_market_zh, etc.) → map it to one of US / HK / A-Share / Other and use it.
2. ticker ends in .HK → HK
3. ticker ends in .SS or .SZ, OR a 6-digit fund_code, OR a 6-digit A-share code → A-Share
4. plain US-listed ticker (no suffix) → US
5. cash / WMP / deposit: follow the ACCOUNT's market via broker context —
   IBKR / Schwab / Fidelity / TD / Futu-USD → US; $stock_connect / Futu-HKD → HK;
   mainland bank / $common_cn_platforms → A-Share.
6. cannot determine → Other.

--- quality bar for valid_rows ---
A row is valid if it has at minimum: name + currency + (shares OR current_value).
A row is an issue_row if: name cannot be determined, OR both shares/current_value
are missing, OR the format is completely unintelligible.

Do not hallucinate tickers or fund codes — if uncertain, treat the field as
unknown (see output compactness below for how an unknown field is rendered)
and note it in issues rather than guessing.

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
    ".ax": "AUD",
    ".to": "CAD",
}


_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*\n(.*?)\n```\s*$", re.DOTALL | re.IGNORECASE)


def _strip_code_fence(content: str) -> str:
    """Unwrap a markdown ```json ... ``` fence if present.

    Anthropic models on OpenRouter ignore ``response_format=json_object`` and
    wrap JSON in markdown fences. Strip them before parsing. Returns the inner
    payload, or the original string trimmed when no fence is found.
    """
    match = _FENCE_RE.match(content)
    return match.group(1) if match else content.strip()


# Ticker → canonical asset_class (economic exposure, not product form).
# Covers known holdings; new tickers default via asset_type fallback below.
_TICKER_ASSET_CLASS: dict[str, str] = {
    # US broad market (S&P 500 / total market) — classified by underlying exposure,
    # not listing location (513650.SS is an A-share ETF tracking S&P 500).
    "VOO": "EQUITY_US_BROAD",
    "VTI": "EQUITY_US_BROAD",
    "SPY": "EQUITY_US_BROAD",
    "IVV": "EQUITY_US_BROAD",
    "513650": "EQUITY_US_BROAD",
    "513650.SS": "EQUITY_US_BROAD",
    # US tech / Nasdaq 100
    "QQQM": "EQUITY_US_TECH",
    "QQQ": "EQUITY_US_TECH",
    "019547": "EQUITY_US_TECH",  # China Merchants Nasdaq 100 Index Fund
    # Developed markets ex-US
    "EWJ": "EQUITY_DM",
    # China equity (A-share / HK Chinese / China-focused QDII)
    "FXI": "EQUITY_CN",
    "KWEB": "EQUITY_CN",
    "110011": "EQUITY_CN",  # E Fund Premium Select mixed fund (QDII) — China concept
    # Precious metals (gold) — split from the generic COMMODITY catch-all
    # 2026-06-20: gold's volatility/concentration profile is distinct from
    # energy and other commodities, see config/asset_class_thresholds.yml.
    "SGOL": "PRECIOUS_METALS",
    "GLD": "PRECIOUS_METALS",
    "IAU": "PRECIOUS_METALS",
    "518660": "PRECIOUS_METALS",
    "518660.SS": "PRECIOUS_METALS",
    "518800": "PRECIOUS_METALS",
    "518800.SS": "PRECIOUS_METALS",
    "008142": "PRECIOUS_METALS",  # ICBC Gold ETF feeder fund
    # Bond / T-bill funds
    "BOXX": "BOND_FUND",
    "BIL": "BOND_FUND",
    "SHY": "BOND_FUND",
    "AGG": "BOND_FUND",
    "TLT": "BOND_FUND",
}

_ASSET_TYPE_CLASS: dict[str, str] = {
    "stock": "STOCK",
    "etf": "EQUITY_BROAD",  # unknown ETF: global catch-all; ticker lookup overrides
    "fund": "EQUITY_BROAD",  # unknown fund: global catch-all; ticker/fund_code lookup overrides
    "cash": "CASH_EQUIV",
    "wmf": "CASH_EQUIV",
    "other": "STOCK",
}


def _classify_asset_class(row: dict[str, Any]) -> str:
    ticker = (row.get("ticker") or "").upper()
    if ticker and ticker in _TICKER_ASSET_CLASS:
        return _TICKER_ASSET_CLASS[ticker]
    fund_code = row.get("fund_code") or ""
    if fund_code and fund_code in _TICKER_ASSET_CLASS:
        return _TICKER_ASSET_CLASS[fund_code]
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
    **_VOCAB.market_aliases_zh,
}


def _postprocess(
    raw_rows: list[dict[str, Any]],
    on_invalid_row: Callable[[dict[str, Any], str], None] | None = None,
) -> list[ParsedRow]:
    """Apply deterministic post-processing on top of LLM output.

    `on_invalid_row`, if given, is invoked for any row that still fails
    ParsedRow validation after normalization (e.g. a currency the LLM
    hallucinated that isn't in VALID_CURRENCIES) — the row is dropped from
    the returned list rather than raising, so one bad row can't fail the
    whole upload (issue #25/PR #114 review: currency validation used to
    propagate a bare ValidationError out of parse(), killing every other
    valid row in the same file).
    """
    result: list[ParsedRow] = []
    # Dedup only collapses byte-identical rows (an LLM emitting the same holding
    # twice). The key includes broker/account/quantity so two genuinely distinct
    # lots — e.g. the same ETF at two brokers — are both preserved. (issue #50)
    seen: set[tuple[str | None, ...]] = set()

    for row in raw_rows:
        # Normalize asset_type to the known set BEFORE validation so an off-list
        # value from the model is coerced to null (with a note) rather than
        # either crashing a strict Literal or silently persisting garbage.
        at = row.get("asset_type")
        if at is not None and at not in VALID_ASSET_TYPES:
            row["issues"] = list(row.get("issues") or [])
            row["issues"].append(f"Unrecognized asset_type {at!r} dropped to null")
            row["asset_type"] = None

        # Normalize market to the canonical bucket; an unmappable non-null value
        # becomes "Other" rather than tripping the Literal or being lost.
        mkt = row.get("market")
        if mkt is not None and mkt not in {"US", "HK", "A-Share", "Other"}:
            row["market"] = _MARKET_ALIASES.get(str(mkt).strip().lower(), "Other")

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
                row["issues"] = list(row.get("issues") or [])
                row["issues"].append(f"Currency normalized to {normalized_cur!r}")
                row["currency"] = normalized_cur

        # Canonicalize HK tickers to yfinance's 4-digit form (02333.HK -> 2333.HK)
        # so price lookups don't miss on a leading-zero variant. (issue #49)
        ticker: str | None = row.get("ticker")
        if ticker:
            normalized = _normalize_hk_ticker(ticker)
            if normalized != ticker:
                row["issues"] = list(row.get("issues") or [])
                row["issues"].append(f"Ticker normalized to {normalized} for price lookup")
                row["ticker"] = ticker = normalized

        # Currency correction from ticker suffix.
        if ticker:
            for suffix, currency in _TICKER_CURRENCY_MAP.items():
                if ticker.lower().endswith(suffix):
                    if row.get("currency") != currency:
                        row["issues"] = list(row.get("issues") or [])
                        row["issues"].append(
                            f"Currency corrected to {currency} based on ticker suffix {suffix.upper()}"
                        )
                        row["currency"] = currency
                    break

        # Unrecognized-currency check runs last (after all corrections above
        # had a chance to fix the value) so the note reflects the row's
        # final currency, not an intermediate one.
        if row.get("currency") not in VALID_CURRENCIES:
            row["issues"] = list(row.get("issues") or [])
            row["issues"].append(f"Unrecognized currency {row.get('currency')!r}")

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
                row["issues"] = list(row.get("issues") or [])
                row["issues"].append(
                    f"Dropped spurious ticker/fund_code {bogus_id!r} on cash/wmf row "
                    "(cash/wmf products carry no real instrument identifier)"
                )
                row["ticker"] = None
                row["fund_code"] = None
            # Only moves the amount from shares when current_value is still
            # None — a row where the model populated BOTH fields (e.g. a
            # bogus current_value alongside the real balance in shares)
            # keeps current_value as originally given rather than guessing
            # which of two conflicting numbers is real.
            if row.get("current_value") is None and row.get("shares") is not None:
                row["issues"] = list(row.get("issues") or [])
                row["issues"].append("Cash/wmf amount moved from shares to current_value")
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
                row["issues"] = list(row.get("issues") or [])
                row["issues"].append(
                    "Cleared residual shares/avg_cost on cash/wmf row "
                    "(current_value is authoritative)"
                )
                row["shares"] = None
                row["avg_cost"] = None
            row["pricing_mode"] = "manual"

        # Coerce optional string fields: LLM occasionally emits [] instead of null.
        for str_field in ("notes", "account", "portfolio", "broker"):
            v = row.get(str_field)
            if isinstance(v, list):
                row[str_field] = " ".join(v) if v else None

        # Classify economic exposure (not the LLM's product-form asset_type).
        row["asset_class"] = _classify_asset_class(row)

        # Deduplicate: collapse only fully-identical rows (see comment above).
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
    """
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
        except Exception as exc:
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
    )
