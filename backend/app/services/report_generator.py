"""LLM report generation pipeline (Ring 0 — Stage F2).

Two-pass design:
  Pass 1  macro signals + headlines → LOW_COST_LLM → search queries
  Tavily  execute queries, collect background snippets
  Pass 2  portfolio snapshot + Pass 1 context + anomalies + search results → PRIMARY_LLM → §2/§3/§4 body
  Strip   remove any inline citations / provenance tags / per-line disclaimers
  Compliance scan  reject forbidden advisory language in the body (→ needs_review)
  Assemble  header + data-window + §1 (code-built) + cleaned §2/§3/§4 + footer
  Render   translate the assembled report to the output language (#8)
  Write   reports table (report_md + report_inputs JSONB)

Layer-3/4 compliance:
  - System prompt contains the full forbidden-vocabulary list and Layer 3 rule.
  - A post-generation scan backstops the prompt: a body that emits forbidden
    advisory language is held as 'needs_review' and never emailed.
  - The single disclaimer lives in the template footer (F3); the body carries no
    per-sentence disclaimer suffix and no bracketed provenance tags. The model is
    told not to emit them and `_strip_markers` removes any that slip through.
  - Holdings data is isolated to Pass 2; Pass 1 sees macro signals and public
    headlines only — never anomalies (which are holdings-derived).
  - OPENROUTER_DATA_COLLECTION = "deny" is enforced on every LLM call.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import openai
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.compliance.forbidden_vocab import FORBIDDEN_OUTPUT_PATTERNS as _FORBIDDEN_OUTPUT_PATTERNS
from app.compliance.forbidden_vocab import PROMPT_VOCAB_STRING as _FORBIDDEN_PROMPT_VOCAB
from app.core.config import get_settings
from app.core.deps import get_current_user_id
from app.core.timezones import ET
from app.models.report import Report
from app.services.email_sender import send_report_email
from app.services.forward_events import load_forward_events
from app.services.holding_news import recall_holding_news
from app.services.macro_detector import MacroSignals, detect_macro_signals
from app.services.news_fetcher import NewsItem
from app.services.portfolio_calculator import PortfolioSnapshot, compute_portfolio
from app.services.price_anomaly_detector import PriceAnomaly
from app.services.technical_position import TechnicalPosition, compute_technical_positions
from app.services.window_data import (
    detect_window_anomalies,
    latest_window_close_date,
    load_news_window,
    user_watermark,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PROMPT_VERSION = "f2-v6"  # f2-v6: §4.2 cross-reference restricted to holdings actually in the anomaly table (R-8) + HOLDING-RELEVANT NEWS block from per-holding recall/targeted search (R-3); f2-v5 = direction-requires-evidence + divergence-is-the-signal (no price-direction claims without window data); f2-v4 = §4.2 code table + driver-only, evidence confidence labels, §4.4 technical position
_DISCLAIMER_VERSION = "f3-bilingual-v2"

# A run of one or more LLM citations ([S6][S7][S8] or "[S6] [S7]"). The S-numbers
# do not resolve in the rendered report, so the whole run is stripped (#9).
_NEWS_RUN_RE = re.compile(r"\[S\d+\](?:\s*\[S\d+\])*")
# Legacy per-conclusion disclaimer suffix. No longer emitted (the footer carries
# the single disclaimer); kept here so _strip_markers can remove any stray one.
_COMPLIANCE_MARKER = "[For information only — not investment advice]"

# Output-side compliance backstop — patterns and vocabulary are defined in
# app/compliance/forbidden_vocab.py (single source of truth shared with the
# LLM system prompt). Imported above as _FORBIDDEN_OUTPUT_PATTERNS.


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

# Forward calendar (#1): how far ahead §2.5 looks. The capture task fetches a
# wider horizon so the read is always populated within this window.
_FORWARD_WINDOW_DAYS = 10

# System prompt prefix injected into every LLM call for Layer 3/4 compliance.
# This text is not user-tunable.
_COMPLIANCE_SYSTEM_PREFIX = f"""\
MANDATORY COMPLIANCE — NEVER VIOLATE:
You are an intelligence analyst, not a financial advisor. Your output describes \
what is happening in markets and what signals are worth watching. You NEVER recommend \
actions to take.

Forbidden vocabulary (never emit these words or their equivalents in any language):
{_FORBIDDEN_PROMPT_VOCAB}.

Do NOT append any per-sentence disclaimer or marker, and do NOT write any \
disclaimer, legal notice, or "for informational purposes" statement anywhere — \
the report already carries a single bilingual disclaimer in its footer, added \
outside your output. Do NOT emit bracketed provenance tags of any kind (no [S#], \
no [news], no [analysis], no source labels). Write clean prose.
"""

# Pass 2 task instructions (shared by live generation and re-analysis).
_PASS2_SYSTEM = _COMPLIANCE_SYSTEM_PREFIX + (
    "\nYou are writing a structured financial analysis briefing for a "
    "private investor. Use Markdown. Be concise and factual. Write clean prose "
    "with no bracketed tags or citations.\n"
    "FORWARD EVENTS: if you reference a scheduled event (an upcoming data release, "
    "FOMC meeting, or earnings date), state only that it is scheduled and what is "
    "worth watching — NEVER predict its outcome, direction, or market impact "
    "('will rise/fall', 'likely to beat', 'expected to lift/hurt' are forbidden).\n"
    "DIRECTION REQUIRES EVIDENCE: a sentence that asserts how a SPECIFIC HOLDING'S "
    "PRICE moved, or is positioned to move (e.g. 'gained safe-haven buying', "
    "'sold off', 'outperformed', 'will see buying support'), must be grounded in "
    "the PRICE ANOMALIES or TECHNICAL POSITION data supplied for THAT holding. If "
    "no window price data is supplied for a holding, do not assert a price "
    "direction for it — describe only that a transmission channel exists, say "
    "plainly 'this report period has no price data to confirm the holding's "
    "direction', and cap the confidence label at [Speculative]. Textbook macro "
    "narratives (e.g. 'war risk -> gold rallies') are mechanisms, not "
    "observations — do not restate them as something that already happened to a "
    "specific holding without window price data.\n"
    "DIVERGENCE IS THE SIGNAL: if a holding's actual window price move CONTRADICTS "
    "the textbook direction implied by a macro narrative (e.g. gold falling during "
    "a war-risk spike), report that divergence itself as the noteworthy signal — "
    "do not silently follow the narrative and do not omit the contradiction.\n"
    "§4.2 CROSS-REFERENCES: 'see §4.2' / '见§4.2' may only be used for a holding "
    "that actually appears in the PRICE ANOMALIES data (the §4.2 table is built "
    "ONLY from those holdings). For a holding whose price divergence you raise from "
    "news/research but that is NOT in PRICE ANOMALIES, do NOT point to §4.2 — say "
    "plainly that it did not cross this report's anomaly-monitoring threshold "
    "(e.g. 'this holding did not trigger the report's anomaly threshold this "
    "window')."
)

# H-DEBT-2 completeness guard: a Pass 2 body shorter than this, or missing
# either heading, is treated as a truncated provider response.
_PASS2_REQUIRED_MARKERS = ("## §3", "## §4")
_PASS2_MIN_CHARS = 2000


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
    technical_positions: list[dict[str, Any]] = field(default_factory=list)
    forward_events: list[dict[str, Any]] = field(default_factory=list)
    # R-3 holding-relevant news: {identifier: [news dict, ...]} recalled from the
    # window store for the holdings that moved (plus any targeted-search items).
    holding_news: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    # ADR-002 window bookkeeping (ISO strings / int) for re-render reproducibility.
    period_start: str = ""
    period_end: str = ""
    window_trading_days: int = 0
    # R-5: ISO date of the last in-window close (the real PRICE-data cutoff,
    # distinct from period_end). Empty when the window has no captured close.
    price_data_through: str = ""
    pass1_model: str = ""
    pass1_prompt: str = ""
    pass1_raw: str = ""
    search_queries: list[str] = field(default_factory=list)
    search_results: list[dict[str, Any]] = field(default_factory=list)
    pass2_model: str = ""
    pass2_prompt: str = ""
    pass2_raw: str = ""
    # LLM call records (Pass 1 + Pass 2; translation chunks excluded as they are
    # cheap/many and the per-chunk token count is not material for cost audits).
    llm_calls: list[dict[str, Any]] = field(default_factory=list)
    # Snapshot of the translated report body (dynamic section only, pre-footer).
    # Stored so compliance attribution can be traced per translation chunk if needed.
    pass2_translated: str = ""

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


class LLMEmptyResponseError(RuntimeError):
    """A 200 response from OpenRouter carried no usable choices.

    Observed once as a malformed 200 (`choices=None`) from a low-cost model via
    DigitalOcean/Venice — `resp.choices[0]` then raised a bare, unclassified
    TypeError. Raising a named error lets callers (and Celery's retry) treat it
    as a transient provider fault rather than a code bug. (I-DEBT-2)
    """


# 429 backoff inside _call_llm: the synchronous POST /reports/generate path has
# no Celery-level retry, and even the Celery path benefits from absorbing a brief
# upstream rate-limit without burning a whole task retry. Short, bounded, then
# re-raise so the outer layer still sees a persistent failure. (I-DEBT-2)
_LLM_RATELIMIT_BACKOFF_SECONDS = (5.0, 15.0)


def _call_llm(
    client: openai.OpenAI,
    model: str,
    system: str,
    user: str,
    *,
    with_holdings: bool = False,
    pin_provider: bool = True,
    provider_order: list[str] | None = None,
    usage_sink: list[dict[str, Any]] | None = None,
) -> str:
    """Call an OpenRouter model.  Returns the assistant content string.

    The data_collection policy (deny) is enforced on EVERY call as defense in
    depth: although Pass 1 is contractually holdings-free, denying training
    providers unconditionally means an accidental future holdings leak is still
    protected. `with_holdings` is retained only as an explicit intent marker for
    callers (and the test harness) — it no longer gates the data policy.

    `pin_provider=False` omits OPENROUTER_PROVIDER_ORDER, letting OpenRouter
    route to whichever provider is available — used for translation calls so
    repeated requests aren't all funneled through the same (possibly
    rate-limited) provider pair.

    `provider_order`, when given, sets the provider preference list directly
    (overriding `pin_provider`/`OPENROUTER_PROVIDER_ORDER`) — used to steer a
    call toward providers known to be available when the default pool is
    rate-limited.
    """
    extra: dict[str, Any] = {}
    settings = get_settings()
    provider: dict[str, object] = {"allow_fallbacks": settings.OPENROUTER_ALLOW_FALLBACKS}
    if provider_order:
        provider["order"] = provider_order
    elif pin_provider:
        order = [p.strip() for p in settings.OPENROUTER_PROVIDER_ORDER.split(",") if p.strip()]
        if order:
            provider["order"] = order
    if settings.OPENROUTER_DATA_COLLECTION:
        provider["data_collection"] = settings.OPENROUTER_DATA_COLLECTION
    if provider.keys() - {"allow_fallbacks"}:
        extra["provider"] = provider

    resp: Any = None
    for attempt, backoff in enumerate((*_LLM_RATELIMIT_BACKOFF_SECONDS, None)):
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=0.3,
                extra_body=extra if extra else None,
            )
            break
        except openai.RateLimitError:
            if backoff is None:
                logger.warning("llm call: model=%s exhausted 429 backoff retries", model)
                raise
            logger.warning(
                "llm call: model=%s got 429 (attempt %d), backing off %.0fs",
                model,
                attempt + 1,
                backoff,
            )
            time.sleep(backoff)

    # OpenRouter has been observed returning a 200 with choices=None for some
    # providers; guard before indexing so this surfaces as a classified,
    # retryable error rather than a bare TypeError.
    if not resp.choices:
        raise LLMEmptyResponseError(
            f"model={model} resp_model={getattr(resp, 'model', '?')} returned no choices"
        )
    choice = resp.choices[0]
    usage = resp.usage
    logger.info(
        "llm call: model=%s resp_model=%s finish_reason=%s "
        "tokens=prompt:%s,completion:%s,total:%s cost=%s",
        model,
        resp.model,
        choice.finish_reason,
        usage.prompt_tokens if usage else None,
        usage.completion_tokens if usage else None,
        usage.total_tokens if usage else None,
        getattr(usage, "cost", None) if usage else None,
    )
    if choice.finish_reason not in ("stop", None):
        logger.warning(
            "llm call: model=%s finished with reason=%r (possible truncation)",
            model,
            choice.finish_reason,
        )
    if usage_sink is not None:
        usage_sink.append(
            {
                "model": model,
                "resp_model": resp.model,
                "finish_reason": choice.finish_reason,
                "prompt_tokens": usage.prompt_tokens if usage else None,
                "completion_tokens": usage.completion_tokens if usage else None,
                "total_tokens": usage.total_tokens if usage else None,
                "cost": getattr(usage, "cost", None) if usage else None,
            }
        )
    content = choice.message.content or ""
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
# Holding-relevant news (R-3): recall over the captured window + targeted search
# ---------------------------------------------------------------------------

# Cap on anomaly holdings that get a targeted live search when the captured
# store has nothing for them (the INTC-Google-foundry miss: a window-relevant
# story that the RSS sources never carried). Bounded to protect the Tavily daily
# budget; ordered by |move| so the most-moved holdings are covered first.
_MAX_TARGETED_ANOMALY_SEARCHES = 3


def _targeted_anomaly_queries(
    anomalies: list[dict[str, Any]],
    covered: set[str],
    max_n: int = _MAX_TARGETED_ANOMALY_SEARCHES,
) -> list[tuple[str, str]]:
    """Build (identifier, query) for the most-moved anomaly holdings that have NO
    recalled window news, so a targeted live search can explain the move.

    Holdings are holdings-derived, so this runs after Pass 1 (Pass 1 stays
    holdings-free) and its results are supplied only to Pass 2. `anomalies` is
    already sorted by largest |move| upstream.
    """
    queries: list[tuple[str, str]] = []
    for a in anomalies:
        ident = a.get("identifier", "")
        if not ident or ident in covered:
            continue
        # Funds and non-US tickers tend to have no clean English news query;
        # still try with the name, but the ticker drives recall precision.
        name = a.get("name", "")
        label = f"{name} {ident}".strip()
        queries.append((ident, f"{label} stock news catalyst"))
        if len(queries) >= max_n:
            break
    return queries


def _build_holding_news_block(holding_news: dict[str, list[dict[str, Any]]]) -> str:
    """Render the HOLDING-RELEVANT NEWS section of the Pass 2 prompt.

    Empty input → empty string (no section emitted).
    """
    if not holding_news:
        return ""
    lines = ["", "=== HOLDING-RELEVANT NEWS (per moved holding) ==="]
    for ident, items in holding_news.items():
        lines.append(f"{ident}:")
        for it in items:
            src = it.get("source", "")
            title = it.get("title", "")
            lines.append(f"  [{src}] {title}")
            summary = it.get("summary")
            if summary:
                lines.append(f"    {summary[:300]}")
    return "\n".join(lines)


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


def _f(x: Decimal | None) -> float | None:
    return float(x) if x is not None else None


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
            "trigger": a.trigger,
            "market": a.market,
            "baseline_date": a.baseline_date.isoformat() if a.baseline_date else None,
            "latest_date": a.latest_date.isoformat() if a.latest_date else None,
            "window_net_pct": _f(a.window_net_pct),
            "max_day_pct": _f(a.max_day_pct),
            "max_day_date": a.max_day_date.isoformat() if a.max_day_date else None,
            "prev_close": _f(a.prev_close),
            "day_open": _f(a.day_open),
            "day_high": _f(a.day_high),
            "day_low": _f(a.day_low),
            "day_close": _f(a.day_close),
            "after_hours": _f(a.after_hours),
        }
        for a in anomalies
    ]


def _serialize_technical(positions: list[TechnicalPosition]) -> list[dict[str, Any]]:
    return [
        {
            "ticker": p.ticker,
            "name": p.name,
            "last_close": p.last_close,
            "bars": p.bars,
            "pct_vs_sma50": p.pct_vs_sma50,
            "pct_vs_sma200": p.pct_vs_sma200,
            "range_52w_low": p.range_52w_low,
            "range_52w_high": p.range_52w_high,
            "pct_in_52w_range": p.pct_in_52w_range,
            "vol_20d_annualized": p.vol_20d_annualized,
        }
        for p in positions
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
            "broker": hv.broker,
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


def _fmt_anomaly_arc(a: dict[str, Any]) -> str:
    """One detailed line per anomaly: window net, worst day, and the latest
    trading day's session arc, with explicit comparison points so the report can
    state what was compared to what (#7/#10)."""
    parts = [f"  {a['name']} ({a['identifier']}) [{a.get('market', '')}/{a.get('asset_type', '')}]"]
    net = a.get("window_net_pct")
    if net is not None:
        parts.append(
            f"    window net {net * 100:+.2f}% "
            f"(baseline close {a.get('baseline_date')} -> latest close {a.get('latest_date')})"
        )
    mday = a.get("max_day_pct")
    if mday is not None:
        parts.append(f"    worst single day {mday * 100:+.2f}% on {a.get('max_day_date')}")
    # Latest trading day session arc.
    pc, op, hi, lo, cl, ah = (
        a.get("prev_close"),
        a.get("day_open"),
        a.get("day_high"),
        a.get("day_low"),
        a.get("day_close"),
        a.get("after_hours"),
    )
    arc = []
    if pc is not None:
        arc.append(f"prev close {pc:g}")
    if op is not None and pc:
        arc.append(f"open {op:g} ({(op / pc - 1) * 100:+.1f}% gap)")
    if hi is not None and lo is not None:
        arc.append(f"intraday {lo:g}-{hi:g}")
    if cl is not None:
        arc.append(f"close {cl:g}")
    if ah is not None and cl:
        arc.append(f"after-hours {ah:g} ({(ah / cl - 1) * 100:+.1f}%)")
    if arc:
        parts.append(f"    latest day ({a.get('latest_date')}): " + "; ".join(arc))
    parts.append(f"    trigger: {a.get('trigger', '')}")
    return "\n".join(parts)


def _build_section42_table(anomalies: list[dict[str, Any]]) -> str:
    """Build the §4.2 price-anomaly table from data — no LLM (#3).

    The numeric session arc already lives in `report_inputs.price_anomalies`; a
    code-built table is deterministic, token-free, and cannot hallucinate a
    number. The LLM writes only the one-line driver per holding underneath
    (see the §4.2 prompt instruction). English headers are rendered to the
    output language by the translation pass, same as §1.
    """

    def pct(x: float | None) -> str:
        return f"{x * 100:+.2f}%" if x is not None else "—"

    def num(x: float | None) -> str:
        return f"{x:g}" if x is not None else "—"

    lines: list[str] = [
        "| Holding | Net % | Worst day (date) | Prev close | Open (gap %) "
        "| Intraday range | Close | After-hrs | Trigger |",
        "|---------|-------|------------------|------------|--------------"
        "|----------------|-------|-----------|---------|",
    ]
    for a in anomalies:
        ident = a.get("identifier", "")
        name_col = f"{a.get('name', '')} ({ident})" if ident else a.get("name", "")

        wd = a.get("max_day_date")
        worst = pct(a.get("max_day_pct"))
        worst_col = f"{worst} ({wd})" if a.get("max_day_pct") is not None and wd else worst

        pc, op = a.get("prev_close"), a.get("day_open")
        open_col = f"{op:g} ({(op / pc - 1) * 100:+.1f}%)" if op is not None and pc else num(op)

        lo, hi = a.get("day_low"), a.get("day_high")
        range_col = f"{lo:g}-{hi:g}" if lo is not None and hi is not None else "—"

        cl, ah = a.get("day_close"), a.get("after_hours")
        ah_col = f"{ah:g} ({(ah / cl - 1) * 100:+.1f}%)" if ah is not None and cl else num(ah)

        lines.append(
            f"| {name_col} | {pct(a.get('window_net_pct'))} | {worst_col} "
            f"| {num(pc)} | {open_col} | {range_col} | {num(cl)} | {ah_col} "
            f"| {a.get('trigger', '')} |"
        )
    return "\n".join(lines)


def _inject_section42_table(body: str, table: str) -> str:
    """Insert the code-built §4.2 anomaly table right after the §4.2 heading the
    LLM emits, above its one-line drivers (#3). Falls back to appending a §4.2
    block when the heading is absent so the table is never dropped silently."""
    out: list[str] = []
    injected = False
    for line in body.split("\n"):
        out.append(line)
        if not injected and line.lstrip().startswith("### 4.2"):
            out += ["", table, ""]
            injected = True
    if not injected:
        out += ["", "### 4.2 Price anomalies", "", table, ""]
    return "\n".join(out)


def _build_section44_technical(positions: list[dict[str, Any]]) -> str:
    """Build the §4.4 technical-position block from data — no LLM (#4).

    Pure description of where each holding's price sits: distance to its 50/200-day
    average, position inside the trailing 52-week range, recent realized volatility.
    No signals, no actions. Cells with insufficient captured history render as "—".
    If no holding has any computed metric yet (capture layer still warming up /
    backfill not run), a single explanatory line replaces the table.
    """

    def pct(x: float | None) -> str:
        return f"{x * 100:+.1f}%" if x is not None else "—"

    def rng(x: float | None) -> str:
        return f"{x * 100:.0f}%" if x is not None else "—"

    rows = [p for p in positions if p.get("ticker")]
    if not any(
        p.get("pct_vs_sma50") is not None
        or p.get("pct_vs_sma200") is not None
        or p.get("pct_in_52w_range") is not None
        or p.get("vol_20d_annualized") is not None
        for p in rows
    ):
        return (
            "### 4.4 Technical position\n\n"
            "Insufficient captured price history to compute technical position "
            "(run the one-year OHLCV backfill; metrics populate as the capture "
            "layer accumulates closes)."
        )

    lines = [
        "### 4.4 Technical position",
        "",
        "| Holding | vs 50-day avg | vs 200-day avg | 52-week range position "
        "| 20-day volatility (ann.) |",
        "|---------|---------------|----------------|-------------------------"
        "|--------------------------|",
    ]
    for p in rows:
        ident = p.get("ticker", "")
        name_col = f"{p.get('name', '')} ({ident})" if ident else p.get("name", "")
        lines.append(
            f"| {name_col} | {pct(p.get('pct_vs_sma50'))} | {pct(p.get('pct_vs_sma200'))} "
            f"| {rng(p.get('pct_in_52w_range'))} | {pct(p.get('vol_20d_annualized'))} |"
        )
    lines += [
        "",
        "> Range position: 0% = at the 52-week low, 100% = at the 52-week high. "
        "Figures describe where the price sits; they are not signals.",
    ]
    return "\n".join(lines)


# Exposure mapping for the forward calendar (#1) — code-built, deterministic.
_RATE_SENSITIVE_SECTORS = {
    "Technology",
    "Consumer Discretionary",
    "Communication Services",
    "Real Estate",
}
_CONSUMER_SECTORS = {"Consumer Discretionary", "Consumer Staples"}
_GOLD_TICKERS = {"GLD", "IAU", "GLDM", "SGOL", "GLDX"}
# RSS-derived delay caveat triggers (#1): a funding lapse can suspend BLS/BEA releases.
_RELEASE_DELAY_TERMS = (
    "government shutdown",
    "funding lapse",
    "funding gap",
    "appropriations lapse",
    "budget shutdown",
    "政府停摆",
    "拨款中断",
    "停摆",
)


def _is_gold(h: dict[str, Any]) -> bool:
    ticker = (h.get("ticker") or "").upper()
    return "gold" in (h.get("name") or "").lower() or ticker in _GOLD_TICKERS


def _us_equity(h: dict[str, Any]) -> bool:
    return h.get("market") == "US" and h.get("asset_type") in ("stock", "etf")


def _forward_exposure(
    event: dict[str, Any], holdings: list[dict[str, Any]]
) -> tuple[list[str], str]:
    """Map a scheduled event to the holdings exposed to it + what to watch.

    Pure facts and observation framing — never a directional forecast.
    """
    if event.get("event_type") == "earnings":
        tk = event.get("ticker", "")
        exposed = [h["name"] for h in holdings if h.get("ticker") == tk]
        return (exposed or [tk]), "reported results vs expectations for this holding"

    low = event.get("name", "").lower()
    if "fomc" in low:
        return (
            [
                h["name"]
                for h in holdings
                if _us_equity(h) and (h.get("sector") in _RATE_SENSITIVE_SECTORS or _is_gold(h))
            ],
            "policy-rate decision and statement tone",
        )
    if any(k in low for k in ("cpi", "ppi", "pce", "personal income")):
        return (
            [
                h["name"]
                for h in holdings
                if _us_equity(h) and (h.get("sector") in _RATE_SENSITIVE_SECTORS or _is_gold(h))
            ],
            "inflation reading vs consensus; rate-path implications",
        )
    if "payroll" in low or "employment" in low:
        return (
            [h["name"] for h in holdings if _us_equity(h)],
            "labor-market strength; rate-path implications",
        )
    if "retail" in low:
        return (
            [h["name"] for h in holdings if h.get("sector") in _CONSUMER_SECTORS],
            "consumer-spending momentum",
        )
    if "sentiment" in low:
        return (
            [h["name"] for h in holdings if h.get("sector") in _CONSUMER_SECTORS],
            "household-sentiment trend",
        )
    if "gross domestic" in low or "gdp" in low:
        return ([h["name"] for h in holdings if _us_equity(h)], "growth pace vs consensus")
    return ([h["name"] for h in holdings if _us_equity(h)], "relevance to US-exposed holdings")


def _forward_delay_risk(news_items: list[dict[str, Any]]) -> bool:
    """True if window news mentions a funding lapse that could delay US releases."""
    for it in news_items:
        text = f"{it.get('title', '')} {it.get('summary') or ''}".lower()
        if any(term in text for term in _RELEASE_DELAY_TERMS):
            return True
    return False


def _build_forward_block(
    events: list[dict[str, Any]],
    holdings: list[dict[str, Any]],
    news_items: list[dict[str, Any]],
    report_date_str: str = "",
) -> str:
    """Build §2.5 Forward Calendar from data — no LLM (#1).

    Lists scheduled US macro releases + held-company earnings in the forward
    window and maps each to the holdings carrying exposure. Calendar facts only;
    the 'Watch' column points to an observation, never a predicted outcome. If
    window news flags a funding lapse, a delay caveat is appended. Events dated
    the report's own day are tagged '(today)' and also promoted to a §2 lead note
    (R-6).
    """
    lines = [
        "## §2.5 Forward Calendar",
        "",
        "Scheduled US events in the days ahead and the holdings exposed to each. "
        "These are calendar facts, not forecasts.",
        "",
        "| Date | Event | Exposed holdings | Watch |",
        "|------|-------|------------------|-------|",
    ]
    for e in events:
        exposed, watch = _forward_exposure(e, holdings)
        if len(exposed) > 3:
            shown = ", ".join(exposed[:3]) + f" +{len(exposed) - 3}"
        else:
            shown = ", ".join(exposed) if exposed else "—"
        date_str = str(e.get("scheduled_date", ""))
        date_cell = f"{date_str} (today)" if date_str[:10] == report_date_str else date_str
        lines.append(f"| {date_cell} | {e.get('name', '')} | {shown} | {watch} |")
    if _forward_delay_risk(news_items):
        lines += [
            "",
            "> Note: window news references a government funding lapse, which can "
            "delay scheduled BLS/BEA releases; listed dates may slip.",
        ]
    return "\n".join(lines)


def _inject_forward_block(body: str, block: str) -> str:
    """Insert the §2.5 forward block before §3 (#1); fallback before §4 or append."""
    for anchor in ("## §3", "## §4"):
        idx = body.find(anchor)
        if idx != -1:
            return body[:idx].rstrip() + "\n\n" + block + "\n\n" + body[idx:]
    return body.rstrip() + "\n\n" + block


def _build_today_events_block(
    events: list[dict[str, Any]], holdings: list[dict[str, Any]], report_date_str: str
) -> str:
    """Promote events scheduled for the report's own date to a §2 lead note (R-6).

    A forward calendar entry whose date == the report date is happening today, not
    "ahead": leaving it only in the §2.5 forward table understates it. This lifts
    it to the top of §2 as a calendar fact. The report's price data stops at the
    prior close (see R-5), so any same-day release's result is by construction not
    reflected here — stated, never forecast. Empty input → empty string.
    """
    today = [e for e in events if str(e.get("scheduled_date", ""))[:10] == report_date_str]
    if not today:
        return ""
    lines = [
        "**Today's scheduled events** (calendar facts; results not yet in this report's data):"
    ]
    for e in today:
        exposed, watch = _forward_exposure(e, holdings)
        shown = ", ".join(exposed[:3]) + (f" +{len(exposed) - 3}" if len(exposed) > 3 else "")
        tail = f" — exposed: {shown}" if exposed else ""
        lines.append(f"- {e.get('name', '')}{tail}. {watch}")
    return "\n".join(lines)


def _inject_today_events(body: str, block: str) -> str:
    """Insert the today-events note directly under the '## §2' heading (R-6)."""
    anchor = "## §2"
    idx = body.find(anchor)
    if idx == -1:
        return block + "\n\n" + body
    eol = body.find("\n", idx)
    if eol == -1:
        return body + "\n\n" + block
    return body[: eol + 1] + "\n" + block + "\n" + body[eol + 1 :]


def _build_pass2_prompt(
    portfolio: dict[str, Any],
    macro: dict[str, Any],
    anomalies: list[dict[str, Any]],
    search_results: list[dict[str, Any]],
    period_start: str = "",
    period_end: str = "",
    trading_days: int = 0,
    holding_news: dict[str, list[dict[str, Any]]] | None = None,
) -> str:
    lines: list[str] = []

    # Report window facts — anchor all time references to "this report period".
    lines.append("=== REPORT WINDOW ===")
    span = "unknown"
    if period_start and period_end:
        span = f"{period_start[:16].replace('T', ' ')} to {period_end[:16].replace('T', ' ')} UTC"
    lines.append(f"This report covers {span} ({trading_days} trading day(s)).")
    lines.append("")

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

    # Price anomalies (window net + worst single day + latest-day session arc)
    lines.append("")
    lines.append("=== PRICE ANOMALIES (over the report window) ===")
    if anomalies:
        for a in anomalies:
            lines.append(_fmt_anomaly_arc(a))
    else:
        lines.append("(no holding moved beyond its threshold this window)")

    # Holding-relevant news (R-3): captured-window recall + targeted search for
    # the holdings that moved. Distinct from BACKGROUND RESEARCH (macro-themed).
    block = _build_holding_news_block(holding_news or {})
    if block:
        lines.append(block)

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
        "Write sections §2, §3, and §4 of the financial analysis briefing in Markdown.\n"
        "Use the portfolio and signal data above. Do NOT emit bracketed tags, "
        "citations, or per-sentence disclaimers — write clean prose.\n\n"
        "TIME REFERENCES: this is an incremental report over the window stated above. "
        "Refer to events as happening 'in this report period' unless an event "
        "demonstrably occurred on one specific day (then name the date). Never write "
        "'today' or 'this week' as a stand-in for the window.\n\n"
        "CONFIDENCE LABELS: end every causal attribution (in §3 and §4.2) with one "
        "evidence-ordinal label in square brackets — NEVER a numeric percentage:\n"
        "  [Established] — a named mechanism or a citable event drives the move "
        "(e.g. gold up as real yields fell — an identity between real rates and "
        "non-yielding assets; a stock up on a confirmed earnings beat).\n"
        "  [Probable] — partial evidence points to a driver but it is not conclusive.\n"
        "  [Speculative] — no direct evidence; the attribution is a hypothesis "
        "(e.g. an unexplained gap with no identifiable catalyst).\n"
        "The label expresses how sure you are about the PAST move's CAUSE — it is NOT "
        "a view on future direction. Do not drop or downgrade a large unexplained "
        "move: label it [Speculative], keep it brief, and say the catalyst is "
        "unidentified — an unexplained move is itself worth noting.\n\n"
        "## §2 Macro Signals\n"
        "For each triggered macro theme: (a) describe what is happening and what is "
        "driving it; (b) under a bold sub-heading 'Impact on this portfolio', do NOT "
        "stop at naming exposed tickers — trace the transmission mechanism (signal -> "
        "channel -> the specific holding), then separate the read into short-term "
        "(this period / next few sessions), medium-term (weeks to a quarter), and "
        "long-term (structural) effects, and end with the concrete follow-on signals "
        "or scenarios worth watching for each named holding. Stay descriptive: report "
        "what to WATCH, never what to DO (no buy/sell/hold/hedge/trim language). The "
        "short-term read describes a CHANNEL ('X would transmit via Y'), not an "
        "observed move — see DIRECTION REQUIRES EVIDENCE / DIVERGENCE IS THE SIGNAL "
        "above; check PRICE ANOMALIES before stating a holding already moved a "
        "given direction.\n\n"
        "## §3 Holdings Analysis\n"
        "Select the holdings most affected this period and, for each, go beyond "
        "'position size + what happened'. Explain WHY it surfaced (which signal/move "
        "implicates it), the mechanism linking the development to that specific "
        "holding, how it sits relative to the rest of the portfolio (concentration, "
        "correlation, currency), and which forward signals would confirm or dissolve "
        "the thesis. Depth over breadth — a few holdings analysed well beats a list. "
        "End each causal attribution with its confidence label (see CONFIDENCE "
        "LABELS above).\n\n"
        "## §4 Risk Radar\n"
        "### 4.1 Concentration — state the flagged ratios.\n"
        "### 4.2 Price anomalies — a numeric table (net %, worst day, the latest-day "
        "session arc, trigger) is inserted by the system directly under this heading; "
        "do NOT restate those numbers. Under the heading write ONE line per holding in "
        "PRICE ANOMALIES, formatted 'IDENTIFIER — <driver> [Label]', where <driver> is "
        "a single sentence attributing the move to a development from the research/news "
        "and [Label] is the confidence label (see CONFIDENCE LABELS above). If no "
        "catalyst is identifiable, say so plainly and label it [Speculative]; never "
        "invent one. If PRICE ANOMALIES is empty, say so plainly for this window — do "
        "NOT phrase it as 'today'.\n"
        "### 4.3 FX exposure — state currency exposures and any FX note.\n"
        "Throughout §4: state the numbers; never editorialize about what to do."
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
        "| Holding | Currency | Value | % Portfolio | Custodian | Sector |",
        "|---------|----------|-------|-------------|-----------|--------|",
    ]

    holdings = list(portfolio.get("holdings", []))
    # Group by custodian (holding institution), preserving the user's upload
    # order: institutions appear in the order they first show up in the file,
    # holdings within an institution keep their file order. Cash sits inside its
    # own institution. A holding with no declared institution falls into "Other".
    # Each group gets a subtotal so per-institution capital is legible.
    group_order: list[str] = []
    groups: dict[str, list[dict[str, Any]]] = {}
    for h in sorted(
        holdings,
        key=lambda x: x["position"] if x.get("position") is not None else 1_000_000,
    ):
        broker = h.get("broker") or "Other"
        if broker not in groups:
            groups[broker] = []
            group_order.append(broker)
        groups[broker].append(h)

    for broker in group_order:
        members = groups[broker]
        subtotal_base = sum(m.get("market_value_base", 0) for m in members)
        for h in members:
            mv = h.get("market_value", 0)
            mv_base = h.get("market_value_base", 0)
            ratio = mv_base / total if total > 0 else 0
            name_col = h["name"] + (f" ({h['ticker']})" if h.get("ticker") else "")
            lines.append(
                f"| {name_col} | {h.get('currency', '')} | {mv:,.0f} | {ratio:.1%} "
                f"| {h.get('broker', '') or '—'} | {h.get('sector', '—') or '—'} |"
            )
        sub_ratio = subtotal_base / total if total > 0 else 0
        lines.append(
            f"| **{broker} subtotal** | {base_ccy} | **{subtotal_base:,.0f}** "
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
    "This report is generated automatically by an AI large language model (LLM) system. "
    "AI-generated content may contain imprecise, incomplete, or inaccurate language and "
    "does not represent the views or opinions of the sender. "
    "This report is provided for informational purposes only. "
    "It does not constitute investment advice, a recommendation to buy or sell any "
    "security, or a solicitation of any investment. "
    "Past performance is not indicative of future results. "
    "The sender accepts no responsibility for any decisions made based on this content. "
    "Always consult a qualified financial advisor before making investment decisions."
)

_DISCLAIMER_ZH = (
    "本报告由AI大型语言模型（LLM）系统自动生成。"  # noqa: RUF001
    "AI生成内容可能包含不精确、不完整或不准确的措辞，不代表发送方的观点或意见。"  # noqa: RUF001
    "本报告仅供参考，不构成投资建议、证券买卖推荐或投资招揽。"  # noqa: RUF001
    "历史业绩不代表未来表现。"
    "对于基于本内容所作出的任何决策，发送方不承担任何责任。"  # noqa: RUF001
    "在做出任何投资决策前，请咨询持牌财务顾问。"  # noqa: RUF001
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
        f"**Exchange rates:** As of {fx_date} (daily closing rates from yfinance). "
        f"All portfolio valuations are converted to {base_ccy} using these rates. "
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


# Stray provenance/disclaimer tags the LLM may still emit despite the system
# prompt forbidding them. They are pure noise in the rendered report (the
# S-numbers don't resolve, the per-line disclaimer duplicates the footer), so we
# strip them as a backstop. Covers EN and the zh tags produced before #9.
_STRAY_TAGS = [
    re.compile(re.escape(_COMPLIANCE_MARKER)),
    re.compile(r"\[(?:新闻|分析|行情|宏观主题数据|news|analysis|market data)\]", re.IGNORECASE),
]

# A line the model emits as its own disclaimer / legal notice. The single
# disclaimer lives in the template footer (F3), so any disclaimer inside the body
# is both redundant and a false-positive trigger for the forbidden-output scan
# (it legitimately contains "投资建议"/"investment advice"). These phrases appear
# only in disclaimers, never in factual market prose, so dropping the whole line
# is safe. Runs on the body only — the footer is appended afterwards, untouched.
_BODY_DISCLAIMER_RE = re.compile(
    r"投资建议|投资招揽|买卖(推荐|指令)|免责声明|不构成.*(建议|招揽)"
    r"|informational purposes|not\s+constitute\s+investment|investment advice"
    r"|not\s+a\s+recommendation|consult\s+a\s+qualified",
    re.IGNORECASE,
)


def _strip_body_disclaimer(text: str) -> str:
    """Drop whole lines that are the model's own disclaimer, plus the orphaned
    blank lines / horizontal rules left behind.

    Applied both before translation (on the Pass 2 body) and AFTER translation:
    the translator (a separate, cheaper model) sometimes re-introduces a bilingual
    disclaimer of its own, which the pre-translation pass cannot have caught.
    """
    kept = [ln for ln in text.split("\n") if not _BODY_DISCLAIMER_RE.search(ln)]
    while kept and (kept[-1] == "" or kept[-1].strip() == "---"):
        kept.pop()
    return "\n".join(kept)


def _strip_markers(text: str) -> str:
    """Remove bracketed citations / provenance tags / model-emitted disclaimers (#9).

    Markers are no longer injected: the report carries one bilingual disclaimer in
    the footer, and inline tags hurt readability. This drops any that the model
    emits anyway (including a self-written disclaimer paragraph), then tidies the
    whitespace and orphaned rules they leave behind.
    """
    text = _NEWS_RUN_RE.sub("", text)
    for pat in _STRAY_TAGS:
        text = pat.sub("", text)
    # Collapse the runs of spaces / dangling separators the removals leave.
    lines = [re.sub(r"[ \t]{2,}", " ", ln).rstrip() for ln in text.split("\n")]
    lines = [re.sub(r"\s+([.,;:。，；：])", r"\1", ln) for ln in lines]  # noqa: RUF001
    # Drop the model's own disclaimer lines and trailing orphaned rules.
    return _strip_body_disclaimer("\n".join(lines))


# ---------------------------------------------------------------------------
# Assembly / rendering (shared by live generation and re-render)
# ---------------------------------------------------------------------------


def _fx_is_stale(fx_date: str, period_end: str) -> bool:
    """True when the FX rate date trails the window cutoff by more than a day.

    Both are dates we control (fx_rates.rate_date / period_end); a >1-day gap
    means valuations used a materially old rate — worth flagging in a volatile
    week (R-4 surfaced rates frozen 6 days). Any parse failure → not flagged.
    """
    try:
        fx = date.fromisoformat(fx_date[:10])
        end = datetime.fromisoformat(period_end).astimezone(ET).date()
    except (ValueError, TypeError):
        return False
    return (end - fx).days > 1


def _build_data_window(
    news_items: list[dict[str, Any]],
    portfolio: dict[str, Any],
    period_start: str,
    period_end: str,
    trading_days: int,
    price_data_through: str = "",
) -> str:
    """A one-line statement of the intel/data interval this report covers (#5/R-5)."""
    if period_start and period_end:
        span = f"{period_start[:16].replace('T', ' ')} to {period_end[:16].replace('T', ' ')} UTC"
        td = f"{trading_days} trading day(s)"
        window_line = f"since last report: {span} ({td})"
    else:
        window_line = "window unavailable"
    fx_date = portfolio.get("fx_date", "n/a")
    # R-5: state where PRICE data actually stops. The capture layer only takes
    # closes at session nodes, so a premarket/intraday run has no quotes past the
    # prior close — say so rather than letting the wall-clock window imply it.
    price_line = (
        f" Price data through the {price_data_through} close (session-close "
        "snapshots only — no premarket or intraday quotes)."
        if price_data_through
        else ""
    )
    fx_flag = (
        " [!] FX rate is stale relative to the window cutoff."
        if _fx_is_stale(str(fx_date), period_end)
        else ""
    )
    return (
        f"> **Data window** — {window_line}; {len(news_items)} news item(s); "
        f"FX as of {fx_date}.{price_line} Price moves are measured against the "
        f"baseline close at the window start.{fx_flag}\n\n"
    )


def _header_timestamp(report_date_str: str, period_end: str) -> str:
    """Title timestamp: 'YYYY-MM-DD HH:MM ET' from the window cutoff (#1).

    Falls back to the bare date when period_end is unavailable (e.g. legacy
    re-render of a report that predates the window columns).
    """
    if not period_end:
        return report_date_str
    try:
        end_et = datetime.fromisoformat(period_end).astimezone(ET)
    except ValueError:
        return report_date_str
    return end_et.strftime("%Y-%m-%d %H:%M ET")


# A translated chunk shorter than this fraction of its source (when the source is
# non-trivial) is treated as a truncated/dropped response.
# Conservative: Chinese renders in roughly half the characters of English, so the
# threshold only catches near-empty / grossly truncated returns, not dense prose.
_TRANSLATION_MIN_RATIO = 0.25
_TRANSLATION_MIN_SOURCE_CHARS = 200

# Pause between per-chunk translation calls. A full report is ~14 chunks, each up
# to 2 calls (retry on truncation) — all against LOW_COST_LLM_MODEL. Spacing them
# out reduces the odds of tripping a shared per-model rate limit on the upstream
# provider pool (observed: 429 'temporarily rate-limited upstream' on
# deepseek-v4-flash after repeated full-pipeline runs).
_TRANSLATION_PACING_SECONDS = 2.0

# Provider preference for translation calls (deepseek-v4-flash). Distinct from
# OPENROUTER_PROVIDER_ORDER (DigitalOcean,Venice — used for pinned Pass1/Pass2
# calls): chosen 2026-06-10 after DigitalOcean+Venice were both observed
# 429 'temporarily rate-limited upstream' on this model. allow_fallbacks=True
# (set in _call_llm) still lets OpenRouter fall back beyond this list if both
# are unavailable.
_TRANSLATION_PROVIDER_ORDER = ["Cloudflare", "Morph"]


def _translate_md(md: str, target_lang: str) -> str:
    """Translate an assembled report to *target_lang* (#8).

    The LLM reasons in English upstream; this renders the final text in the
    user's language. Tickers, numbers, and table structure are preserved
    verbatim. 'en' is a no-op (the canonical language).

    Translation runs on the LOW_COST model, not PRIMARY: it is a mechanical
    render of already-reasoned text, so the cheaper model is sufficient and the
    expensive analysis model is reserved for Pass 2.
    """
    if target_lang == "en":
        return md
    lang_name = {"zh": "Simplified Chinese"}.get(target_lang, target_lang)
    glossary = {
        "zh": (
            ' Use this exact glossary for fixed terms: "Portfonia Financial Analysis '
            'Report" -> "Portfonia 财经分析报告"; "financial analysis briefing" -> '
            '"财经分析简报"; "Portfolio Snapshot" -> "投资组合快照"; "Holdings Analysis" '
            '-> "持仓分析"; "Custodian" -> "持仓机构"; "Macro Signals" -> "宏观信号"; '
            '"Risk Radar" -> "风险雷达"; "[Established]" -> "[确定]"; '
            '"[Probable]" -> "[较可能]"; "[Speculative]" -> "[推测]". '
            'Never render any word as "智能".'
        )
    }.get(target_lang, "")
    settings = get_settings()
    system = (
        "You are a professional financial translator. Translate the user's Markdown "
        f"report into {lang_name}. STRICT RULES: preserve all Markdown structure, "
        "tables, and numbers exactly; keep ticker symbols, fund codes, and currency "
        "codes verbatim. Translate only natural-language prose. Do not add, remove, "
        "or reorder content, and never introduce advisory or recommendation language." + glossary
    )
    client = _openrouter_client()
    # Translate one (sub)section at a time. A single whole-report request is large
    # enough that the provider intermittently disconnects mid-response or returns a
    # truncated 200; per-(sub)section requests are small and reliable. Chunks are
    # reassembled with the exact original separators, so structure is preserved.
    chunks = _split_sections(md)
    parts: list[str] = []
    for i, chunk in enumerate(chunks):
        if not chunk.strip():
            parts.append(chunk)
            continue
        if i > 0:
            time.sleep(_TRANSLATION_PACING_SECONDS)
        parts.append(_translate_chunk(client, settings.LOW_COST_LLM_MODEL, system, chunk))
    return "\n".join(parts)


def _translate_chunk(client: openai.OpenAI, model: str, system: str, chunk: str) -> str:
    """Translate one chunk, guarding against the model silently dropping content.

    A successful HTTP 200 can still carry a truncated body (provider mid-response
    cut-off, common on the rate-limited free tier). Chinese is denser than English
    so some shrinkage is expected, but a chunk that comes back far shorter than its
    source has almost certainly lost content. Retry once; if it is still short, keep
    the English source for that chunk — a complete English section beats a silently
    dropped one (e.g. §3 vanishing from the report).

    `provider_order=_TRANSLATION_PROVIDER_ORDER`: translation is a mechanical
    render on the low-cost model, steered toward providers other than the
    pinned Pass1/Pass2 pair (which has been observed rate-limited for this
    model independently of these calls).
    """

    def _short(out: str) -> bool:
        return len(chunk) >= _TRANSLATION_MIN_SOURCE_CHARS and len(
            out.strip()
        ) < _TRANSLATION_MIN_RATIO * len(chunk)

    out = _call_llm(
        client,
        model,
        system,
        chunk,
        with_holdings=True,
        pin_provider=False,
        provider_order=_TRANSLATION_PROVIDER_ORDER,
    )
    if _short(out):
        logger.warning(
            "translation chunk looked truncated (%d->%d chars); retrying", len(chunk), len(out)
        )
        time.sleep(_TRANSLATION_PACING_SECONDS)
        out = _call_llm(
            client,
            model,
            system,
            chunk,
            with_holdings=True,
            pin_provider=False,
            provider_order=_TRANSLATION_PROVIDER_ORDER,
        )
    if _short(out):
        logger.error("translation chunk still truncated after retry; keeping source for this chunk")
        return chunk
    return out


def _split_sections(md: str) -> list[str]:
    """Split a report into translation chunks at section AND subsection headings.

    Breaks at both top-level ('## ') and subsection ('### ') headings, so §4 —
    which now carries two large code-built tables (§4.2 anomalies, §4.4 technical)
    plus prose subsections — becomes several small chunks instead of one oversized
    request. Smaller chunks keep the cheap, occasionally rate-limited translation
    model from returning a truncated 200 that silently drops content. The preamble
    before the first heading is its own chunk; joining the chunks with a single
    newline reproduces the original document.
    """
    chunks: list[str] = []
    current: list[str] = []
    for line in md.split("\n"):
        if (line.startswith("## ") or line.startswith("### ")) and current:
            chunks.append("\n".join(current))
            current = [line]
        else:
            current.append(line)
    if current:
        chunks.append("\n".join(current))
    return chunks


def _render_full_md(
    report_date_str: str,
    portfolio: dict[str, Any],
    news_items: list[dict[str, Any]],
    raw_body: str,
    output_lang: str,
    period_start: str = "",
    period_end: str = "",
    trading_days: int = 0,
    anomalies: list[dict[str, Any]] | None = None,
    technical: list[dict[str, Any]] | None = None,
    forward_events: list[dict[str, Any]] | None = None,
    price_data_through: str = "",
) -> tuple[str, list[str], str]:
    """Annotate, assemble, language-render, and compliance-scan a report.

    Returns (full_markdown, violations, translated_body). The third element is
    the translated dynamic section (pre-footer) for compliance audit traceability.
    Pure function of its inputs — this is what makes #6 re-render possible.
    """
    cleaned = _strip_markers(raw_body)
    # §2.5 forward calendar is code-built from stored events + holdings (#1) and
    # inserted before §3: calendar facts mapped to exposed holdings, no forecast.
    if forward_events:
        cleaned = _inject_forward_block(
            cleaned,
            _build_forward_block(
                forward_events, portfolio.get("holdings", []), news_items, report_date_str
            ),
        )
        # R-6: events scheduled for the report's own date are no longer "forward"
        # — promote them to a "today" note at the top of §2 (calendar fact only,
        # results not yet in this report's data).
        today_block = _build_today_events_block(
            forward_events, portfolio.get("holdings", []), report_date_str
        )
        if today_block:
            cleaned = _inject_today_events(cleaned, today_block)
    # §4.2 numeric table is code-built from the stored anomalies and inserted
    # under the LLM's §4.2 heading (#3): deterministic, token-free, no hallucination.
    if anomalies:
        cleaned = _inject_section42_table(cleaned, _build_section42_table(anomalies))
    # §4.4 technical position appended to the §4 body (#4) — also code-built from
    # stored metrics, so re-render reproduces it without touching the DB.
    if technical:
        cleaned = cleaned.rstrip() + "\n\n" + _build_section44_technical(technical)
    header = f"# Portfonia Financial Analysis Report — {_header_timestamp(report_date_str, period_end)}\n\n"
    window = _build_data_window(
        news_items, portfolio, period_start, period_end, trading_days, price_data_through
    )
    section1 = _build_section1(portfolio)
    dynamic_en = header + window + section1 + "\n\n" + cleaned

    # Compliance scan on the English canonical first (highest-signal blacklist).
    violations = _scan_forbidden_output(cleaned)

    dynamic_out = _translate_md(dynamic_en, output_lang)
    # The translator can re-add its own disclaimer paragraph (it runs after the
    # pre-translation strip); remove it so the body carries no disclaimer and the
    # scan does not false-trip on its "投资建议"/"investment advice" wording.
    dynamic_out = _strip_body_disclaimer(dynamic_out)
    if output_lang != "en":
        # Translation can paraphrase into advisory tone — re-scan the output.
        violations = violations + _scan_forbidden_output(dynamic_out)

    full_md = dynamic_out + _build_footer(portfolio)
    return full_md, violations, dynamic_out


# A manual window this short (hours) with nothing in it is a same-day re-run
# artifact, not a real reporting period. (R-7)
_SHORT_MANUAL_WINDOW_HOURS = 2.0


def _is_short_manual_quiet(
    session_node: str,
    period_start: datetime,
    period_end: datetime,
    news_items: list[NewsItem],
    anomalies: list[PriceAnomaly],
) -> bool:
    """True for a manual re-run over a tiny, empty window (R-7).

    All four must hold: triggered manually, window under the short threshold,
    no window news, no anomalies. Scheduled triggers (after_close) never match,
    so a genuinely quiet scheduled week still sends its heartbeat.
    """
    if session_node != "manual" or news_items or anomalies:
        return False
    span_hours = (period_end - period_start).total_seconds() / 3600.0
    return span_hours < _SHORT_MANUAL_WINDOW_HOURS


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def generate_report(
    session: Session,
    report_date: date | None = None,
    report_type: str = "weekly",
    base_currency: str = "USD",
    output_lang: str = "en",
    session_node: str = "manual",
) -> Report:
    """
    Run the full F1 report generation pipeline and persist the result.

    Returns the Report ORM object (status='success' or 'failed').
    Raises if the report record cannot be written (e.g. unique constraint violation
    when a report for the same date+type+session_node already exists).
    """
    settings = get_settings()
    user_id = get_current_user_id()
    eff_date = report_date or datetime.now(tz=ET).date()

    # ------------------------------------------------------------------
    # Idempotency: (user_id, report_date, report_type, session_node) is unique
    # (H-DEBT-1). session_node identifies WHICH trigger produced the report
    # (e.g. "manual" vs "after_close" for the M/W/F 16:30 ET cadence) so two
    # distinct triggers on the same calendar day get separate rows / windows /
    # emails. A redelivered Celery task (task_acks_late=True) or a repeated
    # manual /reports/generate passes the SAME session_node, so it still
    # short-circuits a completed report instead of inserting a duplicate; reuse
    # a prior failed/in_progress row so a retry can regenerate in place.
    # ------------------------------------------------------------------
    existing = session.execute(
        select(Report).where(
            Report.user_id == user_id,
            Report.report_date == eff_date,
            Report.report_type == report_type,
            Report.session_node == session_node,
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
            session_node=session_node,
            status="in_progress",
            prompt_version=_PROMPT_VERSION,
            disclaimer_version=_DISCLAIMER_VERSION,
        )
        session.add(report)
    # ADR-002 incremental window: [previous report's period_end, now], computed
    # ONCE on the first attempt and then frozen for the lifetime of this row. A
    # retry of a failed/needs_review row reuses the original window rather than
    # recomputing it: recomputing on every retry made the window (and therefore
    # the report content) non-deterministic across retries of the SAME row,
    # which is both a bad dedup invariant (two attempts at "the same report"
    # produce different content) and the path by which a same-day retry could
    # collapse start_date == end_date.
    if report.period_start is None or report.period_end is None:
        # Exclude this row from the watermark: its own (not-yet-committed)
        # period_end must not become its own period_start — autoflush=False
        # means the status reset above is not yet visible to this query anyway,
        # but a brand-new row also has no period_end yet to read back.
        period_start = user_watermark(
            session,
            user_id,
            report_type,
            exclude_report_id=report.id if existing is not None else None,
        )
        period_end = datetime.now(tz=UTC)
        report.period_start = period_start
        report.period_end = period_end
        session.flush()  # get the id without committing
        logger.info(
            "report %s: generation started for %s (window %s → %s)",
            report.id,
            eff_date,
            period_start.isoformat(),
            period_end.isoformat(),
        )
    else:
        assert report.period_start is not None and report.period_end is not None
        period_start = report.period_start
        period_end = report.period_end
        logger.info(
            "report %s: retrying for %s (window frozen at %s → %s)",
            report.id,
            eff_date,
            period_start.isoformat(),
            period_end.isoformat(),
        )

    ctx = ReportContext()
    ctx.period_start = period_start.isoformat()
    ctx.period_end = period_end.isoformat()

    try:
        # ------------------------------------------------------------------
        # 1. Gather inputs (news + price moves read from the capture stores)
        # ------------------------------------------------------------------
        logger.info("report %s: fetching portfolio snapshot", report.id)
        portfolio_snap = compute_portfolio(session, base_currency=base_currency)
        ctx.portfolio_summary = _serialize_portfolio(portfolio_snap)

        logger.info("report %s: loading windowed news", report.id)
        news_items = load_news_window(session, period_start, period_end)
        ctx.news_items = _serialize_news(news_items)

        logger.info("report %s: detecting macro signals", report.id)
        macro_signals = detect_macro_signals(news_items)
        ctx.macro_signals = _serialize_macro(macro_signals)

        logger.info("report %s: detecting windowed price anomalies", report.id)
        anomalies, trading_days = detect_window_anomalies(session, period_start, period_end)
        ctx.price_anomalies = _serialize_anomalies(anomalies)
        ctx.window_trading_days = trading_days

        # R-5: the real price-data cutoff (last in-window close), distinct from
        # period_end. Stored so the data-window line and re-render agree.
        price_through = latest_window_close_date(session, period_start, period_end)
        ctx.price_data_through = price_through.isoformat() if price_through else ""

        # Technical position (#4): computed here (needs the session) and stored, so
        # §4.4 stays code-built and re-render reproduces it without a DB read.
        logger.info("report %s: computing technical position", report.id)
        ctx.technical_positions = _serialize_technical(
            compute_technical_positions(
                session, ctx.portfolio_summary.get("holdings", []), eff_date
            )
        )

        # Forward calendar (#1): read the scheduled US events the capture task
        # persisted. Stored so re-render reproduces §2.5 without a DB read.
        logger.info("report %s: loading forward calendar", report.id)
        ctx.forward_events = load_forward_events(
            session, eff_date, eff_date + timedelta(days=_FORWARD_WINDOW_DAYS)
        )

        # ------------------------------------------------------------------
        # 2. Skip check
        # ------------------------------------------------------------------
        if not macro_signals.has_any_hit and not anomalies:
            logger.info("report %s: quiet day — no signals, no anomalies", report.id)
            quiet_body = (
                "## §2 Macro Signals\n\n"
                "No macro keyword themes triggered in this report period.\n\n"
                "## §3 Holdings Analysis\n\n"
                "No significant market developments detected for monitored holdings.\n\n"
                "## §4 Risk Radar\n\n"
                "No price anomalies or concentration alerts in this report period."
            )
            quiet_md, _, _ = _render_full_md(
                eff_date.strftime("%Y-%m-%d"),
                ctx.portfolio_summary,
                ctx.news_items,
                quiet_body,
                output_lang,
                ctx.period_start,
                ctx.period_end,
                ctx.window_trading_days,
                price_data_through=ctx.price_data_through,
            )
            report.status = "skipped"
            report.report_md = quiet_md
            report.report_inputs = ctx.to_jsonb()
            report.generated_at = datetime.now(tz=UTC)
            session.commit()
            # R-7: a short manual re-run (e.g. a same-day second trigger minutes
            # after the first) covers a near-empty window — 0 news, 0 anomalies,
            # nothing the first report didn't have. Emailing it is pure noise
            # (the scheduled cadence never produces this). Skip the heartbeat for
            # that case only; a genuinely quiet SCHEDULED window still emails so
            # a calm week is distinguishable from a broken pipeline.
            if _is_short_manual_quiet(
                session_node, period_start, period_end, news_items, anomalies
            ):
                logger.info(
                    "report %s: short manual quiet window — suppressing heartbeat email",
                    report.id,
                )
                return report
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
        raw_pass1 = _call_llm(
            client,
            low_cost_model,
            pass1_system,
            pass1_user,
            with_holdings=False,
            usage_sink=ctx.llm_calls,
        )
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

        # ------------------------------------------------------------------
        # 5. Holding-relevant news enrichment (R-3) — anomaly-driven
        # ------------------------------------------------------------------
        # After we know WHICH holdings moved, recall window news relevant to each
        # (映射缺口: a captured story that matched no macro theme), and for the
        # most-moved holdings the store has NOTHING for, run a targeted live
        # search (源缺口: a window-relevant story the RSS sources never carried).
        # Both are holdings-derived, so they run AFTER Pass 1 and feed only Pass 2.
        anomaly_ids = [a["identifier"] for a in ctx.price_anomalies if a.get("identifier")]
        recalled = recall_holding_news(news_items, anomaly_ids)
        ctx.holding_news = {ident: _serialize_news(items) for ident, items in recalled.items()}
        logger.info(
            "report %s: recalled holding news for %d/%d moved holdings",
            report.id,
            len(ctx.holding_news),
            len(anomaly_ids),
        )

        targeted = _targeted_anomaly_queries(ctx.price_anomalies, set(ctx.holding_news.keys()))
        remaining_budget = max(0, settings.TAVILY_DAILY_BUDGET - len(ctx.search_queries))
        if targeted and remaining_budget > 0:
            tq = [q for _ident, q in targeted][:remaining_budget]
            logger.info("report %s: %d targeted anomaly searches", report.id, len(tq))
            targeted_results = _run_tavily_search(tq, budget=remaining_budget)
            ctx.search_results.extend(targeted_results)

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
            ctx.period_start,
            ctx.period_end,
            ctx.window_trading_days,
            ctx.holding_news,
        )

        ctx.pass2_model = primary_model
        ctx.pass2_prompt = pass2_user

        logger.info("report %s: Pass 2 LLM call (%s)", report.id, primary_model)
        # Pass 2 carries holdings → enforce data_collection=deny
        raw_pass2 = _call_llm(
            client,
            primary_model,
            _PASS2_SYSTEM,
            pass2_user,
            with_holdings=True,
            usage_sink=ctx.llm_calls,
        )

        # H-DEBT-2: a provider can return a truncated HTTP 200 (rate-limiting,
        # mid-response cutoff). A short body missing §3/§4 must not ship as
        # status='success' with code-injected §2.5/§4.2/§4.4 masking the gap —
        # raise so the Celery task retries (max_retries=2).
        if len(raw_pass2) < _PASS2_MIN_CHARS or not all(
            marker in raw_pass2 for marker in _PASS2_REQUIRED_MARKERS
        ):
            raise RuntimeError(
                f"report {report.id}: Pass 2 output looks truncated "
                f"({len(raw_pass2)} chars, missing one of {_PASS2_REQUIRED_MARKERS})"
            )
        ctx.pass2_raw = raw_pass2

        # ------------------------------------------------------------------
        # 7/8. Annotate + assemble + render language + compliance scan (#5/#7/#8)
        # ------------------------------------------------------------------
        report_date_str = eff_date.strftime("%Y-%m-%d")
        full_md, violations, translated_body = _render_full_md(
            report_date_str,
            ctx.portfolio_summary,
            ctx.news_items,
            raw_pass2,
            output_lang,
            ctx.period_start,
            ctx.period_end,
            ctx.window_trading_days,
            ctx.price_anomalies,
            ctx.technical_positions,
            ctx.forward_events,
            ctx.price_data_through,
        )
        ctx.pass2_translated = translated_body
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
            report.period_start.isoformat() if report.period_start else "",
            report.period_end.isoformat() if report.period_end else "",
            int(inputs.get("window_trading_days", 0)),
            inputs.get("holding_news", {}),
        )
        regen_calls: list[dict[str, Any]] = []
        raw_body = _call_llm(
            _openrouter_client(),
            get_settings().PRIMARY_LLM_MODEL,
            _PASS2_SYSTEM,
            pass2_user,
            with_holdings=True,
            usage_sink=regen_calls,
        )
        if len(raw_body) < _PASS2_MIN_CHARS or not all(
            marker in raw_body for marker in _PASS2_REQUIRED_MARKERS
        ):
            raise RuntimeError(
                f"report {report.id}: regenerated Pass 2 output looks truncated "
                f"({len(raw_body)} chars, missing one of {_PASS2_REQUIRED_MARKERS})"
            )
        # New dict identity so SQLAlchemy flags the JSONB column dirty (an
        # in-place mutation of the existing dict would not be detected).
        report.report_inputs = {
            **inputs,
            "pass2_raw": raw_body,
            "pass2_prompt": pass2_user,
            "llm_calls": regen_calls,
        }
    elif mode == "render":
        raw_body = inputs["pass2_raw"]
    else:
        raise ValueError(f"unknown mode {mode!r} (expected 'render' or 'analyze')")

    report_date_str = report.report_date.strftime("%Y-%m-%d")
    full_md, violations, translated_body = _render_full_md(
        report_date_str,
        portfolio,
        news_items,
        raw_body,
        output_lang,
        report.period_start.isoformat() if report.period_start else "",
        report.period_end.isoformat() if report.period_end else "",
        int(inputs.get("window_trading_days", 0)),
        inputs.get("price_anomalies", []),
        inputs.get("technical_positions", []),
        inputs.get("forward_events", []),
        str(inputs.get("price_data_through", "")),
    )
    report.status = "needs_review" if violations else "success"
    report.report_md = full_md
    # Persist translation snapshot alongside report_md for compliance traceability.
    if mode != "render" and report.report_inputs is not None:
        report.report_inputs = {**report.report_inputs, "pass2_translated": translated_body}
    report.generated_at = datetime.now(tz=UTC)
    session.commit()
    logger.info("report %s: regenerated (mode=%s, lang=%s)", report.id, mode, output_lang)
    return report
