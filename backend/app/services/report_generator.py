"""LLM report generation pipeline (Ring 0 — Stage F1).

Two-pass design:
  Pass 1  macro signals + anomalies + headlines → LOW_COST_LLM → search queries
  Tavily  execute queries, collect background snippets
  Pass 2  portfolio snapshot + Pass 1 context + search results → PRIMARY_LLM → §2/§3/§4 body
  Assemble  §1 (code-built) + §2/§3/§4 (LLM) → final Markdown
  Write   reports table (report_md + report_inputs JSONB)

Layer-3/4 compliance:
  - System prompt contains the full forbidden-vocabulary list and Layer 3 rule.
  - Disclaimer text is injected at the template layer (F3), not by the model.
  - Holdings data is isolated to Pass 2; Pass 1 sees macro signals and anomaly
    labels only (no user portfolio details).
  - OPENROUTER_DATA_COLLECTION = "deny" is enforced on Pass 2 (contains holdings).
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import httpx
import openai
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.deps import get_current_user_id
from app.core.timezones import ET
from app.models.report import Report
from app.services.macro_detector import MacroSignals, detect_macro_signals
from app.services.news_fetcher import NewsItem, fetch_news
from app.services.portfolio_calculator import PortfolioSnapshot, compute_portfolio
from app.services.price_anomaly_detector import PriceAnomaly, detect_price_anomalies

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PROMPT_VERSION = "f1-v1"
_DISCLAIMER_VERSION = "en-v1"

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

    When with_holdings=True, the data_collection policy from settings is
    enforced (deny) so the call never routes to providers that train on payload.
    """
    extra: dict[str, Any] = {}
    settings = get_settings()
    order = [p.strip() for p in settings.OPENROUTER_PROVIDER_ORDER.split(",") if p.strip()]
    provider: dict[str, object] = {"allow_fallbacks": settings.OPENROUTER_ALLOW_FALLBACKS}
    if order:
        provider["order"] = order
    if with_holdings and settings.OPENROUTER_DATA_COLLECTION:
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
    anomalies: list[PriceAnomaly],
    news: list[NewsItem],
) -> str:
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
    lines.append("=== PRICE ANOMALIES ===")
    if anomalies:
        for pa in anomalies[:10]:
            direction = "+" if pa.pct_change > 0 else ""
            lines.append(
                f"  {pa.name} ({pa.identifier}): {direction}{float(pa.pct_change) * 100:.2f}% [{pa.asset_type}]"
            )
    else:
        lines.append("(no anomalies detected)")

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
        lines.append(f"Stale/no-price tickers: {', '.join(stale)}")

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
    for h in sorted(portfolio.get("holdings", []), key=lambda x: -x.get("market_value_base", 0)):
        mv = h.get("market_value", 0)
        mv_base = h.get("market_value_base", 0)
        ratio = mv_base / total if total > 0 else 0
        name_col = h["name"] + (f" ({h['ticker']})" if h.get("ticker") else "")
        lines.append(
            f"| {name_col} | {h.get('currency', '')} | {mv:,.0f} | {ratio:.1%} | {h.get('market', '')}"
            f" | {h.get('sector', '—')} |"
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
# Public API
# ---------------------------------------------------------------------------


def generate_report(
    session: Session,
    report_date: date | None = None,
    report_type: str = "weekly",
    base_currency: str = "USD",
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
    # Create report record (status=in_progress)
    # ------------------------------------------------------------------
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
            section1 = _build_section1(ctx.portfolio_summary)
            quiet_md = (
                f"{section1}\n\n"
                "## §2 Macro Signals\n\n"
                "No macro keyword themes triggered in the past 24 hours.\n\n"
                "## §3 Holdings Intelligence\n\n"
                "No significant market developments detected for monitored holdings.\n\n"
                "## §4 Risk Radar\n\n"
                "No price anomalies or concentration alerts at this time."
            )
            report.status = "skipped"
            report.report_md = quiet_md
            report.report_inputs = ctx.to_jsonb()
            report.generated_at = datetime.now(tz=UTC)
            session.commit()
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
        pass1_user = _build_pass1_prompt(macro_signals, anomalies, news_items)

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
        # 5. Assemble §1 (code-built)
        # ------------------------------------------------------------------
        section1_md = _build_section1(ctx.portfolio_summary)

        # ------------------------------------------------------------------
        # 6. Pass 2 — full report body
        # ------------------------------------------------------------------
        primary_model = settings.PRIMARY_LLM_MODEL

        pass2_system = _COMPLIANCE_SYSTEM_PREFIX + (
            "\nYou are writing a structured financial intelligence briefing for a "
            "private investor. Use Markdown. Be concise and factual. "
            "Cite search results with [S#] notation (e.g. [S1], [S2]). "
            "Every analytical conclusion must end with the marker: "
            "[For information only — not investment advice]"
        )
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
        raw_pass2 = _call_llm(client, primary_model, pass2_system, pass2_user, with_holdings=True)
        ctx.pass2_raw = raw_pass2

        # ------------------------------------------------------------------
        # 7. Assemble final report
        # ------------------------------------------------------------------
        report_date_str = eff_date.strftime("%Y-%m-%d")
        header = f"# Portfonia Intelligence Report — {report_date_str}\n\n"
        full_md = header + section1_md + "\n\n" + raw_pass2

        # ------------------------------------------------------------------
        # 8. Persist
        # ------------------------------------------------------------------
        report.status = "success"
        report.report_md = full_md
        report.report_inputs = ctx.to_jsonb()
        report.generated_at = datetime.now(tz=UTC)
        session.commit()

        logger.info(
            "report %s: generation complete (%d chars, %d search results)",
            report.id,
            len(full_md),
            len(ctx.search_results),
        )
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
