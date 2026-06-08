"""LLM report generation pipeline (Ring 0 — Stage F2).

Two-pass design:
  Pass 1  macro signals + headlines → LOW_COST_LLM → search queries
  Tavily  execute queries, collect background snippets
  Pass 2  portfolio snapshot + Pass 1 context + anomalies + search results → PRIMARY_LLM → §2/§3/§4 body
  F2 annotate  post-process Pass 2 output → inject [行情] / [新闻] / [分析] markers
  Compliance scan  reject forbidden advisory language in the body (→ needs_review)
  Assemble  header + data-window + §1 (code-built) + annotated §2/§3/§4 + footer
  Render   translate the assembled report to the output language (#8)
  Write   reports table (report_md + report_inputs JSONB)

Layer-3/4 compliance:
  - System prompt contains the full forbidden-vocabulary list and Layer 3 rule.
  - A post-generation scan backstops the prompt: a body that emits forbidden
    advisory language is held as 'needs_review' and never emailed.
  - Disclaimer text is injected at the template layer (F3), not by the model.
  - Holdings data is isolated to Pass 2; Pass 1 sees macro signals and public
    headlines only — never anomalies (which are holdings-derived).
  - OPENROUTER_DATA_COLLECTION = "deny" is enforced on every LLM call.

Source annotation (F2):
  [行情]       line references portfolio snapshot data directly (ticker/fund_code present,
               no Tavily citation)
  [新闻]       collapsed from the LLM's [S#] notation; line is news-sourced
  [分析]       analytical inference by the LLM; injected before the compliance marker
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import httpx
import openai
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.deps import get_current_user_id
from app.core.timezones import ET
from app.models.report import Report
from app.services.email_sender import send_report_email
from app.services.macro_detector import MacroSignals, detect_macro_signals
from app.services.news_fetcher import NewsItem, fetch_news
from app.services.portfolio_calculator import PortfolioSnapshot, compute_portfolio
from app.services.price_anomaly_detector import PriceAnomaly, detect_price_anomalies

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PROMPT_VERSION = "f2-v2"  # f2-v2: Pass 1 no longer carries holdings-derived anomalies
_DISCLAIMER_VERSION = "f3-bilingual-v1"

# A run of one or more LLM citations ([S6][S7][S8] or "[S6] [S7]") collapses to a
# single bare [新闻] marker: the dangling S-numbers resolve to nothing in the
# rendered report, so they are noise — but "this line is news-sourced" is signal
# worth keeping. Trailing whitespace after the last citation is preserved.
_NEWS_RUN_RE = re.compile(r"\[S\d+\](?:\s*\[S\d+\])*")
# Compliance suffix that the LLM appends to every analytical conclusion.
_COMPLIANCE_MARKER = "[For information only — not investment advice]"

# Output-side compliance backstop (defense in depth on top of the system prompt).
# High-precision advisory patterns only — bare words like "buy"/"sell"/"hold"
# are deliberately excluded to avoid false positives on factual prose
# ("Holdings", "buyback", "exit poll"). Scanned against the LLM body ONLY, never
# the template footer (whose disclaimer legitimately says "not a recommendation
# to buy or sell"). A hit marks the report 'needs_review' and suppresses email.
_FORBIDDEN_OUTPUT_PATTERNS: list[re.Pattern[str]] = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\brecommend\w*",
        r"\bshould\s+(buy|sell|hold)\b",
        r"\breduce\s+exposure\b",
        r"\bincrease\s+(your\s+)?position\b",
        r"\bstop[-\s]?loss\b",
        r"\btarget\s+price\b",
        r"\bentry\s+point\b",
        r"\boversold\b",
        r"\boverbought\b",
        r"\bstrong\s+buy\b",
        r"\b(bullish|bearish)\s+rating\b",
        r"\bwill\s+(rise|fall)\s+to\b",
        # High-precision Chinese advisory terms (body may be EN, but guard anyway).
        r"止损",
        r"强烈买入",
        r"目标价",
        r"投资建议",
    )
]


def _scan_forbidden_output(body: str) -> list[str]:
    """Return distinct forbidden advisory phrases found in an LLM report body.

    Empty list = compliant. This is a backstop, not the primary guard — the
    Layer-3 rule and vocabulary blacklist live in the system prompt. It exists
    because prompt instructions are not a guarantee, and the Layer-4 boundary is
    a hard prohibition for an intelligence (non-advisory) product.
    """
    found: list[str] = []
    seen: set[str] = set()
    for pat in _FORBIDDEN_OUTPUT_PATTERNS:
        for m in pat.finditer(body):
            term = m.group(0)
            if term.lower() not in seen:
                seen.add(term.lower())
                found.append(term)
    return found


# Maximum search queries to run per report (avoids blowing Tavily daily budget
# on a single run; the LLM is instructed to output ≤ this many anyway).
_MAX_SEARCH_QUERIES = 5
_TAVILY_MAX_RESULTS = 5  # results per query
_TAVILY_SEARCH_DEPTH = "basic"

# System prompt prefix injected into every LLM call for Layer 3/4 compliance.
# This text is not user-tunable.
_COMPLIANCE_SYSTEM_PREFIX = """\
MANDATORY COMPLIANCE — NEVER VIOLATE:
You are an intelligence analyst, not a financial advisor. Your output describes \
what is happening in markets and what signals are worth watching. You NEVER recommend \
actions to take.

Forbidden vocabulary (never emit these words or their equivalents in any language):
recommend, should buy, should sell, hold, reduce exposure, increase position, exit, \
stop-loss, target price, will rise to, will fall to, entry point, oversold, overbought, \
strong buy, bullish rating, bearish rating.

Every analytical conclusion you state must end with the marker:
[For information only — not investment advice]
"""

# Pass 2 task instructions (shared by live generation and re-analysis).
_PASS2_SYSTEM = _COMPLIANCE_SYSTEM_PREFIX + (
    "\nYou are writing a structured financial intelligence briefing for a "
    "private investor. Use Markdown. Be concise and factual. "
    "Cite search results with [S#] notation (e.g. [S1], [S2]). "
    "Every analytical conclusion must end with the marker: "
    "[For information only — not investment advice]"
)


# ---------------------------------------------------------------------------
# Intermediate-data capture (stored in report_inputs JSONB)
# ---------------------------------------------------------------------------


def _decimal_default(o: object) -> object:
    if isinstance(o, Decimal):
        return float(o)
    if isinstance(o, date):
        return o.isoformat()
    if isinstance(o, datetime):
        return o.isoformat()
    if isinstance(o, uuid.UUID):
        return str(o)
    raise TypeError(f"not JSON-serialisable: {type(o)}")


@dataclass
class ReportContext:
    """Intermediate documents captured for the report_inputs JSONB column."""

    portfolio_summary: dict[str, Any] = field(default_factory=dict)
    news_items: list[dict[str, Any]] = field(default_factory=list)
    macro_signals: dict[str, Any] = field(default_factory=dict)
    price_anomalies: list[dict[str, Any]] = field(default_factory=list)
    pass1_model: str = ""
    pass1_prompt: str = ""
    pass1_raw: str = ""
    search_queries: list[str] = field(default_factory=list)
    search_results: list[dict[str, Any]] = field(default_factory=list)
    pass2_model: str = ""
    pass2_prompt: str = ""
    pass2_raw: str = ""

    def to_jsonb(self) -> dict[str, Any]:
        result: dict[str, Any] = json.loads(json.dumps(asdict(self), default=_decimal_default))
        return result


# ---------------------------------------------------------------------------
# LLM client helpers
# ---------------------------------------------------------------------------


def _openrouter_client() -> openai.OpenAI:
    settings = get_settings()
    return openai.OpenAI(
        api_key=settings.OPENROUTER_API_KEY.get_secret_value(),
        base_url=settings.OPENROUTER_BASE_URL,
    )


def _call_llm(
    client: openai.OpenAI,
    model: str,
    system: str,
    user: str,
    *,
    with_holdings: bool = False,
) -> str:
    """Call an OpenRouter model.  Returns the assistant content string.

    The data_collection policy (deny) is enforced on EVERY call as defense in
    depth: although Pass 1 is contractually holdings-free, denying training
    providers unconditionally means an accidental future holdings leak is still
    protected. `with_holdings` is retained only as an explicit intent marker for
    callers (and the test harness) — it no longer gates the data policy.
    """
    extra: dict[str, Any] = {}
    settings = get_settings()
    order = [p.strip() for p in settings.OPENROUTER_PROVIDER_ORDER.split(",") if p.strip()]
    provider: dict[str, object] = {"allow_fallbacks": settings.OPENROUTER_ALLOW_FALLBACKS}
    if order:
        provider["order"] = order
    if settings.OPENROUTER_DATA_COLLECTION:
        provider["data_collection"] = settings.OPENROUTER_DATA_COLLECTION
    if provider.keys() - {"allow_fallbacks"}:
        extra["provider"] = provider

    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.3,
        extra_body=extra if extra else None,
    )
    content = resp.choices[0].message.content or ""
    return content.strip()


# ---------------------------------------------------------------------------
# Tavily search
# ---------------------------------------------------------------------------


def _run_tavily_search(
    queries: list[str],
    budget: int,
) -> list[dict[str, Any]]:
    """Execute up to `budget` Tavily searches.  Returns flattened result list.

    Each result dict: {query, title, url, content, score}.
    Failures on individual queries are logged and skipped (degraded mode).
    """
    settings = get_settings()
    api_key = settings.TAVILY_API_KEY.get_secret_value()
    effective_queries = queries[:budget]

    all_results: list[dict[str, Any]] = []
    for i, query in enumerate(effective_queries):
        try:
            resp = httpx.post(
                "https://api.tavily.com/search",
                json={
                    "api_key": api_key,
                    "query": query,
                    "search_depth": _TAVILY_SEARCH_DEPTH,
                    "topic": "news",
                    "max_results": _TAVILY_MAX_RESULTS,
                },
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            for r in data.get("results", []):
                all_results.append(
                    {
                        "query": query,
                        "title": r.get("title", ""),
                        "url": r.get("url", ""),
                        "content": r.get("content", "")[:600],
                        "score": r.get("score", 0.0),
                        "index": len(all_results) + 1,
                    }
                )
            logger.info(
                "Tavily query %d/%d: %d results",
                i + 1,
                len(effective_queries),
                len(data.get("results", [])),
            )
        except Exception:
            logger.exception("Tavily search failed for query: %s", query)
    return all_results


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------


def _serialize_news(items: list[NewsItem]) -> list[dict[str, Any]]:
    return [
        {
            "title": it.title,
            "source": it.source,
            "url": it.url,
            "published_at": it.published_at.isoformat(),
            "summary": it.summary,
        }
        for it in items
    ]


def _serialize_macro(signals: MacroSignals) -> dict[str, Any]:
    hits: list[dict[str, Any]] = []
    for h in signals.hits:
        hits.append(
            {
                "theme": h.theme,
                "keywords_found": h.keywords_found,
                "article_count": len(h.articles),
                "top_articles": [{"title": a.title, "source": a.source} for a in h.articles[:3]],
            }
        )
    return {
        "has_any_hit": signals.has_any_hit,
        "total_matched_articles": signals.total_matched_articles,
        "hits": hits,
    }


def _serialize_anomalies(anomalies: list[PriceAnomaly]) -> list[dict[str, Any]]:
    return [
        {
            "name": a.name,
            "identifier": a.identifier,
            "asset_type": a.asset_type,
            "pct_change": float(a.pct_change),
            "threshold": float(a.threshold),
            "current_price": float(a.current_price),
            "prev_price": float(a.prev_price),
        }
        for a in anomalies
    ]


def _serialize_portfolio(snap: PortfolioSnapshot) -> dict[str, Any]:
    holdings_list = [
        {
            "name": hv.name,
            "ticker": hv.ticker,
            "fund_code": hv.fund_code,
            "currency": hv.currency,
            "asset_type": hv.asset_type,
            "sector": hv.sector,
            "market": hv.market,
            "market_value": float(hv.market_value),
            "market_value_base": float(hv.market_value_base),
            "price_as_of": hv.price_as_of.isoformat() if hv.price_as_of else None,
            "position": hv.position if hv.position is not None else 1_000_000,
        }
        for hv in snap.holdings
    ]
    return {
        "base_currency": snap.base_currency,
        "fx_date": snap.fx_date.isoformat(),
        "total_base": float(snap.total_base),
        "by_market": {k: float(v) for k, v in snap.by_market.items()},
        "by_currency": {k: float(v) for k, v in snap.by_currency.items()},
        "by_asset_type": {k: float(v) for k, v in snap.by_asset_type.items()},
        "by_sector": {k: float(v) for k, v in snap.by_sector.items()},
        "concentration": {
            "top_holding_name": snap.concentration.top_holding_name,
            "top_holding_ratio": float(snap.concentration.top_holding_ratio)
            if snap.concentration.top_holding_ratio is not None
            else None,
            "top3_ratio": float(snap.concentration.top3_ratio)
            if snap.concentration.top3_ratio is not None
            else None,
            "top_sector_name": snap.concentration.top_sector_name,
            "top_sector_ratio": float(snap.concentration.top_sector_ratio)
            if snap.concentration.top_sector_ratio is not None
            else None,
            "single_holding_watch": snap.concentration.single_holding_watch,
            "single_holding_high": snap.concentration.single_holding_high,
            "top3_watch": snap.concentration.top3_watch,
            "sector_watch": snap.concentration.sector_watch,
        },
        "stale_tickers": snap.stale_tickers,
        "holdings": holdings_list,
    }


# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------


def _build_pass1_prompt(
    signals: MacroSignals,
    news: list[NewsItem],
) -> str:
    # DATA ISOLATION: Pass 1 runs without data_collection=deny, so it must carry
    # only public information (macro themes + news headlines). Price anomalies are
    # holdings-derived (name/ticker reveal what the user owns) and are therefore
    # withheld here — they are supplied only to Pass 2, which enforces deny.
    lines: list[str] = []

    lines.append("=== TODAY'S MACRO SIGNAL THEMES ===")
    if signals.has_any_hit:
        for h in signals.hits:
            kw_str = ", ".join(h.keywords_found[:5])
            lines.append(f"Theme: {h.theme} (keywords: {kw_str})")
            for a in h.articles[:2]:
                lines.append(f"  - [{a.source}] {a.title}")
    else:
        lines.append("(no macro themes triggered)")

    lines.append("")
    lines.append("=== TOP HEADLINES (past 24 h) ===")
    for item in news[:15]:
        lines.append(f"  [{item.source}] {item.title}")

    lines.append("")
    lines.append(
        "Generate 3-5 specific web search queries to retrieve background information "
        "on the most important developments above. Focus on understanding what is "
        "happening and what is driving the signals — not investment recommendations.\n"
        "Output ONLY a JSON object in this exact format:\n"
        '{"queries": ["<query 1>", "<query 2>", ...]}'
    )
    return "\n".join(lines)


def _build_pass2_prompt(
    portfolio: dict[str, Any],
    macro: dict[str, Any],
    anomalies: list[dict[str, Any]],
    search_results: list[dict[str, Any]],
) -> str:
    lines: list[str] = []

    # Portfolio snapshot (scoped to what's needed — not full history)
    total = portfolio.get("total_base", 0)
    base_ccy = portfolio.get("base_currency", "USD")
    fx_date = portfolio.get("fx_date", "unknown")
    lines.append(f"=== PORTFOLIO SNAPSHOT (base: {base_ccy}, FX date: {fx_date}) ===")
    lines.append(f"Total: {base_ccy} {total:,.0f}")
    lines.append("")
    lines.append("Holdings:")
    for h in portfolio.get("holdings", []):
        mv_base = h.get("market_value_base", 0)
        ratio = mv_base / total if total > 0 else 0
        lines.append(
            f"  {h['name']}"
            + (f" ({h['ticker']})" if h.get("ticker") else "")
            + f" — {h.get('currency', '')} {h.get('market_value', 0):,.0f}"
            + f" ({ratio:.1%} of portfolio)"
            + (f" | sector: {h['sector']}" if h.get("sector") else "")
        )
    lines.append("")
    lines.append(f"By market: {portfolio.get('by_market', {})}")
    lines.append(f"By currency: {portfolio.get('by_currency', {})}")

    conc = portfolio.get("concentration", {})
    if conc.get("single_holding_watch") or conc.get("top3_watch") or conc.get("sector_watch"):
        lines.append("")
        lines.append("Concentration flags:")
        if conc.get("single_holding_high"):
            lines.append(
                f"  [!] Top holding {conc.get('top_holding_name')} = {conc.get('top_holding_ratio', 0):.1%} (>25% threshold)"
            )
        elif conc.get("single_holding_watch"):
            lines.append(
                f"  Top holding {conc.get('top_holding_name')} = {conc.get('top_holding_ratio', 0):.1%} (>15% watch)"
            )
        if conc.get("top3_watch"):
            lines.append(f"  Top-3 combined = {conc.get('top3_ratio', 0):.1%} (>50% watch)")
        if conc.get("sector_watch"):
            lines.append(
                f"  Top sector ({conc.get('top_sector_name')}) = {conc.get('top_sector_ratio', 0):.1%} (>35% watch)"
            )

    stale = portfolio.get("stale_tickers", [])
    if stale:
        lines.append("Stale/no-price identifiers (excluded from valuations):")
        for ident in stale:
            lines.append(f"  - {_stale_ticker_hint(ident)}")

    # Macro signals
    lines.append("")
    lines.append("=== MACRO SIGNAL THEMES ===")
    if macro.get("has_any_hit"):
        for hit in macro.get("hits", []):
            lines.append(
                f"Theme: {hit['theme']} — keywords: {', '.join(hit.get('keywords_found', []))}"
            )
            for art in hit.get("top_articles", []):
                lines.append(f"  [{art['source']}] {art['title']}")
    else:
        lines.append("(quiet day — no macro themes triggered)")

    # Price anomalies
    lines.append("")
    lines.append("=== PRICE ANOMALIES ===")
    if anomalies:
        for a in anomalies:
            direction = "+" if a.get("pct_change", 0) > 0 else ""
            lines.append(
                f"  {a['name']} ({a['identifier']}): "
                f"{direction}{a.get('pct_change', 0) * 100:.2f}% "
                f"[{a.get('asset_type', '')}]"
            )
    else:
        lines.append("(no anomalies)")

    # Search results
    if search_results:
        lines.append("")
        lines.append("=== BACKGROUND RESEARCH ===")
        for r in search_results:
            lines.append(f"[S{r.get('index', '?')}] {r.get('title', '')} ({r.get('url', '')})")
            if r.get("content"):
                lines.append(f"  {r['content'][:400]}")

    # Instructions
    lines.append("")
    lines.append(
        "Write sections §2, §3, and §4 of the intelligence briefing in Markdown.\n"
        "Use the portfolio and signal data above. Be specific about which holdings "
        "are exposed to which signals. Cite search results with [S#] notation.\n\n"
        "## §2 Macro Signals\n"
        "Summarise the triggered macro themes and the most relevant news developments. "
        "Describe what is happening and why it is relevant to monitor.\n\n"
        "## §3 Holdings Intelligence\n"
        "For each holding exposed to today's signals, describe what happened "
        "and what the relevant context is from the search results.\n\n"
        "## §4 Risk Radar\n"
        "Describe concentration flags, price anomalies, FX moves, and stale data. "
        "State the numbers; do not editorialize about what to do."
    )
    return "\n".join(lines)


def _build_section1(portfolio: dict[str, Any]) -> str:
    """Build §1 Portfolio Snapshot entirely from data — no LLM."""
    base_ccy = portfolio.get("base_currency", "USD")
    fx_date = portfolio.get("fx_date", "N/A")
    total = portfolio.get("total_base", 0)

    lines: list[str] = [
        "## §1 Portfolio Snapshot",
        "",
        f"**Total value:** {base_ccy} {total:,.0f}  (FX date: {fx_date})",
        "",
        "| Holding | Currency | Value | % Portfolio | Market | Sector |",
        "|---------|----------|-------|-------------|--------|--------|",
    ]

    holdings = list(portfolio.get("holdings", []))
    # Group by market, preserving the user's upload order: market groups appear
    # in the order they first show up in the file, holdings within a group keep
    # their file order. Each group gets a subtotal so cross-market capital is
    # legible — this is what the user encodes via the .md `market` column.
    group_order: list[str] = []
    groups: dict[str, list[dict[str, Any]]] = {}
    for h in sorted(holdings, key=lambda x: x.get("position", 1_000_000)):
        mkt = h.get("market") or "Other"
        if mkt not in groups:
            groups[mkt] = []
            group_order.append(mkt)
        groups[mkt].append(h)

    for mkt in group_order:
        members = groups[mkt]
        subtotal_base = sum(m.get("market_value_base", 0) for m in members)
        for h in members:
            mv = h.get("market_value", 0)
            mv_base = h.get("market_value_base", 0)
            ratio = mv_base / total if total > 0 else 0
            name_col = h["name"] + (f" ({h['ticker']})" if h.get("ticker") else "")
            lines.append(
                f"| {name_col} | {h.get('currency', '')} | {mv:,.0f} | {ratio:.1%} "
                f"| {h.get('market', '')} | {h.get('sector', '—')} |"
            )
        sub_ratio = subtotal_base / total if total > 0 else 0
        lines.append(
            f"| **{mkt} subtotal** | {base_ccy} | **{subtotal_base:,.0f}** "
            f"| **{sub_ratio:.1%}** | | |"
        )

    lines += [
        "",
        "**Distribution:**",
        "",
    ]
    for label, dist in [
        ("By market", portfolio.get("by_market", {})),
        ("By currency", portfolio.get("by_currency", {})),
        ("By asset type", portfolio.get("by_asset_type", {})),
    ]:
        if dist:
            parts = ", ".join(
                f"{k}: {v / total:.1%}"
                for k, v in sorted(dist.items(), key=lambda x: -x[1])
                if total > 0
            )
            lines.append(f"- **{label}:** {parts}")

    stale = portfolio.get("stale_tickers", [])
    if stale:
        lines.append(f"\n> [!] Stale/missing prices: {', '.join(stale)}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Stale ticker type hint
# ---------------------------------------------------------------------------

_FUND_CODE_RE = re.compile(r"^\d{6}$")
_A_SHARE_RE = re.compile(r"^\d{6}\.(SS|SZ)$", re.IGNORECASE)
_HK_RE = re.compile(r"^\d{4}\.HK$", re.IGNORECASE)


def _stale_ticker_hint(identifier: str) -> str:
    """Return an annotated string for a stale identifier to prevent LLM misclassification.

    Without annotation, 6-digit fund codes (e.g. 005827 = 易方达蓝筹精选) get
    misidentified as Korea Exchange listings by the LLM.
    """
    if _FUND_CODE_RE.match(identifier):
        return f"{identifier} (CN mutual fund code / 天天基金)"
    if _A_SHARE_RE.match(identifier):
        return f"{identifier} (A-share / Shanghai or Shenzhen)"
    if _HK_RE.match(identifier):
        return f"{identifier} (HK-listed stock)"
    return f"{identifier} (stock ticker)"


# ---------------------------------------------------------------------------
# F3 fixed footer
# ---------------------------------------------------------------------------

_DISCLAIMER_EN = (
    "This report is generated by an automated intelligence system and is provided "
    "for informational purposes only. It does not constitute investment advice, a "
    "recommendation to buy or sell any security, or a solicitation of any investment. "
    "Past performance is not indicative of future results. Always consult a qualified "
    "financial advisor before making investment decisions."
)

_DISCLAIMER_ZH = (
    "本报告由自动化信息系统生成，仅供参考，不构成投资建议、证券买卖推荐或投资招揽。"  # noqa: RUF001
    "历史业绩不代表未来表现。在做出任何投资决策前，请咨询持牌财务顾问。"  # noqa: RUF001
)


def _build_footer(portfolio: dict[str, Any]) -> str:
    """Build the fixed report footer (F3).

    Injected at the template layer — never generated by the LLM.
    Contains: FX rate note (date-stamped from portfolio snapshot) + bilingual disclaimer.
    """
    base_ccy = portfolio.get("base_currency", "USD")
    fx_date = portfolio.get("fx_date", "unknown")

    lines = [
        "",
        "---",
        "",
        "## Data Sources & Disclaimer",
        "",
        f"**Exchange rates:** As of {fx_date}. "
        f"All portfolio valuations are converted to {base_ccy} using same-day mid-market rates. "
        "Intraday FX moves are not reflected.",
        "",
        f"**Disclaimer:** {_DISCLAIMER_EN}",
        "",
        f"**免责声明：** {_DISCLAIMER_ZH}",  # noqa: RUF001
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# F2 source annotation
# ---------------------------------------------------------------------------


def _annotate_sources(text: str, portfolio: dict[str, Any]) -> str:
    """Post-process Pass 2 LLM output to inject source-provenance markers.

    Three operations, applied in order:

    1. Collapse news citations: a run of [S#] → a single [新闻]
       The dangling S-numbers are not resolvable in the rendered report; the
       provenance signal (news-sourced) is kept, the noise is dropped.

    2. Inject [行情] on lines that:
       - Reference a known portfolio identifier (ticker or fund_code), AND
       - Do not already carry a [新闻] marker on the same line.
       Rationale: a line mentioning AAPL without citing a search result is
       drawing on the portfolio snapshot, not external news.

    3. Inject [分析] on lines that contain the compliance marker.
       Every LLM analytical conclusion ends with _COMPLIANCE_MARKER; inserting
       [分析] immediately before it flags the preceding clause as LLM inference.
    """
    # Step 1 — collapse [S#] runs → single [新闻]
    text = _NEWS_RUN_RE.sub("[新闻]", text)

    # Build portfolio identifier set (ticker symbols and fund codes only;
    # full names are too prone to substring false-positives).
    identifiers: set[str] = set()
    for h in portfolio.get("holdings", []):
        if ticker := h.get("ticker"):
            identifiers.add(ticker)
        if fund_code := h.get("fund_code"):
            identifiers.add(fund_code)

    lines = text.split("\n")
    result: list[str] = []
    for line in lines:
        stripped = line.rstrip()

        # Step 2 — [行情]: portfolio identifier present, no news marker
        if identifiers and "[新闻]" not in stripped and "[行情]" not in stripped:
            for ident in identifiers:
                if re.search(r"\b" + re.escape(ident) + r"\b", stripped):
                    stripped = stripped + " [行情]"
                    break

        # Step 3 — [分析]: compliance marker present, insert [分析] before it
        if _COMPLIANCE_MARKER in stripped and "[分析]" not in stripped:
            stripped = stripped.replace(_COMPLIANCE_MARKER, f"[分析] {_COMPLIANCE_MARKER}")

        result.append(stripped)

    return "\n".join(result)


# ---------------------------------------------------------------------------
# Assembly / rendering (shared by live generation and re-render)
# ---------------------------------------------------------------------------


def _build_data_window(
    news_items: list[dict[str, Any]], portfolio: dict[str, Any], report_date_str: str
) -> str:
    """A one-line statement of the intel/data interval this report covers (#5)."""
    times = sorted(n["published_at"] for n in news_items if n.get("published_at"))
    if times:
        lo = times[0][:16].replace("T", " ")
        hi = times[-1][:16].replace("T", " ")
        news_line = f"news {lo} to {hi} UTC ({len(news_items)} items)"
    else:
        news_line = "no news in window"
    price_dates = sorted(
        h["price_as_of"][:10] for h in portfolio.get("holdings", []) if h.get("price_as_of")
    )
    price_line = f"prices through {price_dates[-1]}" if price_dates else "prices unavailable"
    fx_date = portfolio.get("fx_date", "n/a")
    return (
        f"> **Data window** — {news_line}; {price_line}; FX as of {fx_date}. "
        "Price moves are measured against the prior close.\n\n"
    )


def _translate_md(md: str, target_lang: str) -> str:
    """Translate an assembled report to *target_lang* (#8).

    The LLM reasons in English upstream; this renders the final text in the
    user's language. Tickers, numbers, table structure, and bracketed markers
    ([行情]/[新闻]/[分析]/the compliance marker) are preserved verbatim. 'en' is
    a no-op (the canonical language).
    """
    if target_lang == "en":
        return md
    lang_name = {"zh": "Simplified Chinese"}.get(target_lang, target_lang)
    settings = get_settings()
    system = (
        "You are a professional financial translator. Translate the user's Markdown "
        f"report into {lang_name}. STRICT RULES: preserve all Markdown structure, "
        "tables, and numbers exactly; keep ticker symbols, fund codes, currency "
        "codes, and any bracketed tag verbatim — including [行情], [新闻], [分析] and "
        "[For information only — not investment advice]. Translate only natural-"
        "language prose. Do not add, remove, or reorder content, and never introduce "
        "advisory or recommendation language."
    )
    return _call_llm(
        _openrouter_client(), settings.PRIMARY_LLM_MODEL, system, md, with_holdings=True
    )


def _render_full_md(
    report_date_str: str,
    portfolio: dict[str, Any],
    news_items: list[dict[str, Any]],
    raw_body: str,
    output_lang: str,
) -> tuple[str, list[str]]:
    """Annotate, assemble, language-render, and compliance-scan a report.

    Returns (full_markdown, violations). Pure function of its inputs — this is
    what makes #6 re-render possible: the same stored inputs reproduce the same
    report without re-fetching news or re-running search.
    """
    annotated = _annotate_sources(raw_body, portfolio)
    header = f"# Portfonia Intelligence Report — {report_date_str}\n\n"
    window = _build_data_window(news_items, portfolio, report_date_str)
    section1 = _build_section1(portfolio)
    dynamic_en = header + window + section1 + "\n\n" + annotated

    # Compliance scan on the English canonical first (highest-signal blacklist).
    violations = _scan_forbidden_output(annotated)

    dynamic_out = _translate_md(dynamic_en, output_lang)
    if output_lang != "en":
        # Translation can paraphrase into advisory tone — re-scan the output.
        violations = violations + _scan_forbidden_output(dynamic_out)

    full_md = dynamic_out + _build_footer(portfolio)
    return full_md, violations


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def generate_report(
    session: Session,
    report_date: date | None = None,
    report_type: str = "weekly",
    base_currency: str = "USD",
    output_lang: str = "en",
) -> Report:
    """
    Run the full F1 report generation pipeline and persist the result.

    Returns the Report ORM object (status='success' or 'failed').
    Raises if the report record cannot be written (e.g. unique constraint violation
    when a report for the same date+type already exists).
    """
    settings = get_settings()
    user_id = get_current_user_id()
    eff_date = report_date or datetime.now(tz=ET).date()

    # ------------------------------------------------------------------
    # Idempotency: (user_id, report_date, report_type) is unique. A redelivered
    # Celery task (task_acks_late=True) or a manual /reports/generate racing the
    # weekly Beat run can re-enter for the same key. Short-circuit a completed
    # report instead of inserting a duplicate that would fail on flush; reuse a
    # prior failed/in_progress row so a retry can regenerate in place.
    # ------------------------------------------------------------------
    existing = session.execute(
        select(Report).where(
            Report.user_id == user_id,
            Report.report_date == eff_date,
            Report.report_type == report_type,
        )
    ).scalar_one_or_none()

    if existing is not None and existing.status in ("success", "skipped"):
        logger.info(
            "report %s: already complete for %s (status=%s) — returning existing",
            existing.id,
            eff_date,
            existing.status,
        )
        return existing

    # ------------------------------------------------------------------
    # Create or reset report record (status=in_progress)
    # ------------------------------------------------------------------
    if existing is not None:
        report = existing
        report.status = "in_progress"
        report.prompt_version = _PROMPT_VERSION
        report.disclaimer_version = _DISCLAIMER_VERSION
        report.report_md = None
        report.report_inputs = None
        report.generated_at = None
        report.email_sent_at = None
    else:
        report = Report(
            user_id=user_id,
            report_date=eff_date,
            report_type=report_type,
            status="in_progress",
            prompt_version=_PROMPT_VERSION,
            disclaimer_version=_DISCLAIMER_VERSION,
        )
        session.add(report)
    session.flush()  # get the id without committing
    logger.info("report %s: generation started for %s", report.id, eff_date)

    ctx = ReportContext()

    try:
        # ------------------------------------------------------------------
        # 1. Gather inputs
        # ------------------------------------------------------------------
        logger.info("report %s: fetching portfolio snapshot", report.id)
        portfolio_snap = compute_portfolio(session, base_currency=base_currency)
        ctx.portfolio_summary = _serialize_portfolio(portfolio_snap)

        logger.info("report %s: fetching news", report.id)
        news_items = fetch_news()
        ctx.news_items = _serialize_news(news_items)

        logger.info("report %s: detecting macro signals", report.id)
        macro_signals = detect_macro_signals(news_items)
        ctx.macro_signals = _serialize_macro(macro_signals)

        logger.info("report %s: detecting price anomalies", report.id)
        anomalies = detect_price_anomalies(session)
        ctx.price_anomalies = _serialize_anomalies(anomalies)

        # ------------------------------------------------------------------
        # 2. Skip check
        # ------------------------------------------------------------------
        if not macro_signals.has_any_hit and not anomalies:
            logger.info("report %s: quiet day — no signals, no anomalies", report.id)
            quiet_body = (
                "## §2 Macro Signals\n\n"
                "No macro keyword themes triggered in the past 24 hours.\n\n"
                "## §3 Holdings Intelligence\n\n"
                "No significant market developments detected for monitored holdings.\n\n"
                "## §4 Risk Radar\n\n"
                "No price anomalies or concentration alerts at this time."
            )
            quiet_md, _ = _render_full_md(
                eff_date.strftime("%Y-%m-%d"),
                ctx.portfolio_summary,
                ctx.news_items,
                quiet_body,
                output_lang,
            )
            report.status = "skipped"
            report.report_md = quiet_md
            report.report_inputs = ctx.to_jsonb()
            report.generated_at = datetime.now(tz=UTC)
            session.commit()
            # Heartbeat: still email on a quiet week so the recipient can tell a
            # genuinely calm week apart from a silently broken pipeline. Isolated
            # so a delivery failure cannot corrupt the committed 'skipped' status.
            try:
                send_report_email(report, session)
            except Exception:
                logger.exception("report %s: quiet-day email send raised unexpectedly", report.id)
            return report

        # ------------------------------------------------------------------
        # 3. Pass 1 — search intent
        # ------------------------------------------------------------------
        client = _openrouter_client()
        low_cost_model = settings.LOW_COST_LLM_MODEL

        pass1_system = _COMPLIANCE_SYSTEM_PREFIX + (
            "\nYou are generating search queries for a financial intelligence analyst. "
            "Output ONLY a JSON object with a list of search queries. No other text."
        )
        # Anomalies are intentionally NOT passed: they are holdings-derived and
        # Pass 1 must stay holdings-free (see _build_pass1_prompt).
        pass1_user = _build_pass1_prompt(macro_signals, news_items)

        ctx.pass1_model = low_cost_model
        ctx.pass1_prompt = pass1_user

        logger.info("report %s: Pass 1 LLM call (%s)", report.id, low_cost_model)
        raw_pass1 = _call_llm(client, low_cost_model, pass1_system, pass1_user, with_holdings=False)
        ctx.pass1_raw = raw_pass1

        # Parse search queries from Pass 1 response
        search_queries: list[str] = []
        try:
            # Strip possible markdown fences
            clean = raw_pass1.strip()
            if clean.startswith("```"):
                clean = "\n".join(
                    ln for ln in clean.splitlines() if not ln.strip().startswith("```")
                ).strip()
            parsed = json.loads(clean)
            search_queries = [str(q) for q in parsed.get("queries", []) if q]
        except Exception:
            logger.warning("report %s: could not parse Pass 1 JSON, using empty queries", report.id)
        ctx.search_queries = search_queries[:_MAX_SEARCH_QUERIES]

        # ------------------------------------------------------------------
        # 4. Tavily search
        # ------------------------------------------------------------------
        if ctx.search_queries:
            logger.info(
                "report %s: running %d Tavily queries (budget %d)",
                report.id,
                len(ctx.search_queries),
                settings.TAVILY_DAILY_BUDGET,
            )
            search_results = _run_tavily_search(
                ctx.search_queries, budget=settings.TAVILY_DAILY_BUDGET
            )
        else:
            search_results = []
        ctx.search_results = search_results

        # Re-index results globally for [S#] citation notation
        for i, r in enumerate(ctx.search_results):
            r["index"] = i + 1

        # ------------------------------------------------------------------
        # 6. Pass 2 — full report body
        # ------------------------------------------------------------------
        primary_model = settings.PRIMARY_LLM_MODEL
        pass2_user = _build_pass2_prompt(
            ctx.portfolio_summary,
            ctx.macro_signals,
            ctx.price_anomalies,
            ctx.search_results,
        )

        ctx.pass2_model = primary_model
        ctx.pass2_prompt = pass2_user

        logger.info("report %s: Pass 2 LLM call (%s)", report.id, primary_model)
        # Pass 2 carries holdings → enforce data_collection=deny
        raw_pass2 = _call_llm(client, primary_model, _PASS2_SYSTEM, pass2_user, with_holdings=True)
        ctx.pass2_raw = raw_pass2

        # ------------------------------------------------------------------
        # 7/8. Annotate + assemble + render language + compliance scan (#5/#7/#8)
        # ------------------------------------------------------------------
        report_date_str = eff_date.strftime("%Y-%m-%d")
        full_md, violations = _render_full_md(
            report_date_str, ctx.portfolio_summary, ctx.news_items, raw_pass2, output_lang
        )
        logger.info("report %s: assembled + rendered (lang=%s)", report.id, output_lang)

        # ------------------------------------------------------------------
        # 9. Persist
        # ------------------------------------------------------------------
        # Compliance > everything: a body that tripped the blacklist is held as
        # 'needs_review' and never emailed — content is preserved for inspection.
        report.status = "needs_review" if violations else "success"
        report.report_md = full_md
        report.report_inputs = ctx.to_jsonb()
        report.generated_at = datetime.now(tz=UTC)
        session.commit()

        if violations:
            logger.error(
                "report %s: BLOCKED for compliance review — forbidden terms: %s",
                report.id,
                violations,
            )
            return report

        logger.info(
            "report %s: generation complete (%d chars, %d search results)",
            report.id,
            len(full_md),
            len(ctx.search_results),
        )

        # ------------------------------------------------------------------
        # 10. Email
        # ------------------------------------------------------------------
        # The report is already committed as 'success' above. send_report_email
        # is contracted never to raise, but we isolate it anyway so an unexpected
        # failure here cannot fall through to the generation-failure handler and
        # flip an already-persisted success to 'failed'.
        try:
            send_report_email(report, session)
        except Exception:
            logger.exception("report %s: email send raised unexpectedly", report.id)

        return report

    except Exception:
        logger.exception("report %s: generation failed", report.id)
        report.status = "failed"
        report.report_inputs = ctx.to_jsonb()
        try:
            session.commit()
        except Exception:
            session.rollback()
        raise


def regenerate_report(
    session: Session,
    report_id: uuid.UUID,
    *,
    mode: str = "render",
    output_lang: str = "en",
) -> Report:
    """Rebuild an existing report from its stored inputs WITHOUT re-fetching (#6).

    Intel acquisition (news, Tavily, Pass 1) is never repeated — that data is
    read back from `report_inputs`, so no token/credit is wasted on it.

    mode='render'  : zero new LLM cost except translation. Re-runs annotation +
                     assembly + language render from the stored Pass 2 body.
                     Use it to iterate on formatting/output language.
    mode='analyze' : re-runs only Pass 2 from the stored portfolio + search
                     results (no fetch/Tavily/Pass 1). Use it to iterate on the
                     Pass 2 prompt. Updates the stored Pass 2 body.

    Does not email — this is an iteration/inspection tool.
    """
    user_id = get_current_user_id()
    report = session.execute(
        select(Report).where(Report.id == report_id, Report.user_id == user_id)
    ).scalar_one_or_none()
    if report is None:
        raise ValueError(f"report {report_id} not found")
    inputs = report.report_inputs
    if not inputs or not inputs.get("pass2_raw"):
        raise ValueError(f"report {report_id} has no stored Pass 2 body to regenerate from")

    portfolio = inputs.get("portfolio_summary", {})
    news_items = inputs.get("news_items", [])

    if mode == "analyze":
        pass2_user = _build_pass2_prompt(
            portfolio,
            inputs.get("macro_signals", {}),
            inputs.get("price_anomalies", []),
            inputs.get("search_results", []),
        )
        raw_body = _call_llm(
            _openrouter_client(),
            get_settings().PRIMARY_LLM_MODEL,
            _PASS2_SYSTEM,
            pass2_user,
            with_holdings=True,
        )
        # New dict identity so SQLAlchemy flags the JSONB column dirty (an
        # in-place mutation of the existing dict would not be detected).
        report.report_inputs = {**inputs, "pass2_raw": raw_body, "pass2_prompt": pass2_user}
    elif mode == "render":
        raw_body = inputs["pass2_raw"]
    else:
        raise ValueError(f"unknown mode {mode!r} (expected 'render' or 'analyze')")

    report_date_str = report.report_date.strftime("%Y-%m-%d")
    full_md, violations = _render_full_md(
        report_date_str, portfolio, news_items, raw_body, output_lang
    )
    report.status = "needs_review" if violations else "success"
    report.report_md = full_md
    report.generated_at = datetime.now(tz=UTC)
    session.commit()
    logger.info("report %s: regenerated (mode=%s, lang=%s)", report.id, mode, output_lang)
    return report
