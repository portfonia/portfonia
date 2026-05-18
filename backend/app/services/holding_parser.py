"""Parse free-form holdings text (CSV / Markdown / plain text) via LLM."""

import io
import json
from pathlib import Path

import openai

from app.core.config import get_settings
from app.schemas.holdings import IssueRow, ParsedRow, UploadPreview

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
- Cannot determine → null

--- quality bar for valid_rows ---
A row is valid if it has at minimum: name + currency + (shares OR current_value).
A row is an issue_row if: name cannot be determined, OR both shares/current_value
are missing, OR the format is completely unintelligible.

Do not hallucinate tickers or fund codes — if uncertain, leave the field null and
note it in issues rather than guessing.
"""


def _extract_text(file_bytes: bytes, filename: str) -> str:
    """Convert uploaded file bytes to plain text for LLM ingestion."""
    suffix = Path(filename).suffix.lower()
    if suffix not in _SUPPORTED_EXTENSIONS:
        raise ValueError(
            f"Unsupported file type '{suffix}'. Please upload .md, .txt, .csv, .xlsx, or .xls."
        )
    if suffix in (".md", ".txt", ".csv"):
        return file_bytes.decode("utf-8")
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
    df = xf.parse(xf.sheet_names[0])
    return str(df.to_csv(index=False))


_TICKER_CURRENCY_MAP = {
    ".hk": "HKD",
    ".ss": "CNY",
    ".sz": "CNY",
    ".l": "GBP",
    ".ax": "AUD",
    ".to": "CAD",
}


def _postprocess(raw_rows: list[dict]) -> list[ParsedRow]:  # type: ignore[type-arg]
    """Apply deterministic post-processing on top of LLM output."""
    result: list[ParsedRow] = []
    for row in raw_rows:
        ticker: str | None = row.get("ticker")
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
        result.append(ParsedRow.model_validate(row))
    return result


def parse(text: str) -> UploadPreview:
    """Call LLM to parse free-form holdings text into a structured preview."""
    settings = get_settings()
    client = openai.OpenAI(
        api_key=settings.OPENROUTER_API_KEY.get_secret_value(),
        base_url=settings.OPENROUTER_BASE_URL,
    )

    try:
        response = client.chat.completions.create(
            model=settings.LOW_COST_LLM_MODEL,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
            temperature=0,
        )
    except openai.OpenAIError as exc:
        raise RuntimeError(f"LLM call failed: {exc}") from exc

    content = response.choices[0].message.content or "{}"
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"LLM returned invalid JSON: {exc}") from exc

    valid_rows = _postprocess(payload.get("valid_rows") or [])
    issue_rows = [IssueRow.model_validate(r) for r in (payload.get("issue_rows") or [])]
    return UploadPreview(valid_rows=valid_rows, issue_rows=issue_rows)
