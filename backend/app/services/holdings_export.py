# ruff: noqa: RUF001
"""Holdings export / template dialect (issue #92; #319 follow-up).

`#####` comment rules + one holding per line. Locale is caller-supplied
(issue #319 item 9): `GET /holdings/export`/`GET /holdings/template` pass
the frontend's current UI locale, not `users.locale` (report language) —
see the router.

Trailing tagged fields (`account:`, `portfolio:`, `notes:`, `asset_type:`,
`market:`, `pricing_mode:`) used to carry the columns the positional
dialect drops, and doubled as a marker `holding_parser.try_parse_dialect`
detected to skip the LLM entirely on a re-uploaded export (PR #310).
Issue #319 item 8 removes the tag segment from export/template output on
product-owner request, **explicitly accepting** that a re-uploaded
exported file no longer hits that fast path — it now parses like any
other free-form file, via the LLM, same result but no longer free/fast.
`DIALECT_TAG_KEYS` and `holding_parser.try_parse_dialect`/
`split_tagged_fields`/`parse_dialect_line` stay in place, unmodified and
still reachable by a hand-written tagged file — they just have no export
producing that input anymore, so `_render_tags` below is now unused by
`render_holding_line` and kept only for a user who wants to write the
tagged form by hand (see issue #319 investigation comment).

Cost basis is load-bearing: future return/yield depends on average holding
cost, and `price_snapshots` is market data, not what the user paid. Export
must therefore emit shares and avg_cost for non-cash rows whenever they
are present — dropping them on export then replace-all is unrecoverable.
Cash and wealth-management products have no cost basis today and still
emit `current_value` only. Manual-priced listed rows always emit all three
numeric slots (shares, avg_cost, current_value), using the placeholder
`MANUAL_LISTED_PLACEHOLDER` for a slot that is unset (`pricing_mode:manual`
means those three tokens after currency parse as shares / avg_cost /
current_value — see `holding_parser._manual_match_explicit`). A slot
silently omitted instead of placeholder-marked is unrecoverable: the
parser cannot tell "shares + current_value, no avg_cost" apart from
"shares + avg_cost, no current_value" by count alone, and round 4 of this
PR fabricated a cost basis that way (PR #310 round 5 review).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from decimal import Decimal

from app.models.holding import Holding

MANUAL_LISTED_PLACEHOLDER = "-"

DIALECT_TAG_KEYS: tuple[str, ...] = (
    "account",
    "portfolio",
    "notes",
    "asset_type",
    "market",
    "pricing_mode",
)

_RULES_EN = """\
##### Holdings template
#####
##### One holding per line. Order is flexible — the parser reads free-form text.
##### Listed assets (auto): name  ticker-or-fund-code  currency  shares  avg-cost  broker
##### Listed assets (pricing_mode:manual): same, plus current-value after avg-cost
##### Cash or wealth-management products (no public code): name  total-value  currency  broker
##### Optional trailing tags (quote a value if it contains spaces):
#####   account:"IRA" portfolio:Growth notes:"long-term" asset_type:stock market:US pricing_mode:auto
##### asset_type is stock / etf / fund / cash / wealth-management (cash-like) / other.
##### pricing_mode is auto or manual. market is listing venue (US / HK / A-Share / UK / Europe / Japan / Korea / Other).
#####
##### Ticker suffixes: .HK (Hong Kong), .SS / .SZ (A-shares), .L (London),
##### .AS / .PA / .DE (Europe), .T (Japan), .KS / .KQ (Korea). US listings need none.
##### Once Market is set or confidently derived, the matching suffix is stored.
##### Pershing Square is entered as PSH.L with market:UK. If Market cannot be
##### determined, no suffix is guessed — set Market if known.
##### Chinese public funds: enter the 6-digit fund code (e.g. 110011).
##### Cash and wealth-management products with no ticker are Other — not inferred
##### from the bank.
##### Lines starting with ##### are comments and will be ignored by the parser.
"""

_RULES_ZH = """\
##### 持仓模板
#####
##### 一行一条。顺序不限，解析器按自由文本读取。
##### 上市标的（自动定价）：名称  代码或基金代码  货币  份额  平均成本  券商
##### 上市标的（pricing_mode:manual）：同上，平均成本后再加当前市值
##### 现金或银行理财产品（无公开代码）：名称  总金额  货币  券商
##### 行尾可选标签（含空格的值请加双引号）：
#####   account:"IRA" portfolio:Growth notes:"长期" asset_type:stock market:US pricing_mode:auto
##### asset_type 为 stock / etf / fund / cash / 理财类 / other。
##### pricing_mode 为 auto 或 manual。market 是上市地（US / HK / A-Share / UK / Europe / Japan / Korea / Other）。
#####
##### 代码后缀：.HK（港股）、.SS / .SZ（A股）、.L（伦敦）、.AS / .PA / .DE（欧洲）、
##### .T（日本）、.KS / .KQ（韩国）。美股无需后缀。
##### 一旦确定上市地（用户填写或有把握推导），即写入对应后缀。
##### Pershing Square 请写 PSH.L 并标 market:UK。
##### 无法确定上市地时不猜后缀 — 若已知请设置 Market。
##### 中国公募基金请填写 6 位基金代码（例如 110011）。
##### 无代码的现金和理财为 Other，不根据银行券商推断为 A 股。
##### 以 ##### 开头的行是注释，解析时会被忽略。
"""

_EXAMPLES_EN = """\
##### --- examples (delete these lines and add your own) ---
##### Apple AAPL USD 100 228 IBKR asset_type:stock market:US pricing_mode:auto
##### SPDR S&P 500 ETF SPY USD 20 450 IBKR asset_type:etf market:US pricing_mode:auto
##### Tencent 0700.HK HKD 380 371.47 Futu asset_type:stock market:HK pricing_mode:auto
##### Kweichow Moutai 600519.SS CNY 10 1680 China Securities asset_type:stock market:A-Share pricing_mode:auto
##### E Fund Blue Chip 110011 CNY 40000 3.99 Alipay asset_type:fund market:A-Share pricing_mode:auto
##### USD Cash 50000 USD Schwab asset_type:cash market:Other pricing_mode:manual
##### Bank wealth-management product 100000 CNY CMB asset_type:wealth-management market:Other pricing_mode:manual
##### Pershing Square PSH.L GBP 50 55 IBKR asset_type:stock market:UK pricing_mode:auto
"""

_EXAMPLES_ZH = """\
##### --- 示例（删除这些行后填入你自己的持仓） ---
##### Apple AAPL USD 100 228 IBKR asset_type:stock market:US pricing_mode:auto
##### SPDR S&P 500 ETF SPY USD 20 450 IBKR asset_type:etf market:US pricing_mode:auto
##### 腾讯 0700.HK HKD 380 371.47 富途 asset_type:stock market:HK pricing_mode:auto
##### 贵州茅台 600519.SS CNY 10 1680 中信证券 asset_type:stock market:A-Share pricing_mode:auto
##### 易方达蓝筹精选 110011 CNY 40000 3.99 支付宝 asset_type:fund market:A-Share pricing_mode:auto
##### 美元现金 50000 USD Schwab asset_type:cash market:Other pricing_mode:manual
##### 银行理财产品 100000 CNY 招商银行 asset_type:wealth-management market:Other pricing_mode:manual
##### Pershing Square PSH.L GBP 50 55 IBKR asset_type:stock market:UK pricing_mode:auto
"""


def _flatten(value: object) -> str:
    if value is None:
        return ""
    return str(value).replace("\n", " ").replace("\r", " ").strip()


def _fmt_num(value: Decimal | None) -> str:
    if value is None:
        return ""
    s = format(value, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


def _quote_tag(value: str) -> str:
    if re.search(r'[\s":]', value) or value == "":
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


def _render_tags(holding: Holding) -> str:
    parts: list[str] = []
    for key in DIALECT_TAG_KEYS:
        raw = getattr(holding, key)
        if raw is None:
            continue
        flat = _flatten(raw)
        if not flat:
            continue
        if key == "asset_type" and flat == "wmf":
            # Template/export copy must not use the letters w-m-f as jargon.
            flat = "wealth-management"
        parts.append(f"{key}:{_quote_tag(flat)}")
    return " ".join(parts)


def render_holding_line(holding: Holding) -> str:
    """One template-dialect line for a persisted holding."""
    name = _flatten(holding.name)
    broker = _flatten(holding.broker)
    if holding.asset_type in ("cash", "wmf"):
        parts = [name, _fmt_num(holding.current_value), holding.currency or "", broker]
    elif holding.pricing_mode == "manual":
        # pricing_mode:manual listed: always emit all three numeric slots
        # (shares, avg_cost, current_value), using MANUAL_LISTED_PLACEHOLDER
        # for an unset one. A slot silently omitted instead of placeholder-
        # marked is unrecoverable: "shares + current_value, no avg_cost"
        # and "shares + avg_cost, no current_value" are both two numeric
        # tokens, indistinguishable by count alone (PR #310 round 5).
        ident = _flatten(holding.ticker or holding.fund_code)
        parts = [
            name,
            ident,
            holding.currency or "",
            _fmt_num(holding.shares) or MANUAL_LISTED_PLACEHOLDER,
            _fmt_num(holding.avg_cost) or MANUAL_LISTED_PLACEHOLDER,
            _fmt_num(holding.current_value) or MANUAL_LISTED_PLACEHOLDER,
            broker,
        ]
    else:
        ident = _flatten(holding.ticker or holding.fund_code)
        parts = [
            name,
            ident,
            holding.currency or "",
            _fmt_num(holding.shares),
            _fmt_num(holding.avg_cost),
            broker,
        ]
    return " ".join(p for p in parts if p)


# Locale-keyed dispatch (issue #319 item 9) replacing the old `locale == "zh"`
# ternary — a locale not present here (including a future zh-Hant) falls back
# to "en", same as the ternary's implicit else did.
_RULES_BY_LOCALE: dict[str, str] = {"en": _RULES_EN, "zh": _RULES_ZH}
_EXAMPLES_BY_LOCALE: dict[str, str] = {"en": _EXAMPLES_EN, "zh": _EXAMPLES_ZH}


def render_rules(locale: str, *, include_examples: bool) -> str:
    rules = _RULES_BY_LOCALE.get(locale, _RULES_BY_LOCALE["en"])
    if not include_examples:
        return rules
    examples = _EXAMPLES_BY_LOCALE.get(locale, _EXAMPLES_BY_LOCALE["en"])
    return rules + examples


def render_export(holdings: list[Holding], locale: str) -> str:
    lines = [render_rules(locale, include_examples=False).rstrip(), ""]
    lines.extend(render_holding_line(h) for h in holdings)
    return "\n".join(lines).rstrip() + "\n"


def render_template(locale: str) -> str:
    return render_rules(locale, include_examples=True).rstrip() + "\n"


def holdings_export_filename(now: datetime | None = None) -> str:
    """UTC filename for GET /holdings/export, e.g. holdings-20260902-051530Z.md."""
    when = now if now is not None else datetime.now(tz=UTC)
    stamp = when.astimezone(UTC).strftime("%Y%m%d-%H%M%SZ")
    return f"holdings-{stamp}.md"
