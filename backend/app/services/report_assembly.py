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
from app.services.i18n_glossary import load_i18n_glossary
from app.services.report_llm import _call_llm
from app.services.report_prompts import (
    _COMPLIANCE_SYSTEM_PREFIX,
    _RULE_CONFIDENCE_LABELS,
    _RULE_TIME_REFERENCES,
    _SHARED_BODY_RULES,
    _stale_ticker_hint,
)

logger = logging.getLogger(__name__)

# Bumped when the assembly prompt's contract changes. Recorded on the report
# row alongside the body so a stored report says which contract produced it.
ASSEMBLY_PROMPT_VERSION = "a4-v1"

# The assembly pass writes the same §2/§3/§4 body Pass 2 writes — same
# markers, same downstream injection points — so it reuses Pass 2's narrative
# rules verbatim (report_prompts._SHARED_BODY_RULES) and adds only what makes
# this pass different: restate the supplied conclusions, do not re-derive
# them.
_ASSEMBLY_SYSTEM = (
    _COMPLIANCE_SYSTEM_PREFIX
    + _SHARED_BODY_RULES
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


def build_assembly_prompt(
    portfolio: dict[str, Any],
    price_anomalies: list[dict[str, Any]],
    ticker_intel: dict[str, str],
    macro_event_intel: dict[str, dict[str, Any]],
    macro_event_exposure: dict[str, list[str]],
    period_start: str = "",
    period_end: str = "",
    trading_days: int = 0,
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
    lines.append(f"=== PORTFOLIO (base: {base_ccy}) ===")
    lines.append(f"Total: {base_ccy} {total:,.0f}")
    lines.append("")
    lines.append("Holdings, largest first (weight is what this report should prioritize by):")
    holdings = sorted(
        portfolio.get("holdings", []),
        key=lambda h: _weight(h, total),
        reverse=True,
    )
    for h in holdings:
        # Fund-only rows (no ticker) still need an identifier printed, or the
        # model has no way to connect this line's holding name to an L1 entry
        # keyed by fund_code below (review round-1 finding, PR #163).
        ident = h.get("ticker") or h.get("fund_code")
        lines.append(
            f"  {h.get('name', '')}"
            + (f" ({ident})" if ident else "")
            + f" — {_weight(h, total):.1%} of portfolio"
            + (f" | asset_class: {h['asset_class']}" if h.get("asset_class") else "")
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
    # actually holds, and ordered by how much of the portfolio they touch.
    lines.append("")
    lines.append("=== SHARED MACRO EVENT INTEL (already analyzed — restate, do not re-derive) ===")
    exposed_keys = [k for k in macro_event_intel if macro_event_exposure.get(k)]
    if exposed_keys:
        for key in sorted(exposed_keys, key=lambda k: len(macro_event_exposure[k]), reverse=True):
            intel = macro_event_intel[key]
            lines.append(f"{key}:")
            lines.append(f"  {intel.get('analysis', '')}")
            lines.append(
                "  your exposure (asset classes you hold that this event bears on): "
                + ", ".join(macro_event_exposure[key])
            )
    else:
        lines.append("(no macro event this period bears on an asset class in this portfolio)")

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
    lines.append(
        "Write sections §2, §3 and §4 of the financial analysis briefing in Markdown, "
        "using the headings '## §2 Macro Signals', '## §3 Holdings Analysis' and "
        "'## §4 Risk Radar'.\n"
        "Your job is assembly, not investigation: the analysis above has already "
        "established what happened and why. Select what matters for THIS portfolio, "
        "connect each supplied conclusion to the specific holdings it bears on, and "
        "prioritize by weight and exposure.\n\n"
        + _RULE_TIME_REFERENCES
        + _RULE_CONFIDENCE_LABELS
        + "## §2 Macro Signals\n"
        "For each supplied macro event: restate what it is and trace the "
        "transmission mechanism to the named holdings it reaches through the stated "
        "exposure. Separate short-term (this period), medium-term (weeks to a "
        "quarter) and structural reads, and end with the follow-on signals worth "
        "watching. Report what to WATCH, never what to DO.\n\n"
        "## §3 Holdings Analysis\n"
        "Take the holdings the supplied intel actually covers, heaviest weight "
        "first. For each: why it surfaced, the mechanism linking the development to "
        "that holding, how it sits against the rest of the portfolio "
        "(concentration, currency), and what would confirm or dissolve the read. "
        "Depth over breadth. End each causal attribution with its confidence "
        "label.\n\n"
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
        _ASSEMBLY_SYSTEM,
        prompt,
        with_holdings=True,
        usage_sink=usage_sink,
    )
