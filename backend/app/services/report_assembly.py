"""Second-layer personalized assembly (issue #128 A4 — design doc §6,
Hermes/Portfonia/Docs/Ring 1-A design.md).

Replaces the single giant Pass 2 call — which inferred every fact AND wrote
every sentence, per user — with a narrower pass over facts that L1/L2 have
already inferred once for everybody:

    §1/§4.2/§4.4/§2.5/footer   code-built, unchanged, never LLM-written
    L1 ticker intel            shared, one analysis per (identifier, day)
    L2 macro-event intel       shared, one inference per (event, day)
    portfolio weights          per-user — the actual personalization
                    -> one short assembly pass -> §2/§3/§4 body

WHERE THE SAVING COMES FROM (design doc §6.3): the task boundary narrows,
not the model. This pass never sees the raw news corpus or the search
snippets — those were digested into L1/L2 already — so its prompt is a
fraction of Pass 2's and its job is restatement rather than inference. That
is also why `build_assembly_prompt` has no `news_items`/`search_results`
parameter: re-admitting them would quietly restore Pass 2's token profile
while looking like a feature.

THE TYPE BOUNDARY, INVERTED (design doc §4.8's rule applied to A4's shape):
A2/A3 had to stop per-user values from getting INTO a shared cache. A4 never
writes a cache — it reads two of them and mixes in per-user holdings, so its
failure mode is the mirror image: assembling ANOTHER user's shared rows into
this user's report, which is design doc §1.3's cross-user leak resurfacing at
the last checkpoint. `build_assembly_prompt` therefore takes no `Session` and
this module imports no ORM model: with no DB handle there is no way to ask
for "everything cached today", only for what the per-user caller passed in —
and what it passes in (`ctx.ticker_intel`, `ctx.macro_event_intel`) is
already scoped by `l1_identifiers_for_user` / `l2_event_keys_for_user`.
Locked by two structural tests, not by a reviewer's attention.

DEGRADATION IS THE DEFAULT (design doc §6.3, §6.5): `SHARED_COMPUTE_ENABLED`
gates the whole path, `should_use_assembly` refuses to assemble from empty
caches or without a chosen model, and the caller falls back to Pass 2 if the
assembled body fails the same completeness guard Pass 2 answers to. The worst
case of turning this on is the pre-A4 report, never a thinner one.

COMPLIANCE (design doc §6.3): the pass carries portfolio weights, so
`with_holdings=True` and `OPENROUTER_DATA_COLLECTION=deny` stays ENFORCED —
it does NOT inherit the BYOK exception scoped to Pass 1 + translation. The
assembled body then goes through the identical `_render_full_md` path as
Pass 2's, so the Layer-4 output scan, the code-built sections and the single
footer disclaimer all apply unchanged.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from openai import OpenAI

from app.core.timezones import ET
from app.services._yfinance import _normalize_hk_ticker
from app.services.analysis_framework import load_analysis_framework
from app.services.i18n_glossary import load_i18n_glossary
from app.services.report_llm import _call_llm
from app.services.report_prompts import (
    _COMPLIANCE_SYSTEM_PREFIX,
    _RULE_CONFIDENCE_LABELS,
    _RULE_TIME_REFERENCES,
    _SHARED_BODY_RULES_NO_LARGE_HOLDINGS,
    _build_investor_preferences_block,
    _stale_ticker_hint,
)
from app.services.transmission_taxonomy import transmissions_for_classes

logger = logging.getLogger(__name__)

# Bumped when the assembly prompt's contract changes. Recorded on the report
# row alongside the body so a stored report says which contract produced it.
# a4-v1 -> a4-v2 (issue #128 quality gate, PR #167 review round 3, nit): the
# user-turn contract grew a CROSS-NAME MECHANISM block, closed-set
# TRANSMISSION labels, TRACKING POSITION display rules, and a TECHNICAL
# POSITION block — a stale version string would make a pre- and
# post-quality-gate assembled report indistinguishable in `report_inputs`.
# a4-v3 -> a4-v4 (issue #129 checkpoint B6, PR #212 review finding): the
# original implementation only wired investor-preference injection into the
# Pass 2 fallback branch, so an assembled body silently ignored it and its
# report_inputs snapshot stayed unset. build_assembly_prompt() now takes the
# same investor_locale/investor_questionnaire/investor_free_text params as
# _build_pass2_prompt and renders the same _build_investor_preferences_block.
#
# a4-v2 -> a4-v3 (issue #128 Ring 1 stage B / B1 PR, Grok review PR #172):
# _build_assembly_system() now injects the analysis framework basis and the
# §2 "no direct holding mapping -> no standalone paragraph" tightening — a
# real system-prompt contract change caught by review for not bumping this
# constant, the same class of gap _PROMPT_VERSION's own f2-v6 comment
# documents on the Pass 2 side.
ASSEMBLY_PROMPT_VERSION = "a4-v4"


def _build_assembly_system() -> str:
    """Compose the assembly pass's system prompt fresh on every call.

    The assembly pass writes the same §2/§3/§4 body Pass 2 writes — same
    markers, same downstream injection points — so it reuses Pass 2's
    narrative rules (report_prompts._SHARED_BODY_RULES_NO_LARGE_HOLDINGS —
    the same eight rules Pass 2 uses, minus the LARGE HOLDINGS WINDOW PRICE
    references neither `build_assembly_prompt` below nor its caller ever has
    data for; see that constant's own comment in report_prompts.py) and adds
    only what makes this pass different: restate the supplied conclusions,
    do not re-derive them.

    Same analysis-framework injection and hot-reload contract as
    report_prompts._build_pass2_system (issue #128 Ring 1 stage B,
    checkpoint B1), and the SAME loader call, not a second copy of the
    framework text (§3.3(3): the two paths must stay in one philosophy, not
    two, even while SHARED_COMPUTE_ENABLED is off and this path does not
    ship).
    """
    return (
        _COMPLIANCE_SYSTEM_PREFIX
        + load_analysis_framework().text
        + "\n"
        + _SHARED_BODY_RULES_NO_LARGE_HOLDINGS
        + (
            "SUPPLIED CONCLUSIONS ARE YOUR ONLY EVIDENCE: the SHARED TICKER INTEL and "
            "SHARED MACRO EVENT INTEL blocks below were produced by a prior analysis "
            "pass over this trading day's news and price data. Restate, connect and "
            "prioritize them for THIS portfolio — do not introduce facts, events, "
            "causes or figures that appear nowhere in the supplied data, and do not "
            "re-derive an explanation the intel already gives. If the supplied intel "
            "does not explain a holding's move, say plainly that no catalyst was "
            "identified and label it [Speculative]; never fill the gap from prior "
            "knowledge.\n"
            "RELEVANCE IS THE PERSONALIZATION: lead with what matters most to this "
            "portfolio by weight and by exposure, not with whatever the intel "
            "happens to list first. A macro event is worth space only to the extent "
            "the stated exposure connects it to holdings actually held.\n"
        )
    )


def parse_shadow_models(raw: str) -> list[str]:
    """Model ids from the comma-separated `ASSEMBLY_SHADOW_MODELS` setting.

    Deduplicated (a repeated entry would bill the same comparison twice)
    while preserving the order given, which is the order the product owner
    reads them side by side in.
    """
    out: list[str] = []
    for part in raw.split(","):
        model = part.strip()
        if model and model not in out:
            out.append(model)
    return out


def should_use_assembly(
    *,
    enabled: bool,
    model: str,
    ticker_intel: dict[str, str],
    macro_event_intel: dict[str, dict[str, Any]],
) -> bool:
    """Whether this run may assemble instead of calling Pass 2.

    Every "no" here degrades to Pass 2, which is the pre-A4 behavior — so a
    false negative costs a normal report, never a broken one.

    `model` empty means the shadow comparison has not yet chosen one
    (design doc §6.3.1 leaves `ASSEMBLY_LLM_MODEL` unset deliberately). That
    is a configuration state, not an error: fall back rather than guess a
    model whose quality on this task nobody has measured.

    Both caches empty means there is nothing this pass could restate — a
    cold cache, a day's cap exhausted, or every candidate blocked. Only ONE
    of them having content is an ordinary day (quiet macro, or holdings that
    all sat still), not a degraded one.
    """
    if not enabled or not model.strip():
        return False
    return bool(ticker_intel) or bool(macro_event_intel)


def _weight(holding: dict[str, Any], total: float) -> float:
    if total <= 0:
        return 0.0
    value = holding.get("market_value_base") or 0.0
    return float(value) / total


def _identifier(holding: dict[str, Any]) -> str:
    """The key a holding is looked up under elsewhere in this pipeline:
    `_normalize_hk_ticker(...).upper()`'d ticker, or fund_code for a
    fund-only row (`_normalize_hk_ticker` passes a non-HK-shaped string —
    including a plain numeric fund code — through unchanged, so this is
    safe for both). Matches `select_user_anomalies`/`compute_global_moves`'s
    key convention, which `ticker_intel.build_l1_facts` already had to
    reconcile once for the same reason (see its docstring on the raw-vs-
    normalized HK ticker bug).

    Review round-1 finding, PR #163: this module previously read `ticker`
    only, so a fund-only row fell out of the weight lookup below entirely
    and silently sorted to the bottom of the L1 block, plus the Holdings
    listing printed no identifier for it at all — nothing in the prompt
    connected "Offshore Fund" prose to an L1 entry keyed by "110011"."""
    raw = holding.get("ticker") or holding.get("fund_code") or ""
    return _normalize_hk_ticker(str(raw)).upper()


# Below this share of the book a holding is treated as a TRACKING POSITION:
# a deliberate near-zero stake used to watch a name that is not owned yet
# (design doc §6.7, ruled by the product owner 2026-08-18 — the real 8/17 book
# carries three of them at ~CNY 383 against ~USD 840k). The ruling is explicit
# that these are legitimate and must stay VISIBLE; what they must not do is
# occupy a `###` section the size of a 22.5% holding's. So this is a DISPLAY
# threshold only — it never touches L1 selection, which deliberately has no
# weight floor on the L2-class-intersection channel precisely so these names
# get analyzed.
_TRACKING_POSITION_MAX_WEIGHT = 0.01


def portfolio_identifiers(portfolio: dict[str, Any]) -> list[str]:
    """Every identifier this portfolio snapshot currently holds, in the
    normalized spelling L1/L2/L3 key everything under.

    Public because `regenerate_report` needs it to re-narrow stored cross-name
    clusters against the FRESH portfolio — the same correction PR #163's round
    2 made for `macro_event_exposure`: a per-user projection replayed from
    storage can name a holding sold between generation and regeneration.
    """
    out: list[str] = []
    for holding in portfolio.get("holdings", []):
        ident = _identifier(holding)
        if ident and ident not in out:
            out.append(ident)
    return out


def _cross_name_instruction(has_clusters: bool) -> str:
    """The §3 instruction covering L3's clusters — present ONLY when clusters
    were actually supplied.

    Asking for a cross-name conclusion when none was established is how an
    invented one gets written: the model reads a standing instruction as a
    requirement to satisfy. When the day produced nothing, the block above
    says so plainly and §3 simply does not raise the subject.
    """
    if not has_clusters:
        return ""
    return (
        "Where the CROSS-NAME MECHANISM block links several of these holdings, "
        "state the shared mechanism ONCE — name the holdings it binds, carry its "
        "confidence label, and say what it would take to confirm or dissolve it — "
        "rather than repeating it under each name separately. Do not extend a "
        "cluster to holdings it does not list, and do not assert a connection the "
        "block does not contain.\n"
    )


def _tracking_identifiers(portfolio: dict[str, Any]) -> set[str]:
    """Identifiers whose weight puts them under the display clamp.

    A holding with no price (excluded from every total, hence weight 0) is NOT
    swept in here: `stale_tickers` already carries its own prompt line saying
    it is unvalued, and labelling it a tracking position would assert
    something about position size that the missing price cannot support.
    """
    total = float(portfolio.get("total_base") or 0.0)
    if total <= 0:
        return set()
    stale = {str(s).upper() for s in portfolio.get("stale_tickers", [])}
    out: set[str] = set()
    for holding in portfolio.get("holdings", []):
        ident = _identifier(holding)
        value = float(holding.get("market_value_base") or 0.0)
        if not ident or ident in stale or value <= 0:
            continue
        if value / total < _TRACKING_POSITION_MAX_WEIGHT:
            out.add(ident)
    return out


def build_assembly_prompt(
    portfolio: dict[str, Any],
    price_anomalies: list[dict[str, Any]],
    ticker_intel: dict[str, str],
    macro_event_intel: dict[str, dict[str, Any]],
    macro_event_exposure: dict[str, list[str]],
    period_start: str = "",
    period_end: str = "",
    trading_days: int = 0,
    technical_positions: list[dict[str, Any]] | None = None,
    cross_name_intel: list[dict[str, Any]] | None = None,
    investor_locale: str | None = None,
    investor_questionnaire: dict[str, Any] | None = None,
    investor_free_text: str | None = None,
) -> str:
    """Assemble the user-turn prompt. Takes no `Session` by design — see the
    module docstring's type-boundary note; every value here arrives already
    scoped to one user by the caller.

    `macro_event_exposure` is L2's per-user half (`user_event_exposure`): the
    cached affected classes intersected with what this user actually holds.
    Events are ordered by that overlap, and an event with none is omitted —
    the shared cache holds a day's events for everybody, and one that touches
    nothing this user owns is noise in this user's report.
    """
    lines: list[str] = []

    lines.append("=== REPORT WINDOW ===")
    # Rendered in ET, matching Pass 2's own window line exactly (review
    # round-1 finding, PR #163: this previously printed the raw UTC ISO
    # strings, which disagreed with every other ET-labeled timestamp the
    # model is given elsewhere in the pipeline).
    span = "unknown"
    if period_start and period_end:
        ps_et = datetime.fromisoformat(period_start).astimezone(ET).strftime("%Y-%m-%d %H:%M")
        pe_et = datetime.fromisoformat(period_end).astimezone(ET).strftime("%Y-%m-%d %H:%M")
        span = f"{ps_et} to {pe_et} ET"
    lines.append(f"This report covers {span} ({trading_days} trading day(s)).")
    lines.append("")

    total = float(portfolio.get("total_base") or 0.0)
    base_ccy = portfolio.get("base_currency", "USD")
    fx_date = portfolio.get("fx_date", "unknown")
    # §4.3 asks for an FX note; fx_date is the only FX fact the snapshot
    # carries, and Pass 2's header includes it (review round-2 finding, PR
    # #163: this prompt previously omitted it entirely).
    lines.append(f"=== PORTFOLIO (base: {base_ccy}, FX date: {fx_date}) ===")
    lines.append(f"Total: {base_ccy} {total:,.0f}")
    lines.append("")
    lines.append("Holdings, largest first (weight is what this report should prioritize by):")
    tracking = _tracking_identifiers(portfolio)
    holdings = sorted(
        portfolio.get("holdings", []),
        key=lambda h: _weight(h, total),
        reverse=True,
    )
    for h in holdings:
        # Printed via `_identifier()` — the SAME key the L1 weight lookup
        # and L1 block below use — not the raw `ticker`/`fund_code` value.
        # A fund-only row needs an identifier printed at all (round 1
        # finding); a raw un-normalized HK ticker ("700.HK") needs the SAME
        # spelling L1 keys everything under ("0700.HK"), or the model can't
        # connect this line to its L1 entry either (round 2 nit, PR #163 —
        # the same join failure, one spelling short of round 1's fix).
        ident = _identifier(h) or None
        lines.append(
            f"  {h.get('name', '')}"
            + (f" ({ident})" if ident else "")
            + f" — {_weight(h, total):.1%} of portfolio"
            + (f" | asset_class: {h['asset_class']}" if h.get("asset_class") else "")
            + (" | TRACKING POSITION" if ident and ident in tracking else "")
        )
    lines.append("")
    lines.append(f"By asset class: {portfolio.get('by_asset_class', {})}")
    lines.append(f"By currency: {portfolio.get('by_currency', {})}")

    # Which holdings have no usable price and are excluded from every total —
    # the model must not describe one as if it were valued (review round-1
    # finding, PR #163: Pass 2 carries this, the assembly prompt did not).
    stale = portfolio.get("stale_tickers", [])
    if stale:
        lines.append("Stale/no-price identifiers (excluded from valuations):")
        vendor_zh = load_i18n_glossary().vendor_names["Tiantian Fund"]["zh-Hans"]
        for ident in stale:
            lines.append(f"  - {_stale_ticker_hint(ident, vendor_zh)}")

    conc = portfolio.get("concentration", {})
    if conc.get("single_holding_watch") or conc.get("top3_watch") or conc.get("asset_class_watch"):
        lines.append("")
        lines.append("Concentration flags (state these ratios in §4.1):")
        if conc.get("single_holding_high"):
            lines.append(
                f"  [!] Top holding {conc.get('top_holding_name')} "
                f"({conc.get('top_holding_asset_class')}) = "
                f"{conc.get('top_holding_ratio', 0):.1%} — above this asset class's high threshold"
            )
        elif conc.get("single_holding_watch"):
            lines.append(
                f"  Top holding {conc.get('top_holding_name')} "
                f"({conc.get('top_holding_asset_class')}) = "
                f"{conc.get('top_holding_ratio', 0):.1%} — above this asset class's watch threshold"
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

    # L1: the per-identifier "what happened" analysis, already inferred once
    # for every user holding it. Ordered by this user's own weight so the
    # pass reads the portfolio's biggest positions first.
    lines.append("")
    lines.append("=== SHARED TICKER INTEL (already analyzed — restate, do not re-derive) ===")
    if ticker_intel:
        # Keyed via `_identifier` (ticker OR fund_code), not `ticker` alone —
        # a fund-only row previously fell out of this map entirely and
        # sorted to the bottom of the L1 block regardless of its actual size
        # (review round-1 finding, PR #163).
        weight_by_ident = {
            _identifier(h): _weight(h, total)
            for h in portfolio.get("holdings", [])
            if h.get("ticker") or h.get("fund_code")
        }
        for ident in sorted(
            ticker_intel, key=lambda i: weight_by_ident.get(i.upper(), 0.0), reverse=True
        ):
            lines.append(f"{ident}:")
            lines.append(f"  {ticker_intel[ident]}")
    else:
        lines.append("(no per-holding intel available for this period)")

    # L2: macro/calendar events, filtered to those touching classes this user
    # actually holds, and ordered by how much of the portfolio they touch —
    # the SUM of overlapping classes' weight, not how many classes overlap
    # (review round-2 finding, PR #163: a class-count sort could rank an
    # event touching two 3% sleeves above one touching a single 80% sleeve,
    # inverting the "lead by weight" personalization this docstring and the
    # system prompt both promise).
    lines.append("")
    lines.append("=== SHARED MACRO EVENT INTEL (already analyzed — restate, do not re-derive) ===")
    exposed_keys = [k for k in macro_event_intel if macro_event_exposure.get(k)]
    by_asset_class = portfolio.get("by_asset_class", {})

    def _exposure_weight(key: str) -> float:
        return sum(float(by_asset_class.get(c, 0.0)) for c in macro_event_exposure[key])

    if exposed_keys:
        for key in sorted(exposed_keys, key=_exposure_weight, reverse=True):
            intel = macro_event_intel[key]
            lines.append(f"{key}:")
            lines.append(f"  {intel.get('analysis', '')}")
            lines.append(
                "  your exposure (asset classes you hold that this event bears on): "
                + ", ".join(macro_event_exposure[key])
            )
            channels = transmissions_for_classes(macro_event_exposure[key])
            if channels:
                lines.append("  TRANSMISSION (code-built, closed set): " + ", ".join(channels))
    else:
        lines.append("(no macro event this period bears on an asset class in this portfolio)")

    # L3: the day's cross-name conclusions, already narrowed by
    # `cross_name_intel.clusters_for_user` to clusters with at least two of
    # THIS user's own L1 names. This block is what makes a stack sentence
    # possible at all: without it the pass has per-name notes and per-event
    # notes and is contractually barred from drawing the edge between them
    # itself (design doc §6.7). The summaries are written about the mechanism
    # and carry no identifiers; the member names come from the structured
    # list, which is the half that could be filtered.
    lines.append("")
    lines.append("=== CROSS-NAME MECHANISM (already inferred — restate, do not re-derive) ===")
    clusters = list(cross_name_intel or [])
    if clusters:
        for cluster in clusters:
            members = ", ".join(str(i) for i in cluster.get("identifiers", []))
            mechanism = str(cluster.get("mechanism", ""))
            confidence = str(cluster.get("confidence", "Speculative"))
            lines.append(f"{mechanism} [{confidence}]")
            lines.append(f"  your holdings on this channel: {members}")
            lines.append(f"  mechanism: {cluster.get('summary', '')}")
    else:
        lines.append(
            "(no cross-holding mechanism was established for this period — do not assert one)"
        )

    # Per-user anomaly list: which holdings crossed THIS user's thresholds.
    # The numeric table under §4.2 is code-built downstream from this same
    # data, so the body must reference it, never restate its figures.
    lines.append("")
    lines.append("=== PRICE ANOMALIES (this portfolio, over the report window) ===")
    if price_anomalies:
        for a in price_anomalies:
            net = a.get("window_net_pct")
            net_str = f"{net * 100:+.2f}%" if net is not None else "n/a"
            lines.append(
                f"  {a.get('name', '')} ({a.get('identifier', '')}) — window net {net_str}"
            )
    else:
        lines.append("(no holding moved beyond its threshold this window)")

    lines.append("")
    lines.append("=== TECHNICAL POSITION (code-built OHLCV facts, not signals) ===")
    tech_rows = list(technical_positions or [])
    if tech_rows:
        for t in tech_rows:
            ident = t.get("ticker") or t.get("identifier") or ""
            bits: list[str] = []
            sma50 = t.get("pct_vs_sma50")
            sma200 = t.get("pct_vs_sma200")
            rng = t.get("pct_in_52w_range")
            vol = t.get("vol_20d_annualized")
            if sma50 is not None:
                bits.append(f"vs 50-day avg {float(sma50):+.1%}")
            if sma200 is not None:
                bits.append(f"vs 200-day avg {float(sma200):+.1%}")
            if rng is not None:
                bits.append(f"in 52-week range {float(rng):.0%}")
            if vol is not None:
                bits.append(f"20-day vol {float(vol):.0%}")
            if ident and bits:
                lines.append(f"  {ident}: " + "; ".join(bits))
    else:
        lines.append("(no technical-position facts supplied)")

    # Investor preferences (issue #129 checkpoint B6, decision point 6,
    # corrected 2026-08-25): same block and same SCOPE guardrail as Pass 2's
    # _build_investor_preferences_block (report_prompts.py) — assembly must
    # not silently skip this just because it is a different body-writing
    # pass (PR #212 review finding: the original implementation only wired
    # this into the Pass 2 fallback branch).
    preferences_block = _build_investor_preferences_block(
        investor_locale, investor_questionnaire, investor_free_text
    )
    if preferences_block:
        lines.append(preferences_block)

    lines.append("")
    lines.append(
        "Write sections §2, §3 and §4 of the financial analysis briefing in Markdown, "
        "using the headings '## §2 Macro Signals', '## §3 Holdings Analysis' and "
        "'## §4 Risk Radar'.\n"
        "Your job is assembly, not investigation: the analysis above has already "
        "established what happened and why. Select what matters for THIS portfolio, "
        "connect each supplied conclusion to the specific holdings it bears on "
        "(including a large holding that has L1 intel even if it did not print a "
        "price anomaly), and prioritize by weight and exposure. You MUST NOT invent "
        "facts, events, figures or catalysts that appear nowhere in the supplied "
        "blocks. You MAY connect facts that ARE supplied: an L2 event plus its "
        "TRANSMISSION labels plus an L1 entry on a related holding is one chain, "
        "not three unrelated notes. Do not introduce new facts.\n\n"
        + _RULE_TIME_REFERENCES
        + _RULE_CONFIDENCE_LABELS
        + "## §2 Macro Signals\n"
        "From the supplied macro events, select the ones — typically 2 to 4 — "
        "that the supplied intel shows a genuine, evidenced change in this period; "
        "apply the analysis framework's own judgment (see ANALYSIS FRAMEWORK above) "
        "for how much space each earns. Write each selected event as ONE flowing "
        "paragraph, not a bulleted or sub-headed structure: restate what it is and "
        "trace the transmission mechanism to the named holdings it reaches through "
        "the stated exposure, letting near-term, medium-term and structural "
        "significance appear in whatever order and proportion the supplied intel "
        "supports — do not label them and do not add a sub-heading before naming "
        "holdings. An event with no material change this period does not need its "
        "own paragraph. An event with no direct, concrete mapping to a holding "
        "does not earn its own paragraph by default, regardless of how much change "
        "it shows elsewhere — at most, mention it as one aside inside the relevant "
        "holding's §3 analysis. Report what to WATCH, never what to DO.\n\n"
        "## §3 Holdings Analysis\n"
        "Take the holdings the supplied intel actually covers, heaviest weight "
        "first. For each: why it surfaced, the mechanism linking the development to "
        "that holding, how it sits against the rest of the portfolio "
        "(concentration, currency), and what would confirm or dissolve the read. "
        "Depth over breadth. End each causal attribution with its confidence "
        "label.\n"
        + _cross_name_instruction(bool(clusters))
        + "A holding marked TRACKING POSITION gets ONE line at most and never a "
        "heading of its own — it is a deliberate watch-only stake, so it stays "
        "visible but must not take the space of a real position.\n\n"
        "## §4 Risk Radar\n"
        "### 4.1 Concentration — state the flagged ratios above.\n"
        "### 4.2 Price anomalies — a numeric table is inserted by the system "
        "directly under this heading; do NOT restate those numbers. Write ONE line "
        "per holding in PRICE ANOMALIES, formatted 'IDENTIFIER — <driver> [Label]', "
        "where <driver> attributes the move to the supplied intel in one sentence. "
        "If the intel identifies no catalyst, say so plainly and label it "
        "[Speculative]; never invent one. If PRICE ANOMALIES is empty, say so "
        "plainly for this window — do NOT phrase it as 'today'.\n"
        "### 4.3 FX exposure — state currency exposures and any FX note.\n"
        "Throughout §4: state the numbers; never editorialize about what to do."
    )
    return "\n".join(lines)


def run_assembly_pass(
    client: OpenAI,
    model: str,
    prompt: str,
    usage_sink: list[dict[str, Any]] | None = None,
) -> str:
    """Run one assembly pass.

    `with_holdings=True` and no provider/data-collection overrides: this
    payload carries portfolio weights, so `deny` stays enforced and the BYOK
    exception scoped to Pass 1 + translation deliberately does not apply
    (design doc §6.3).
    """
    return _call_llm(
        client,
        model,
        _build_assembly_system(),
        prompt,
        with_holdings=True,
        usage_sink=usage_sink,
    )
