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
from string import Template
from typing import Any, TypedDict, cast

import httpx
import openai
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.compliance.forbidden_vocab import FORBIDDEN_OUTPUT_PATTERNS as _FORBIDDEN_OUTPUT_PATTERNS
from app.compliance.forbidden_vocab import PROMPT_VOCAB_STRING as _FORBIDDEN_PROMPT_VOCAB
from app.core.config import OR_ATTRIBUTION_HEADERS, get_settings
from app.core.deps import get_current_user_id
from app.core.ops_log import log_ops_event
from app.core.timezones import ET
from app.models.report import Report
from app.services.email_sender import send_ops_alert, send_report_email
from app.services.forward_events import load_forward_events
from app.services.github_issues import create_bug_report
from app.services.holding_news import recall_holding_news
from app.services.i18n_glossary import load_i18n_glossary, locale_for_output_lang
from app.services.llm_retry_config import load_llm_retry_config
from app.services.macro_detector import MacroSignals, detect_macro_signals
from app.services.news_fetcher import NewsItem
from app.services.portfolio_calculator import PortfolioSnapshot, compute_portfolio
from app.services.price_anomaly_detector import PriceAnomaly
from app.services.report_types import validate_report_type
from app.services.technical_position import TechnicalPosition, compute_technical_positions
from app.services.window_data import (
    detect_window_anomalies,
    latest_window_close_date,
    load_news_window,
    mark_news_surfaced,
    unmark_news_surfaced,
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
# The §4.2 cross-reference example below reads i18n_glossary.yml's
# cross_reference_example once (frozen into this module constant at import —
# same restart-to-pick-up-a-YAML-edit caveat as _RELEASE_DELAY_TERMS/
# _STRAY_TAGS/_BODY_DISCLAIMER_RE; see i18n_glossary.py's module docstring).
_pass2_cross_ref = load_i18n_glossary().templates["cross_reference_example"]
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
    f"§4.2 CROSS-REFERENCES: '{_pass2_cross_ref['en']}' / "
    f"'{_pass2_cross_ref['zh-Hans']}' may only be used for a holding "
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

# Mechanism prep for Ring 1 multi-cadence report types (see the Obsidian
# multi-cadence report redesign notes, phase 3): the §2/§3/§4 narrative
# instructions are split into per-section blocks so _build_pass2_prompt can be
# asked for a subset. generate_report() always requests ALL_NARRATIVE_SECTIONS
# today — no caller picks a subset yet, that mapping (which report_type gets
# which sections) is a Ring 1 decision, not built here.
_SECTION2_INSTRUCTIONS = (
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
)
_SECTION3_INSTRUCTIONS = (
    "## §3 Holdings Analysis\n"
    "Select the holdings most affected this period and, for each, go beyond "
    "'position size + what happened'. Explain WHY it surfaced (which signal/move "
    "implicates it), the mechanism linking the development to that specific "
    "holding, how it sits relative to the rest of the portfolio (concentration, "
    "correlation, currency), and which forward signals would confirm or dissolve "
    "the thesis. Depth over breadth — a few holdings analysed well beats a list. "
    "End each causal attribution with its confidence label (see CONFIDENCE "
    "LABELS above).\n\n"
)
_SECTION4_INSTRUCTIONS = (
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
_NARRATIVE_SECTION_BLOCKS: dict[str, str] = {
    "§2": _SECTION2_INSTRUCTIONS,
    "§3": _SECTION3_INSTRUCTIONS,
    "§4": _SECTION4_INSTRUCTIONS,
}
ALL_NARRATIVE_SECTIONS: frozenset[str] = frozenset(_NARRATIVE_SECTION_BLOCKS)

_PASS2_PREAMBLE_TEMPLATE = (
    "Write {sections_clause} of the financial analysis briefing in Markdown.\n"
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
)


def _section_list_clause(sections: list[str]) -> str:
    label = "section" if len(sections) == 1 else "sections"
    if len(sections) <= 2:
        names = " and ".join(sections)
    else:
        names = ", ".join(sections[:-1]) + f", and {sections[-1]}"
    return f"{label} {names}"


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


class ReportInputsDict(TypedDict, total=False):
    """Static-typed view of the `report_inputs` JSONB shape (#39).

    Mirrors ReportContext's field set — keep the two in sync by hand; the
    to_jsonb() JSON round-trip (dataclasses.asdict -> json.dumps/loads) means
    nothing enforces that sync automatically, only mypy's key/type checking
    at call sites that `cast` a raw dict into this type.

    `total=False`: every key is optional at read time. Rows written before a
    later ReportContext field was added won't have it, and
    regenerate_report's analyze-mode update (`{**inputs, "pass2_raw": ...}`)
    only ever adds/overwrites the keys it touches, never re-derives the rest
    — so no key here can be assumed universally present on a stored row.
    """

    portfolio_summary: dict[str, Any]
    news_items: list[dict[str, Any]]
    macro_signals: dict[str, Any]
    price_anomalies: list[dict[str, Any]]
    technical_positions: list[dict[str, Any]]
    forward_events: list[dict[str, Any]]
    holding_news: dict[str, list[dict[str, Any]]]
    period_start: str
    period_end: str
    window_trading_days: int
    price_data_through: str
    pass1_model: str
    pass1_prompt: str
    pass1_raw: str
    search_queries: list[str]
    search_results: list[dict[str, Any]]
    pass2_model: str
    pass2_prompt: str
    pass2_raw: str
    llm_calls: list[dict[str, Any]]
    pass2_translated: str


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
        """Return the write-side dict for the `report_inputs` JSONB column.

        Kept as `dict[str, Any]`, not `ReportInputsDict` (#39) — the column
        itself is untyped JSONB (`Mapped[dict[str, Any] | None]` on the ORM
        model), so a TypedDict return here would only fight that boundary at
        every assignment site for no type-safety gain. `ReportInputsDict` is
        the read-side contract instead: callers `cast` into it once they pull
        a row's `report_inputs` back out, which is where the drift this issue
        cares about (readers assuming a key/shape ReportContext never wrote)
        actually gets caught.
        """
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
        default_headers=OR_ATTRIBUTION_HEADERS,
    )


class LLMEmptyResponseError(RuntimeError):
    """A 200 response from OpenRouter carried no usable choices.

    Observed once as a malformed 200 (`choices=None`) from a low-cost model via
    DigitalOcean/Venice — `resp.choices[0]` then raised a bare, unclassified
    TypeError. Raising a named error lets callers (and Celery's retry) treat it
    as a transient provider fault rather than a code bug. (I-DEBT-2)
    """


# Backoff sequences inside _call_llm (bounded, then re-raise so the outer
# Celery retry still sees a persistent failure). (I-DEBT-2)
# Values are admin-editable via config/llm_retry.yml (#38), loaded fresh on
# every call by load_llm_retry_config() — see that module's docstring for why
# BYOK provider order is deliberately NOT included in the same config.


def _call_llm(
    client: openai.OpenAI,
    model: str,
    system: str,
    user: str,
    *,
    with_holdings: bool = False,
    pin_provider: bool = True,
    provider_order: list[str] | None = None,
    allow_fallbacks: bool | None = None,
    enforce_data_collection: bool = True,
    disable_reasoning: bool = False,
    usage_sink: list[dict[str, Any]] | None = None,
) -> str:
    """Call an OpenRouter model.  Returns the assistant content string.

    The data_collection policy (deny) is enforced on EVERY call by default as
    defense in depth: although Pass 1 is contractually holdings-free, denying
    training providers unconditionally means an accidental future holdings leak
    is still protected. `with_holdings` is retained only as an explicit intent
    marker for callers (and the test harness) — it no longer gates the data
    policy.

    `enforce_data_collection=False` opts a call OUT of the deny guard — issue
    #78 decision (2026-08-06): a scoped compliance exception for Pass 1 /
    translation only, which route via OpenRouter BYOK straight to DeepSeek's
    own first-party backend (a specific `order` pin, not the general
    marketplace pool), where the data_collection filter would otherwise exclude
    it. Every other call site keeps the default (True). Because this bypasses
    the deny guard, those same call sites MUST also pass
    `allow_fallbacks=False` (see below) — otherwise a DeepSeek outage could
    silently reroute the (holdings-bearing, for translation) payload to an
    arbitrary marketplace provider deny would normally have excluded
    (PR #79 review finding).

    `pin_provider=False` omits OPENROUTER_PROVIDER_ORDER, letting OpenRouter
    route to whichever provider is available — used for translation calls so
    repeated requests aren't all funneled through the same (possibly
    rate-limited) provider pair.

    `provider_order`, when given, sets the provider preference list directly
    (overriding `pin_provider`/`OPENROUTER_PROVIDER_ORDER`) — used to steer a
    call toward providers known to be available when the default pool is
    rate-limited.

    `allow_fallbacks`, when not None, overrides OPENROUTER_ALLOW_FALLBACKS for
    this call. Pass `False` alongside a `provider_order` pin to make that pin a
    hard requirement — the call fails outright rather than silently routing to
    an unpinned provider. Required whenever `enforce_data_collection=False`
    (the BYOK exception above only holds if the pinned provider is the only
    one that can ever be used).

    `disable_reasoning=True` suppresses reasoning/thinking tokens via
    `extra_body={"reasoning": {"enabled": False}}`. Needed for
    `~vendor/model-latest` router aliases (e.g. `~deepseek/deepseek-v4-flash-latest`)
    whose `reasoning.default_enabled` is True unlike their non-aliased
    counterparts — without this a mechanical, low-cost call silently starts
    paying for and waiting on reasoning tokens it never asked for.

    `enforce_data_collection=False` and `allow_fallbacks=False` are a required
    pair, enforced at runtime (not just by docstring/call-site discipline —
    PR #81 review): a caller cannot silently reopen the PR #79
    marketplace-fallback gap by passing the former without the latter.
    """
    extra: dict[str, Any] = {}
    settings = get_settings()
    effective_allow_fallbacks = (
        settings.OPENROUTER_ALLOW_FALLBACKS if allow_fallbacks is None else allow_fallbacks
    )
    if not enforce_data_collection and effective_allow_fallbacks is not False:
        raise ValueError(
            "_call_llm: enforce_data_collection=False requires allow_fallbacks=False "
            "explicitly — an open fallback with the deny guard off can silently reroute "
            "a holdings-bearing payload to an arbitrary marketplace provider (PR #79/#81 review)."
        )
    provider: dict[str, object] = {"allow_fallbacks": effective_allow_fallbacks}
    if provider_order:
        provider["order"] = provider_order
    elif pin_provider:
        order = [p.strip() for p in settings.OPENROUTER_PROVIDER_ORDER.split(",") if p.strip()]
        if order:
            provider["order"] = order
    if enforce_data_collection and settings.OPENROUTER_DATA_COLLECTION:
        provider["data_collection"] = settings.OPENROUTER_DATA_COLLECTION
    # An explicitly-passed allow_fallbacks (a deliberate hard pin, e.g. False)
    # must never be silently dropped just because it's the only key present —
    # that would strip the pin from the actual request (PR #81 review).
    if provider.keys() - {"allow_fallbacks"} or allow_fallbacks is not None:
        extra["provider"] = provider
    if disable_reasoning:
        extra["reasoning"] = {"enabled": False}

    retry_config = load_llm_retry_config()
    resp: Any = None
    _rate_limit_attempts = 0
    _connect_attempts = 0
    for _attempt in range(
        max(
            len(retry_config.ratelimit_backoff_seconds),
            len(retry_config.connect_backoff_seconds),
        )
        + 1
    ):
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
            backoff_idx = _rate_limit_attempts
            _rate_limit_attempts += 1
            if backoff_idx >= len(retry_config.ratelimit_backoff_seconds):
                logger.warning("llm call: model=%s exhausted 429 backoff retries", model)
                raise
            wait = retry_config.ratelimit_backoff_seconds[backoff_idx]
            logger.warning(
                "llm call: model=%s got 429 (attempt %d), backing off %.0fs",
                model,
                backoff_idx + 1,
                wait,
            )
            time.sleep(wait)
        except openai.APIConnectionError:
            backoff_idx = _connect_attempts
            _connect_attempts += 1
            if backoff_idx >= len(retry_config.connect_backoff_seconds):
                logger.warning("llm call: model=%s exhausted connection backoff retries", model)
                raise
            wait = retry_config.connect_backoff_seconds[backoff_idx]
            logger.warning(
                "llm call: model=%s connection error (attempt %d), backing off %.0fs",
                model,
                backoff_idx + 1,
                wait,
            )
            time.sleep(wait)

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


def _tavily_used_today(
    session: Session, report_date: date, exclude_report_id: uuid.UUID | None = None
) -> int:
    """Return the number of Tavily queries already fired today (ET calendar date).

    Counts search_queries stored in report_inputs of terminal-state reports so
    a second run in the same day (manual + after_close, or a retry) sees the
    cumulative daily spend. Excludes the current in-progress row to avoid
    double-counting a retry. Note: Hermes shares this Tavily key; cross-project
    spend is not tracked here — the budget is a Portfonia-only floor.
    """
    rows = session.execute(
        select(Report.report_inputs, Report.id).where(
            Report.report_date == report_date,
            Report.status.in_(("success", "skipped", "needs_review")),
        )
    ).all()
    total = 0
    for inputs, row_id in rows:
        if row_id == exclude_report_id:
            continue
        if inputs and isinstance(inputs, dict):
            total += len(cast(ReportInputsDict, inputs).get("search_queries", []))
    return total


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
            "theme": a.theme,
            "theme_label_zh": a.theme_label_zh,
            "theme_label_en": a.theme_label_en,
            "constituents": [
                {
                    "name": c.name,
                    "identifier": c.identifier,
                    "pct_change": float(c.pct_change),
                    "current_value": float(c.current_value),
                }
                for c in (a.constituents or [])
            ],
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
            "asset_class": hv.asset_class,
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
        "by_asset_class": {k: float(v) for k, v in snap.by_asset_class.items()},
        "concentration": {
            "top_holding_name": snap.concentration.top_holding_name,
            "top_holding_ratio": float(snap.concentration.top_holding_ratio)
            if snap.concentration.top_holding_ratio is not None
            else None,
            "top_holding_asset_class": snap.concentration.top_holding_asset_class,
            "top3_ratio": float(snap.concentration.top3_ratio)
            if snap.concentration.top3_ratio is not None
            else None,
            "top_asset_class_name": snap.concentration.top_asset_class_name,
            "top_asset_class_ratio": float(snap.concentration.top_asset_class_ratio)
            if snap.concentration.top_asset_class_ratio is not None
            else None,
            "single_holding_watch": snap.concentration.single_holding_watch,
            "single_holding_high": snap.concentration.single_holding_high,
            "top3_watch": snap.concentration.top3_watch,
            "asset_class_watch": snap.concentration.asset_class_watch,
            "asset_class_high": snap.concentration.asset_class_high,
        },
        "stale_tickers": snap.stale_tickers,
        "stale_priced_tickers": snap.stale_priced_tickers,
        "holdings": holdings_list,
    }


# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------


def _build_pass1_prompt(
    signals: MacroSignals,
    news: list[NewsItem],
) -> str:
    # DATA ISOLATION: Pass 1 must carry only public information (macro themes +
    # news headlines). Price anomalies are holdings-derived (name/ticker reveal
    # what the user owns) and are therefore withheld here — they are supplied
    # only to Pass 2. This isolation is independent of data_collection=deny
    # (which Pass 1 is now a scoped exception to — issue #78, _BYOK_PROVIDER_ORDER):
    # even routed via BYOK with deny off, Pass 1 must never see holdings, because
    # the exception covers WHERE the (already holdings-free) payload can go, not
    # WHAT the payload is allowed to contain.
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

    For theme-aggregated entries (e.g. Gold = SGOL + 518660 + 518800), the
    headline row shows the weighted-average move; a "Constituents" block below
    the table lists each holding's individual contribution.
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
    theme_detail_lines: list[str] = []

    for a in anomalies:
        theme = a.get("theme")
        ident = a.get("identifier", "")

        if theme:
            # Theme entry: show label + theme key.  Constituents go in a note below.
            label_zh = a.get("theme_label_zh") or a.get("name", "")
            name_col = f"{label_zh} ({ident})" if ident != label_zh else label_zh
        else:
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

        if theme:
            constituents: list[dict[str, Any]] = a.get("constituents") or []
            if len(constituents) >= 2:
                parts = ", ".join(
                    f"{c.get('identifier') or c.get('name')} {pct(c.get('pct_change'))}"
                    for c in constituents
                )
                label = a.get("theme_label_en") or ident
                theme_detail_lines.append(
                    f"- **{label}**: {parts} (weighted avg {pct(a.get('window_net_pct'))})"
                )

    result = "\n".join(lines)
    if theme_detail_lines:
        result += "\n\n**Constituent breakdown:**\n" + "\n".join(theme_detail_lines)
    return result


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
# RSS-derived delay caveat triggers (#1): a funding lapse can suspend BLS/BEA
# releases. zh-Hans terms load from i18n_glossary.yml's release_delay_terms_zh
# — frozen into this module constant at import, so an admin edit to that YAML
# list needs a process restart to take effect (PR #91 review: unlike
# _build_footer/_stale_ticker_hint, which call load_i18n_glossary() fresh
# inside the function body and pick up an edit on the next call).
_RELEASE_DELAY_TERMS = (
    "government shutdown",
    "funding lapse",
    "funding gap",
    "appropriations lapse",
    "budget shutdown",
    *load_i18n_glossary().release_delay_terms_zh,
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
    enabled_sections: frozenset[str] = ALL_NARRATIVE_SECTIONS,
) -> str:
    lines: list[str] = []

    # Report window facts — anchor all time references to "this report period".
    lines.append("=== REPORT WINDOW ===")
    span = "unknown"
    if period_start and period_end:
        ps_et = datetime.fromisoformat(period_start).astimezone(ET).strftime("%Y-%m-%d %H:%M")
        pe_et = datetime.fromisoformat(period_end).astimezone(ET).strftime("%Y-%m-%d %H:%M")
        span = f"{ps_et} to {pe_et} ET"
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
            + (f" | asset_class: {h['asset_class']}" if h.get("asset_class") else "")
        )
    lines.append("")
    lines.append(f"By market: {portfolio.get('by_market', {})}")
    lines.append(f"By currency: {portfolio.get('by_currency', {})}")
    lines.append(f"By asset class: {portfolio.get('by_asset_class', {})}")

    conc = portfolio.get("concentration", {})
    if conc.get("single_holding_watch") or conc.get("top3_watch") or conc.get("asset_class_watch"):
        lines.append("")
        lines.append("Concentration flags:")
        if conc.get("single_holding_high"):
            lines.append(
                f"  [!] Top holding {conc.get('top_holding_name')} "
                f"({conc.get('top_holding_asset_class')}) = {conc.get('top_holding_ratio', 0):.1%} "
                "— above the high threshold for this asset class"
            )
        elif conc.get("single_holding_watch"):
            lines.append(
                f"  Top holding {conc.get('top_holding_name')} "
                f"({conc.get('top_holding_asset_class')}) = {conc.get('top_holding_ratio', 0):.1%} "
                "— above the watch threshold for this asset class"
            )
        if conc.get("top3_watch"):
            lines.append(f"  Top-3 combined = {conc.get('top3_ratio', 0):.1%} (>50% watch)")
        if conc.get("asset_class_high"):
            lines.append(
                f"  [!] Top asset class ({conc.get('top_asset_class_name')}) = "
                f"{conc.get('top_asset_class_ratio', 0):.1%} (>65% threshold)"
            )
        elif conc.get("asset_class_watch"):
            lines.append(
                f"  Top asset class ({conc.get('top_asset_class_name')}) = "
                f"{conc.get('top_asset_class_ratio', 0):.1%} (>50% watch)"
            )

    stale = portfolio.get("stale_tickers", [])
    if stale:
        lines.append("Stale/no-price identifiers (excluded from valuations):")
        vendor_zh = load_i18n_glossary().vendor_names["Tiantian Fund"]["zh-Hans"]
        for ident in stale:
            lines.append(f"  - {_stale_ticker_hint(ident, vendor_zh)}")

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
    ordered_sections = [s for s in ("§2", "§3", "§4") if s in enabled_sections]
    lines.append("")
    instructions = _PASS2_PREAMBLE_TEMPLATE.format(
        sections_clause=_section_list_clause(ordered_sections)
    ) + "".join(_NARRATIVE_SECTION_BLOCKS[s] for s in ordered_sections)
    lines.append(instructions)
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
        "| Holding | Currency | Value | % Portfolio | Custodian | Asset Class |",
        "|---------|----------|-------|-------------|-----------|-------------|",
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
                f"| {h.get('broker', '') or '—'} | {h.get('asset_class', '—') or '—'} |"
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
        ("By asset class", portfolio.get("by_asset_class", {})),
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

    stale_priced = portfolio.get("stale_priced_tickers", [])
    if stale_priced:
        lines.append(
            f"\n> [!] Price data stale (>4 calendar days old): {', '.join(stale_priced)}"
            " — values included but may not reflect recent moves."
        )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Stale ticker type hint
# ---------------------------------------------------------------------------

_FUND_CODE_RE = re.compile(r"^\d{6}$")
_A_SHARE_RE = re.compile(r"^\d{6}\.(SS|SZ)$", re.IGNORECASE)
_HK_RE = re.compile(r"^\d{4}\.HK$", re.IGNORECASE)


def _stale_ticker_hint(identifier: str, vendor_zh: str) -> str:
    """Return an annotated string for a stale identifier to prevent LLM misclassification.

    Without annotation, 6-digit fund codes (e.g. a real CN mutual fund code
    like 005827) get misidentified as Korea Exchange listings by the LLM.
    *vendor_zh* is the Tiantian Fund zh-Hans name — passed in rather than
    loaded here so a caller iterating multiple stale identifiers reads
    i18n_glossary.yml once, not once per identifier.
    """
    if _FUND_CODE_RE.match(identifier):
        return f"{identifier} (CN mutual fund code / Tiantian Fund, {vendor_zh})"
    if _A_SHARE_RE.match(identifier):
        return f"{identifier} (A-share / Shanghai or Shenzhen)"
    if _HK_RE.match(identifier):
        return f"{identifier} (HK-listed stock)"
    return f"{identifier} (stock ticker)"


# ---------------------------------------------------------------------------
# F3 fixed footer
# ---------------------------------------------------------------------------


def _build_footer(portfolio: dict[str, Any]) -> str:
    """Build the fixed report footer (F3).

    Injected at the template layer — never generated by the LLM.
    Contains: FX rate note (date-stamped from portfolio snapshot) + bilingual disclaimer.
    Wording (both languages) is sourced from i18n_glossary.yml's `templates`
    section (issue #90) — this function only fills in $fx_date/$base_ccy.
    Loads the glossary once (not once per template lookup — PR #91 review).
    """
    base_ccy = portfolio.get("base_currency", "USD")
    fx_date = portfolio.get("fx_date", "unknown")
    templates = load_i18n_glossary().templates

    def tpl(key: str, locale: str) -> str:
        return templates[key][locale]

    def note(locale: str) -> str:
        return Template(tpl("data_sources_note", locale)).substitute(
            fx_date=fx_date, base_ccy=base_ccy
        )

    lines = [
        "",
        "---",
        "",
        f"## {tpl('footer_header', 'en')} / {tpl('footer_header', 'zh-Hans')}",
        "",
        f"**{tpl('data_sources_label', 'en')}** {note('en')}",
        "",
        f"**{tpl('data_sources_label', 'zh-Hans')}** {note('zh-Hans')}",
        "",
        f"**{tpl('disclaimer_label', 'en')}** {tpl('disclaimer', 'en')}",
        "",
        f"**{tpl('disclaimer_label', 'zh-Hans')}** {tpl('disclaimer', 'zh-Hans')}",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# F2 source annotation
# ---------------------------------------------------------------------------


# Stray provenance/disclaimer tags the LLM may still emit despite the system
# prompt forbidding them. They are pure noise in the rendered report (the
# S-numbers don't resolve, the per-line disclaimer duplicates the footer), so we
# strip them as a backstop. Covers EN and the zh tags produced before #9 — the
# zh set loads from i18n_glossary.yml's legacy_removed_markers_zh, frozen into
# this compiled-pattern list at import (restart to pick up a YAML edit —
# same caveat as _RELEASE_DELAY_TERMS above).
_STRAY_TAGS = [
    re.compile(re.escape(_COMPLIANCE_MARKER)),
    re.compile(
        r"\[(?:"
        + "|".join(load_i18n_glossary().legacy_removed_markers_zh)
        + r"|news|analysis|market data)\]",
        re.IGNORECASE,
    ),
]

# A line the model emits as its own disclaimer / legal notice. The single
# disclaimer lives in the template footer (F3), so any disclaimer inside the body
# is both redundant and a false-positive trigger for the forbidden-output scan
# (it legitimately contains advisory-sounding wording in both languages). These
# phrases appear only in disclaimers, never in factual market prose, so dropping
# the whole line is safe. Runs on the body only — the footer is appended
# afterwards, untouched. zh-Hans fragments load from i18n_glossary.yml's
# body_disclaimer_regex_terms_zh (some contain regex alternation/wildcard
# syntax, not plain literal substrings) — frozen into this compiled pattern
# at import, same restart caveat as _RELEASE_DELAY_TERMS/_STRAY_TAGS above.
# i18n_glossary.py's loader rejects an empty body_disclaimer_regex_terms_zh
# at load time, so this can't silently become a match-everything pattern
# via a leading empty regex alternative (PR #91 review).
_BODY_DISCLAIMER_RE = re.compile(
    "|".join(load_i18n_glossary().body_disclaimer_regex_terms_zh)
    + r"|informational purposes|not\s+constitute\s+investment|investment advice"
    r"|not\s+a\s+recommendation|consult\s+a\s+qualified"
    r"|a\s+recommendation\s+to\s+(buy|sell)|solicitation\s+of\s+any\s+invest",
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
        ps_et = datetime.fromisoformat(period_start).astimezone(ET).strftime("%Y-%m-%d %H:%M")
        pe_et = datetime.fromisoformat(period_end).astimezone(ET).strftime("%Y-%m-%d %H:%M")
        span = f"{ps_et} to {pe_et} ET"
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

# Provider preference for the unstructured/BYOK calls — Pass 1 search-query
# generation and translation (issue #78, 2026-08-06). Distinct from
# OPENROUTER_PROVIDER_ORDER (DigitalOcean,Venice — used for pinned Pass2/
# regenerate calls on PRIMARY_LLM_MODEL): pins straight to DeepSeek's own
# first-party backend via OpenRouter BYOK rather than the DigitalOcean/Venice
# marketplace pool. Both call sites also pass enforce_data_collection=False
# (see _call_llm) — a scoped compliance exception, since routing to DeepSeek's
# own API means the general OPENROUTER_DATA_COLLECTION=deny guard (which
# exists specifically to keep calls off DeepSeek's first-party endpoint) would
# otherwise exclude this provider entirely. UNLIKE _call_llm's usual
# allow_fallbacks=True default, both call sites force allow_fallbacks=False —
# a HARD pin, not a preference: since the deny guard is off for these calls,
# an open fallback on DeepSeek unavailability could silently reroute
# holdings-bearing payloads (translation ships with_holdings=True) to an
# arbitrary marketplace provider deny would normally have excluded. Fail the
# call rather than degrade the compliance guarantee (PR #79 review finding).
_BYOK_PROVIDER_ORDER = ["DeepSeek"]


def _build_glossary_instruction(target_lang: str) -> str:
    """Build the LLM glossary-instruction suffix for *target_lang* from i18n_glossary.yml.

    Returns "" for a locale with no glossary entry (mirrors the previous
    hardcoded dict's `.get(target_lang, "")` fallback).
    """
    locale = locale_for_output_lang(target_lang)
    glossary = load_i18n_glossary()
    if locale not in glossary.supported_locales:
        return ""
    pairs = "; ".join(
        f'"{en}" -> "{translations[locale]}"'
        for en, translations in glossary.report_glossary.items()
    )
    forbidden = "; ".join(
        f'"{translations[locale]}"' for translations in glossary.forbidden_renderings.values()
    )
    return (
        f" Use this exact glossary for fixed terms: {pairs}. Never render any word as {forbidden}."
    )


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
    glossary = _build_glossary_instruction(target_lang)
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

    `provider_order=_BYOK_PROVIDER_ORDER`: translation is a mechanical render on
    the low-cost model, routed via OpenRouter BYOK straight to DeepSeek's own
    backend (issue #78) rather than the pinned Pass2 marketplace pool.
    `allow_fallbacks=False` makes that pin a hard requirement — this call
    carries holdings-derived report text (with_holdings=True), so it must fail
    rather than silently reroute to an arbitrary provider if DeepSeek is
    unavailable. `enforce_data_collection=False` and `disable_reasoning=True`
    are part of the same change — see _call_llm docstring.
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
        provider_order=_BYOK_PROVIDER_ORDER,
        allow_fallbacks=False,
        enforce_data_collection=False,
        disable_reasoning=True,
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
            provider_order=_BYOK_PROVIDER_ORDER,
            allow_fallbacks=False,
            enforce_data_collection=False,
            disable_reasoning=True,
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
    # scan does not false-trip on its advisory-sounding wording in either language.
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
    report_type: str = "incremental",
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
    validate_report_type(report_type)
    settings = get_settings()
    user_id = get_current_user_id()
    eff_date = report_date or datetime.now(tz=ET).date()

    # ------------------------------------------------------------------
    # Idempotency: (user_id, report_date, report_type, session_node) is unique
    # (H-DEBT-1). session_node identifies WHICH trigger produced the report
    # (e.g. "manual" vs "after_close" for the M/W/F 17:00 ET cadence) so two
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
        # issue #45 review follow-up: email_sent_at and provider_message_id are
        # a pair (both set together in email_sender.send_report_email). Clearing
        # only email_sent_at here would leave a stale Resend id from the prior
        # send attached to a row that now reads as "not sent".
        report.provider_message_id = None
        # H-DEBT-3 / PR #139 review: this row's window is frozen and reused
        # below, so a retry (e.g. a reopened needs_review row) must reselect
        # the SAME news candidate set the first attempt saw. Without this, a
        # prior attempt's own mark_news_surfaced call would make
        # load_news_window silently exclude those items on retry — a no-op
        # for a failed row (never marked), a real bug for needs_review.
        unmark_news_surfaced(session, report.id)
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

    log_ops_event(
        "report.generate.start",
        report_id=str(report.id),
        report_date=str(eff_date),
        session_node=session_node,
        report_type=report_type,
        period_start=period_start.isoformat(),
        period_end=period_end.isoformat(),
    )

    ctx = ReportContext()
    ctx.period_start = period_start.isoformat()
    ctx.period_end = period_end.isoformat()

    try:
        # ------------------------------------------------------------------
        # 1. Gather inputs (news + price moves read from the capture stores)
        # ------------------------------------------------------------------
        logger.info("report %s: fetching portfolio snapshot", report.id)
        portfolio_snap = compute_portfolio(
            session,
            user_id=user_id,
            base_currency=base_currency,
            as_of=period_end.astimezone(ET).date(),
        )
        ctx.portfolio_summary = _serialize_portfolio(portfolio_snap)
        if portfolio_snap.stale_tickers:
            stale_list = ", ".join(portfolio_snap.stale_tickers)
            logger.warning(
                "report %s: %d holding(s) missing price, excluded from report: %s",
                report.id,
                len(portfolio_snap.stale_tickers),
                stale_list,
            )
            alert_body = (
                f"Report {report.id} ({report.report_date}) excluded the following "
                f"holdings due to missing price data:\n\n"
                + "\n".join(f"  - {t}" for t in portfolio_snap.stale_tickers)
                + "\n\nCheck price_snapshots and capture logs."
            )
            send_ops_alert(
                subject=f"[Portfonia] price missing — {len(portfolio_snap.stale_tickers)} holding(s) excluded",
                body=alert_body,
                idempotency_key=f"ops-price-missing-{report.id}",
            )
            create_bug_report(
                title=f"holdings excluded: price missing for {stale_list}",
                body=(
                    f"## Holdings excluded from report due to missing price\n\n"
                    f"**Report:** {report.id} ({report.report_date})\n\n"
                    f"**Excluded holdings:** {stale_list}\n\n"
                    f"These holdings were absent from §1 portfolio composition and all "
                    f"aggregation totals. Likely causes: capture task failure, new holding "
                    f"with no price_snapshots row, or ticker/fund_code lookup mismatch.\n\n"
                    f"**Fix:** verify `price_snapshots` has recent rows for each identifier "
                    f"and that `compute_portfolio` looks them up correctly."
                ),
                labels=["bug", "ops", "data-quality"],
            )

        if portfolio_snap.stale_priced_tickers:
            stale_priced_list = ", ".join(portfolio_snap.stale_priced_tickers)
            logger.warning(
                "report %s: %d holding(s) have stale price data (>4 days): %s",
                report.id,
                len(portfolio_snap.stale_priced_tickers),
                stale_priced_list,
            )
            send_ops_alert(
                subject=f"[Portfonia] price data stale — {len(portfolio_snap.stale_priced_tickers)} holding(s)",
                body=(
                    f"Report {report.id} ({report.report_date}) used price data older than "
                    f"4 calendar days for the following holdings:\n\n"
                    + "\n".join(f"  - {t}" for t in portfolio_snap.stale_priced_tickers)
                    + "\n\nHoldings are included in totals but valuations may not reflect "
                    "recent market moves.\n\nCheck price capture logs for these tickers."
                ),
                idempotency_key=f"ops-price-stale-{report.id}",
            )

        # FX stale check: if rates trail the window cutoff, valuation in non-USD
        # currencies is based on stale exchange rates — alert ops but don't block.
        fx_date_str = ctx.portfolio_summary.get("fx_date", "")
        if fx_date_str and _fx_is_stale(fx_date_str, period_end.isoformat()):
            send_ops_alert(
                subject=f"[Portfonia] FX rates stale — report {report.report_date}",
                body=(
                    f"Report {report.id} ({report.report_date}): FX rates are as of "
                    f"{fx_date_str}, which trails the window cutoff. "
                    f"CNY/HKD portfolio values and the FX footer note will reflect "
                    f"stale exchange rates.\n\n"
                    f"Likely cause: capture_fx_task missed or failed. "
                    f"Check worker.log and run capture_fx_task.apply() to backfill."
                ),
                idempotency_key=f"ops-fx-stale-{report.id}",
            )

        logger.info("report %s: loading windowed news", report.id)
        news_items = load_news_window(session, period_start, period_end, user_id)
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
            # H-DEBT-3 (#30): mark this window's news as surfaced in the same
            # transaction as the status commit, so the two can never diverge.
            mark_news_surfaced(session, user_id, report.id, [item.url_hash for item in news_items])
            session.commit()
            log_ops_event("report.generate.end", report_id=str(report.id), status="skipped")
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
                if not send_report_email(report, session):
                    logger.warning(
                        "report %s: email sent but state unconfirmed (commit failed)",
                        report.id,
                    )
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
            pin_provider=False,
            provider_order=_BYOK_PROVIDER_ORDER,
            allow_fallbacks=False,
            enforce_data_collection=False,
            disable_reasoning=True,
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
        # 4. Tavily search  — daily budget enforced across runs
        # ------------------------------------------------------------------
        used_today = _tavily_used_today(session, eff_date, exclude_report_id=report.id)
        daily_remaining = max(0, settings.TAVILY_DAILY_BUDGET - used_today)
        if ctx.search_queries:
            logger.info(
                "report %s: running %d Tavily queries (daily budget %d, used today %d, remaining %d)",
                report.id,
                len(ctx.search_queries),
                settings.TAVILY_DAILY_BUDGET,
                used_today,
                daily_remaining,
            )
            search_results = _run_tavily_search(ctx.search_queries, budget=daily_remaining)
        else:
            search_results = []
        ctx.search_results = search_results

        # ------------------------------------------------------------------
        # 5. Holding-relevant news enrichment (R-3) — anomaly-driven
        # ------------------------------------------------------------------
        # After we know WHICH holdings moved, recall window news relevant to each
        # (mapping gap: a captured story that matched no macro theme), and for the
        # most-moved holdings the store has NOTHING for, run a targeted live
        # search (source gap: a window-relevant story the RSS sources never carried).
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
        targeted_remaining = max(0, daily_remaining - len(ctx.search_results))
        if targeted and targeted_remaining > 0:
            tq = [q for _ident, q in targeted][:targeted_remaining]
            logger.info("report %s: %d targeted anomaly searches", report.id, len(tq))
            targeted_results = _run_tavily_search(tq, budget=targeted_remaining)
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
        final_status = "needs_review" if violations else "success"
        report.status = final_status
        report.report_md = full_md
        report.report_inputs = ctx.to_jsonb()
        report.generated_at = datetime.now(tz=UTC)
        # H-DEBT-3 (#30): mark this window's news as surfaced in the same
        # transaction as the status commit, so the two can never diverge.
        mark_news_surfaced(session, user_id, report.id, [item.url_hash for item in news_items])
        session.commit()
        log_ops_event("report.generate.end", report_id=str(report.id), status=final_status)

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
            if not send_report_email(report, session):
                logger.warning(
                    "report %s: email sent but state unconfirmed (commit failed)",
                    report.id,
                )
        except Exception:
            logger.exception("report %s: email send raised unexpectedly", report.id)

        return report

    except Exception:
        logger.exception("report %s: generation failed", report.id)
        report.status = "failed"
        report.report_inputs = ctx.to_jsonb()
        log_ops_event("report.generate.end", report_id=str(report.id), status="failed")
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
    log_ops_event("report.regenerate.start", report_id=str(report_id), mode=mode)
    report = session.execute(
        select(Report).where(Report.id == report_id, Report.user_id == user_id)
    ).scalar_one_or_none()
    if report is None:
        raise ValueError(f"report {report_id} not found")
    inputs = cast(ReportInputsDict | None, report.report_inputs)
    if not inputs or not inputs.get("pass2_raw"):
        raise ValueError(f"report {report_id} has no stored Pass 2 body to regenerate from")

    portfolio = inputs.get("portfolio_summary", {})
    news_items = inputs.get("news_items", [])

    if mode == "analyze":
        # Refresh portfolio from the live DB so holdings changes between the
        # original generation and this regenerate are picked up (ticker fixes,
        # broker corrections, new/removed rows). Pass 2 and §1 both use it.
        stored_base_ccy = inputs.get("portfolio_summary", {}).get("base_currency", "USD")
        fresh_snap = compute_portfolio(
            session,
            user_id=user_id,
            base_currency=stored_base_ccy,
            as_of=report.period_end.astimezone(ET).date() if report.period_end else None,
        )
        portfolio = _serialize_portfolio(fresh_snap)

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
        # Recompute technical positions from the live DB so a backfill run
        # between the original generation and this regenerate is reflected.
        fresh_technical = _serialize_technical(
            compute_technical_positions(session, portfolio.get("holdings", []), report.report_date)
        )
        # New dict identity so SQLAlchemy flags the JSONB column dirty (an
        # in-place mutation of the existing dict would not be detected).
        report.report_inputs = {
            **inputs,
            "pass2_raw": raw_body,
            "pass2_prompt": pass2_user,
            "llm_calls": regen_calls,
            "technical_positions": fresh_technical,
            "portfolio_summary": portfolio,
        }
        technical_positions = fresh_technical
    elif mode == "render":
        raw_body = inputs["pass2_raw"]
        technical_positions = inputs.get("technical_positions", [])
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
        technical_positions,
        inputs.get("forward_events", []),
        str(inputs.get("price_data_through", "")),
    )
    report.status = "needs_review" if violations else "success"
    report.report_md = full_md
    # Persist translation snapshot alongside report_md for compliance traceability.
    if report.report_inputs is not None:
        report.report_inputs = {**report.report_inputs, "pass2_translated": translated_body}
    report.generated_at = datetime.now(tz=UTC)
    session.commit()
    logger.info("report %s: regenerated (mode=%s, lang=%s)", report.id, mode, output_lang)
    return report
