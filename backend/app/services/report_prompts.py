"""Pass 1 / Pass 2 LLM prompt text: system prompts, task instructions, and the
functions that assemble the user-turn prompt from portfolio/macro/search data.

Split out of report_generator.py (#37).
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from app.compliance.forbidden_vocab import PROMPT_VOCAB_STRING as _FORBIDDEN_PROMPT_VOCAB
from app.core.timezones import ET
from app.services.i18n_glossary import load_i18n_glossary
from app.services.macro_detector import MacroSignals
from app.services.news_fetcher import NewsItem

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
# same restart-to-pick-up-a-YAML-edit caveat as report_sections.
# _RELEASE_DELAY_TERMS and output_scan._STRAY_TAGS/_BODY_DISCLAIMER_RE; see
# i18n_glossary.py's module docstring).
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


# ---------------------------------------------------------------------------
# Stale ticker type hint (used inline in the Pass 2 prompt)
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
