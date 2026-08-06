"""Parse free-form holdings text (CSV / Markdown / plain text) via LLM."""

import io
import json
import logging
import re
import time
from pathlib import Path
from typing import Any

import openai

from app.core.config import OR_ATTRIBUTION_HEADERS, get_settings
from app.core.llm import structured_provider
from app.schemas.holdings import (
    BrokerGroup,
    CurrencySubtotal,
    IssueRow,
    ParsedRow,
    UploadPreview,
)

_SUPPORTED_EXTENSIONS = {".md", ".txt", ".csv", ".xlsx", ".xls"}

_SYSTEM_PROMPT = """\
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
   (招商, 建设, 工商, 农业, 中信, 兴业, 浦发, 民生, 光大, 平安,
    支付宝, 微信, 天天基金, 蚂蚁, 余额宝, 招财宝, 零钱通,
    招行, 建行, 工行, 农行) → CNY
7. ticker is pure ASCII letters with no suffix AND no other CNY/HKD signals → USD
8. asset is cash/现金/保证金/margin/deposit: infer from broker context;
   if broker is foreign (IBKR, Schwab, Fidelity, TD, Futu USD account) → USD;
   if broker is mainland Chinese → CNY;
   if broker is Hong Kong platform (富途/Futu HKD, 港股通) → HKD;
   if cannot determine → add note to issues, set confidence < 0.7.
9. Otherwise: make best guess and add explanation to issues.

--- asset_type inference ---
- Has ticker with exchange suffix (.HK, .SS, .SZ) or well-known US ticker → "stock"
- Name contains ETF/指数/index fund keywords or ticker starts with common ETF pattern → "etf"
- Has a 6-digit fund_code → "fund"
- Name contains 现金/cash/存款/货币/保证金/margin/deposit → "cash"
- Bank-sold WMP (理财产品/财富管理/结构性存款/代销) → "wmf"
- Cannot determine → unknown (omit the key — see output compactness below)

--- market inference (the user groups capital by market; preserve their intent) ---
1. The user explicitly gives a market/exchange column (US, HK, A-Share/A股/沪深,
   美股, 港股, etc.) → map it to one of US / HK / A-Share / Other and use it.
2. ticker ends in .HK → HK
3. ticker ends in .SS or .SZ, OR a 6-digit fund_code, OR a 6-digit A-share code → A-Share
4. plain US-listed ticker (no suffix) → US
5. cash / WMP / deposit: follow the ACCOUNT's market via broker context —
   IBKR / Schwab / Fidelity / TD / Futu-USD → US; 港股通 / Futu-HKD → HK;
   mainland bank / 支付宝 / 微信 / 余额宝 / 天天基金 → A-Share.
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
"""


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
        # Chinese brokers (天天基金, 招商, etc.) commonly export GBK/GB2312, not
        # UTF-8. Try UTF-8 first, then fall back to gb18030 (a superset of both)
        # so a legitimately-encoded CN export doesn't 500 on decode.
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


_ALLOWED_ASSET_TYPES = {"stock", "etf", "fund", "cash", "wmf", "other"}

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
    "019547": "EQUITY_US_TECH",  # 招商纳斯达克100指数基金
    # Developed markets ex-US
    "EWJ": "EQUITY_DM",
    # China equity (A-share / HK Chinese / China-focused QDII)
    "FXI": "EQUITY_CN",
    "KWEB": "EQUITY_CN",
    "110011": "EQUITY_CN",  # 易方达优质精选混合(QDII) — China concept
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
    "008142": "PRECIOUS_METALS",  # 工银黄金ETF联接
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


# Map common free-text market labels the model may emit onto the canonical bucket.
_MARKET_ALIASES = {
    "us": "US",
    "usa": "US",
    "美股": "US",
    "美国": "US",
    "hk": "HK",
    "hkex": "HK",
    "港股": "HK",
    "香港": "HK",
    "a-share": "A-Share",
    "a股": "A-Share",
    "ashare": "A-Share",
    "沪深": "A-Share",
    "cn": "A-Share",
    "china": "A-Share",
}


def _postprocess(raw_rows: list[dict[str, Any]]) -> list[ParsedRow]:
    """Apply deterministic post-processing on top of LLM output."""
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
        if at is not None and at not in _ALLOWED_ASSET_TYPES:
            row["issues"] = list(row.get("issues") or [])
            row["issues"].append(f"Unrecognized asset_type {at!r} dropped to null")
            row["asset_type"] = None

        # Normalize market to the canonical bucket; an unmappable non-null value
        # becomes "Other" rather than tripping the Literal or being lost.
        mkt = row.get("market")
        if mkt is not None and mkt not in {"US", "HK", "A-Share", "Other"}:
            row["market"] = _MARKET_ALIASES.get(str(mkt).strip().lower(), "Other")

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

        result.append(ParsedRow.model_validate(row))
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
    """Per-broker (持仓机构) cross-check summary in upload order.

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
    return response.choices[0].message.content or "{}"


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
    model = settings.STRUCTURED_LLM_MODEL
    provider = structured_provider()
    attempts = [(model, provider), (model, provider)]

    content: str | None = None
    last_exc: openai.OpenAIError | None = None
    for i, (attempt_model, provider) in enumerate(attempts, start=1):
        started = time.monotonic()
        try:
            content = _parse_attempt(client, attempt_model, provider, text)
            logging.getLogger(__name__).info(
                "holding_parser: attempt %d/%d model=%s succeeded in %.1fs",
                i,
                len(attempts),
                attempt_model,
                time.monotonic() - started,
            )
            break
        except openai.OpenAIError as exc:
            last_exc = exc
            logging.getLogger(__name__).warning(
                "holding_parser: attempt %d/%d model=%s failed after %.1fs (%s), trying next",
                i,
                len(attempts),
                attempt_model,
                time.monotonic() - started,
                exc,
            )
    if content is None:
        raise RuntimeError(f"LLM call failed: {last_exc}") from last_exc
    try:
        payload = json.loads(_strip_code_fence(content))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"LLM returned invalid JSON: {exc}") from exc

    valid_rows = _postprocess(payload.get("valid_rows") or [])
    issue_rows = [IssueRow.model_validate(r) for r in (payload.get("issue_rows") or [])]
    return UploadPreview(
        valid_rows=valid_rows,
        issue_rows=issue_rows,
        broker_groups=_summarize(valid_rows),
    )
