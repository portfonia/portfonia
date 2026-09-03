# ruff: noqa: RUF001
"""Portfolio snapshot export: xlsx (openpyxl) and md (issue #331).

Separate from `holdings_export.py`: that module renders the *declared,
unpriced* fields in a re-importable `#####` dialect (issue #92/#310); this
module renders the *computed, priced* results from `compute_portfolio()`
for read-only consumption (Excel or an LLM prompt) — different semantics,
different format, not a shared renderer. `by_*` aggregates are page-view
stats and are deliberately never read here (issue #331 scope).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from io import BytesIO
from typing import Literal

from openpyxl import Workbook

from app.services.portfolio_calculator import HoldingValue, PortfolioSnapshot

ExportFormat = Literal["xlsx", "md"]

# Column order is the issue #331 design contract, identical across both
# formats — keep in sync with the header dicts below. Deliberately omits
# `sector` (dropped from this dashboard's vocabulary per issue #330) and
# `notes`/`holding_id` (internal, not analysis-relevant).
EXPORT_COLUMNS: tuple[str, ...] = (
    "ticker",
    "fund_code",
    "name",
    "market",
    "broker",
    "account",
    "portfolio",
    "asset_class",
    "currency",
    "shares",
    "avg_cost",
    "market_value",
    "market_value_base",
    "cost_basis_base",
    "unrealized_pnl_base",
    "unrealized_pnl_pct",
    "pricing_mode",
    "capture_supported",
)

_HEADERS_EN: dict[str, str] = {
    "ticker": "Ticker",
    "fund_code": "Fund Code",
    "name": "Name",
    "market": "Market",
    "broker": "Custodian",
    "account": "Account",
    "portfolio": "Group",
    "asset_class": "Asset Class",
    "currency": "Currency",
    "shares": "Shares",
    "avg_cost": "Avg Cost",
    "market_value": "Market Value",
    "market_value_base": "Market Value (Base)",
    "cost_basis_base": "Cost Basis (Base)",
    "unrealized_pnl_base": "Unrealized P&L (Base)",
    "unrealized_pnl_pct": "Unrealized P&L %",
    "pricing_mode": "Pricing Mode",
    "capture_supported": "Live Price Available",
}

_HEADERS_ZH: dict[str, str] = {
    "ticker": "代码",
    "fund_code": "基金代码",
    "name": "名称",
    "market": "市场",
    "broker": "托管机构",
    "account": "账户",
    "portfolio": "分组",
    "asset_class": "资产类别",
    "currency": "币种",
    "shares": "份额",
    "avg_cost": "平均成本",
    "market_value": "市值（原币种）",
    "market_value_base": "市值（本位币）",
    "cost_basis_base": "成本（本位币）",
    "unrealized_pnl_base": "浮动盈亏（本位币）",
    "unrealized_pnl_pct": "浮动盈亏率",
    "pricing_mode": "定价方式",
    "capture_supported": "是否可实时取价",
}

# Locale-keyed dispatch (issue #319 pattern, same shape as
# holdings_export.py's _RULES_BY_LOCALE) — a locale not present here
# (including a future zh-Hant) falls back to "en".
_HEADERS_BY_LOCALE: dict[str, dict[str, str]] = {"en": _HEADERS_EN, "zh": _HEADERS_ZH}
_AS_OF_LABEL_BY_LOCALE: dict[str, str] = {"en": "As of", "zh": "截至"}
_BASE_CURRENCY_LABEL_BY_LOCALE: dict[str, str] = {"en": "Base currency", "zh": "本位币"}


def _headers(locale: str) -> dict[str, str]:
    return _HEADERS_BY_LOCALE.get(locale, _HEADERS_BY_LOCALE["en"])


def _meta_labels(locale: str) -> tuple[str, str]:
    return (
        _AS_OF_LABEL_BY_LOCALE.get(locale, _AS_OF_LABEL_BY_LOCALE["en"]),
        _BASE_CURRENCY_LABEL_BY_LOCALE.get(locale, _BASE_CURRENCY_LABEL_BY_LOCALE["en"]),
    )


# compute_portfolio()'s _ratio() (portfolio_calculator.py) stores this field
# as a 0..1 fraction, not a percent — every other consumer (the /portfolio
# table's formatPercent, the overview email's `:.1%`) multiplies by 100
# before display. Scale here too, or the exported figure reads 100x too
# small under a "%"-labeled column (PR #335 review 5103601953).
_PERCENT_SCALE_COLUMNS = frozenset({"unrealized_pnl_pct"})


def _row_values(holding: HoldingValue) -> list[object]:
    values: list[object] = []
    for column in EXPORT_COLUMNS:
        value = getattr(holding, column)
        if column in _PERCENT_SCALE_COLUMNS and isinstance(value, Decimal):
            value = value * 100
        values.append(value)
    return values


def _fmt_decimal(value: Decimal) -> str:
    s = format(value, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


def _fmt_cell_md(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, Decimal):
        text = _fmt_decimal(value)
    elif isinstance(value, bool):
        text = "true" if value else "false"
    else:
        text = str(value)
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def render_portfolio_export_md(snapshot: PortfolioSnapshot, locale: str) -> str:
    as_of_label, base_currency_label = _meta_labels(locale)
    as_of = snapshot.price_as_of_date.isoformat() if snapshot.price_as_of_date else ""
    headers = _headers(locale)
    lines = [
        f"{as_of_label}: {as_of}",
        f"{base_currency_label}: {snapshot.base_currency}",
        "",
        "| " + " | ".join(headers[c] for c in EXPORT_COLUMNS) + " |",
        "| " + " | ".join("---" for _ in EXPORT_COLUMNS) + " |",
    ]
    for holding in snapshot.holdings:
        lines.append("| " + " | ".join(_fmt_cell_md(v) for v in _row_values(holding)) + " |")
    return "\n".join(lines) + "\n"


def _fmt_cell_xlsx(value: object) -> object:
    if isinstance(value, Decimal):
        return float(value)
    return value


def render_portfolio_export_xlsx(snapshot: PortfolioSnapshot, locale: str) -> bytes:
    as_of_label, base_currency_label = _meta_labels(locale)
    as_of = snapshot.price_as_of_date.isoformat() if snapshot.price_as_of_date else ""
    headers = _headers(locale)

    wb = Workbook()
    ws = wb.active
    assert ws is not None  # Workbook() always creates one default sheet
    ws.title = "Portfolio"
    ws.append([as_of_label, as_of])
    ws.append([base_currency_label, snapshot.base_currency])
    ws.append([])
    ws.append([headers[c] for c in EXPORT_COLUMNS])
    for holding in snapshot.holdings:
        ws.append([_fmt_cell_xlsx(v) for v in _row_values(holding)])

    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def portfolio_export_filename(fmt: ExportFormat, now: datetime | None = None) -> str:
    """UTC filename, e.g. portfolio-20260902-051530Z.md / .xlsx."""
    when = now if now is not None else datetime.now(tz=UTC)
    stamp = when.astimezone(UTC).strftime("%Y%m%d-%H%M%SZ")
    return f"portfolio-{stamp}.{fmt}"
