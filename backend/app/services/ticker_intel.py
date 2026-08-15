"""L1 shared ticker-level intel cache (issue #128, Ring 1 A2 — design doc
§4, Hermes/Portfonia/Docs/Ring 1-A design.md).

This is a NEW analysis stage, not a refactor: before A2 there was no
per-identifier "what happened to this security this window" pass — Pass 2
did all narrative work in one per-user call. `get_l1_intel_batch` computes
that analysis exactly once per (identifier, trade_date, prompt_version) and
caches it, so a multi-user fan-out that shares an identifier (two users both
holding NVDA) pays for one LLM call, not one per user.

Hard constraint (design doc §4.3): the L1 prompt must carry NO per-user
data — no position size, portfolio weight, account value, or holder count —
because the cached result is reused verbatim across every user who holds the
identifier. `L1Facts` is a fixed dataclass of public, window-level facts
only; there is no field a caller could populate to leak per-user data into
the prompt (see test_ticker_intel.py's structural + prompt-content tests).

Compliance (design doc §4.3): a cached entry that later ships to N users
multiplies the blast radius of a forbidden-vocabulary slip by N. The output
is scanned with the same Layer-4 backstop the report body uses
(`_scan_forbidden_output`) BEFORE it is written to the cache — a violation is
never cached, ops is alerted, and the caller simply gets no L1 intel for
that identifier this run (report generation is not blocked by this).

Model/compliance parameters (design doc §4.3): `data_collection=deny` stays
enforced (no BYOK exception — identifiers are holdings-derived, unlike Pass
1's public-only inputs). Model choice is `LOW_COST_LLM_MODEL`: this is a
narrower, more mechanical task than Pass 2 ("what happened to this one
identifier", not a full cross-holding narrative), and A2's whole point is
cost reduction — no dedicated ASSEMBLY_LLM_MODEL-style setting was
introduced for A2 (that pattern is reserved for A4's personalization pass,
design doc §6.3, whose scope is different enough to warrant its own
shadow-compared setting).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.compliance.output_scan import _scan_forbidden_output
from app.core.config import get_settings
from app.models.ticker_intel import TickerIntel
from app.services.email_sender import send_ops_alert
from app.services.report_llm import _call_llm, _openrouter_client
from app.services.report_prompts import _COMPLIANCE_SYSTEM_PREFIX

logger = logging.getLogger(__name__)

# Bumping this deliberately stops serving old cache entries under a new
# prompt contract (design doc §4.4) — it is part of the table's unique key,
# not just an audit column.
_PROMPT_VERSION = "l1-v1"

# Per-day cap on FRESH LLM analyses (design doc §4.3) — cache hits are free
# and never count against this, so a broad anomaly day degrades to "some
# identifiers have no L1 intel" rather than an unbounded cost spike.
_MAX_L1_ANALYSES_PER_DAY = 15

_L1_SYSTEM = _COMPLIANCE_SYSTEM_PREFIX + (
    "\nYou are writing a short SHARED factual briefing about a single security "
    "or theme for an internal cache. This text will be reused verbatim across "
    "every user in the system who holds it — it must contain NOTHING specific "
    "to any one user: no position size, portfolio weight, account value, or "
    "how many people hold it. Describe ONLY what happened to this security in "
    "the given window, using the facts provided.\n"
    "End any causal attribution with a confidence label in square brackets: "
    "[Established] (a named mechanism or citable event drives the move), "
    "[Probable] (partial evidence, not conclusive), or [Speculative] (no "
    "direct evidence — a hypothesis). If no catalyst is identifiable, say so "
    "plainly and use [Speculative]. Write 2-4 sentences, no headings, no "
    "bracketed citations."
)


@dataclass
class L1Facts:
    """Public, non-per-user facts about one identifier for the L1 prompt.

    Every field here is either a window-level price-move fact (shared,
    identical across every user who holds the identifier — the same
    `compute_global_moves` output A1 introduced), a captured headline, or a
    descriptive OHLCV fact (`technical_position.py`). None of it is
    holdings-derived beyond the identifier string itself.
    """

    net_pct: float | None = None
    max_day_pct: float | None = None
    trigger: str = ""
    market: str = ""
    current_price: float | None = None
    prev_price: float | None = None
    latest_date: str = ""
    news_headlines: list[str] = field(default_factory=list)
    pct_vs_sma50: float | None = None
    pct_vs_sma200: float | None = None
    pct_in_52w_range: float | None = None
    vol_20d_annualized: float | None = None

    def to_jsonb(self) -> dict[str, Any]:
        return {
            "net_pct": self.net_pct,
            "max_day_pct": self.max_day_pct,
            "trigger": self.trigger,
            "market": self.market,
            "current_price": self.current_price,
            "prev_price": self.prev_price,
            "latest_date": self.latest_date,
            "news_headlines": self.news_headlines,
            "pct_vs_sma50": self.pct_vs_sma50,
            "pct_vs_sma200": self.pct_vs_sma200,
            "pct_in_52w_range": self.pct_in_52w_range,
            "vol_20d_annualized": self.vol_20d_annualized,
        }


def _build_l1_prompt(identifier: str, facts: L1Facts) -> str:
    # `facts.trigger` (single_day vs cumulative) is deliberately NOT included
    # here (round 2 review finding): it comes from the CALLING USER's own
    # asset_class threshold (window_data.select_user_anomalies) — two holders
    # of the same identifier can classify the identical move differently, and
    # baking the first caller's classification into the shared prompt text
    # would make the cached analysis reflect an arbitrary user's framing.
    # Still recorded in `facts.to_jsonb()` for audit (which user's call
    # produced this row), just not sent to the LLM.
    lines = [f"Identifier: {identifier}", ""]
    if facts.net_pct is not None:
        lines.append(f"Window net price change: {facts.net_pct:+.2%}")
    if facts.max_day_pct is not None:
        lines.append(f"Largest single-day move in window: {facts.max_day_pct:+.2%}")
    if facts.market:
        lines.append(f"Market: {facts.market}")
    if facts.current_price is not None and facts.prev_price is not None:
        lines.append(f"Price: {facts.prev_price} -> {facts.current_price}")
    if facts.latest_date:
        lines.append(f"Latest data date: {facts.latest_date}")
    if facts.pct_vs_sma50 is not None:
        lines.append(f"Distance to 50-day average: {facts.pct_vs_sma50:+.2%}")
    if facts.pct_vs_sma200 is not None:
        lines.append(f"Distance to 200-day average: {facts.pct_vs_sma200:+.2%}")
    if facts.pct_in_52w_range is not None:
        lines.append(f"Position in 52-week range: {facts.pct_in_52w_range:.2%}")
    if facts.vol_20d_annualized is not None:
        lines.append(f"20-day annualized volatility: {facts.vol_20d_annualized:.2%}")
    if facts.news_headlines:
        lines.append("")
        lines.append("Related headlines captured this window:")
        for h in facts.news_headlines:
            lines.append(f"- {h}")
    lines.append("")
    lines.append(
        "Write the briefing described in your system instructions, grounded "
        "ONLY in the facts above."
    )
    return "\n".join(lines)


def _fetch_cached(session: Session, identifier: str, trade_date: date) -> TickerIntel | None:
    """Returns the full row, not just `.analysis` — a row can exist with
    `analysis IS NULL` (an "attempted, no usable result" marker; see
    TickerIntel's docstring), and the caller must be able to tell "never
    attempted today" (no row) apart from "attempted today, nothing to serve"
    (row present, analysis None) to avoid re-attempting the latter."""
    return session.execute(
        select(TickerIntel).where(
            TickerIntel.identifier == identifier,
            TickerIntel.trade_date == trade_date,
            TickerIntel.prompt_version == _PROMPT_VERSION,
        )
    ).scalar_one_or_none()


def _count_analyzed_today(session: Session, trade_date: date) -> int:
    return int(
        session.execute(
            select(func.count())
            .select_from(TickerIntel)
            .where(
                TickerIntel.trade_date == trade_date,
                TickerIntel.prompt_version == _PROMPT_VERSION,
            )
        ).scalar_one()
    )


def _write_cache(
    session: Session,
    identifier: str,
    trade_date: date,
    model: str,
    analysis: str | None,
    facts: L1Facts,
) -> None:
    """`analysis=None` writes the "attempted, no usable result" marker row
    (see TickerIntel's docstring) — used for both an LLM failure and a
    compliance block, so neither is re-attempted by a later user today."""
    stmt = (
        pg_insert(TickerIntel)
        .values(
            identifier=identifier,
            trade_date=trade_date,
            prompt_version=_PROMPT_VERSION,
            model=model,
            analysis=analysis,
            facts=facts.to_jsonb(),
        )
        .on_conflict_do_nothing(constraint="uq_ticker_intel_identifier_date_version")
    )
    session.execute(stmt)
    session.flush()


def _generate(
    session: Session,
    identifier: str,
    trade_date: date,
    facts: L1Facts,
    usage_sink: list[dict[str, Any]] | None = None,
) -> str | None:
    """Call the LLM, gate the output through the compliance scan, and ALWAYS
    write a row — either the real analysis, or (on an LLM failure or a
    compliance block) a null-analysis marker row, so a systematically
    failing/blocked identifier is attempted at most once per day rather than
    once per user in the fan-out (review round 1 fix: the first draft wrote
    nothing on failure, so the daily cap — which counts cache rows — never
    saw these attempts and could be bypassed entirely). Returns None (never
    raises) on either failure mode — both degrade to "no L1 intel this run",
    not a report failure (design doc §4.3).

    `usage_sink` (round 2 review finding): forwarded straight to `_call_llm`,
    same as Pass 1/Pass 2 already do — without it, L1's per-identifier spend
    was invisible in `report_inputs.llm_calls`, so a report's total LLM cost
    audit silently excluded whatever L1 analysis it triggered.
    """
    settings = get_settings()
    model = settings.LOW_COST_LLM_MODEL
    prompt = _build_l1_prompt(identifier, facts)
    try:
        client = _openrouter_client()
        analysis = _call_llm(
            client,
            model,
            _L1_SYSTEM,
            prompt,
            with_holdings=False,
            disable_reasoning=True,
            usage_sink=usage_sink,
        )
    except Exception:
        logger.exception("ticker_intel: L1 analysis call failed for %s", identifier)
        _write_cache(session, identifier, trade_date, model, None, facts)
        return None

    violations = _scan_forbidden_output(analysis)
    if violations:
        logger.error(
            "ticker_intel: L1 output for %s (%s) BLOCKED by compliance scan: %s",
            identifier,
            trade_date,
            violations,
        )
        send_ops_alert(
            subject=f"[Portfonia] L1 shared intel BLOCKED for compliance — {identifier}",
            body=(
                f"L1 shared-cache analysis for identifier {identifier} on {trade_date} "
                f"tripped the forbidden-vocabulary scan: {violations}\n\n"
                f"The forbidden text was NOT stored or served — a null-analysis marker "
                f"row was written for (identifier={identifier}, trade_date={trade_date}, "
                f"prompt_version={_PROMPT_VERSION}) instead, so this identifier will not "
                f"be re-attempted today (do not manually retry it; that would just hit "
                f"the same unique-key row). Report generation for the affected user(s) "
                f"continues without L1 intel for this identifier."
            ),
            idempotency_key=f"ops-l1-blocked-{identifier}-{trade_date}",
        )
        _write_cache(session, identifier, trade_date, model, None, facts)
        return None

    _write_cache(session, identifier, trade_date, model, analysis, facts)
    return analysis


def get_l1_intel_batch(
    session: Session,
    identifiers: list[str],
    trade_date: date,
    facts_by_identifier: dict[str, L1Facts],
    usage_sink: list[dict[str, Any]] | None = None,
) -> dict[str, str]:
    """Read-through cache over `identifiers` (expected ordered by |move|
    descending — see `build_l1_candidates`). A cache HIT — whether it holds
    a real analysis or a null "attempted, no result" marker (see
    TickerIntel's docstring) — never re-calls the LLM: an already-attempted
    identifier is never retried today, success or not. A cache MISS beyond
    the day's remaining fresh-analysis budget is simply skipped (that
    identifier has no L1 intel this run — degrade, don't fail).

    `fresh_budget` is decremented on EVERY fresh attempt via `_generate`,
    regardless of outcome (review round 1 fix): counting only successful
    writes let a systematically-blocked or -failing identifier bypass the
    daily cap entirely, since every user in the fan-out would see "not yet
    attempted" and retry it.

    `usage_sink` (round 2 review finding): forwarded to every fresh
    `_generate` call — a cache HIT makes no LLM call, so nothing to record.
    Pass `ctx.llm_calls` from `generate_report` so a day's L1 spend shows up
    in the SAME report_inputs.llm_calls list Pass 1/Pass 2 already use.

    Sequential fan-out (`generate_incremental_report` processes users one at
    a time, not concurrently) is what actually makes "shared across users"
    hold here: the first user's call caches to the DB before the second
    user's call ever runs, so no in-memory batch cache (unlike A1's
    moves_cache) is needed for the sharing property itself — see design doc
    §4.1 UAT-4 and test_ticker_intel.py's cache-hit tests.
    """
    result: dict[str, str] = {}
    fresh_budget = max(0, _MAX_L1_ANALYSES_PER_DAY - _count_analyzed_today(session, trade_date))
    for identifier in identifiers:
        cached = _fetch_cached(session, identifier, trade_date)
        if cached is not None:
            if cached.analysis is not None:
                result[identifier] = cached.analysis
            continue
        if fresh_budget <= 0:
            logger.info(
                "ticker_intel: daily L1 analysis cap (%d) reached, skipping %s",
                _MAX_L1_ANALYSES_PER_DAY,
                identifier,
            )
            continue
        facts = facts_by_identifier.get(identifier)
        if facts is None:
            continue
        analysis = _generate(session, identifier, trade_date, facts, usage_sink)
        fresh_budget -= 1
        if analysis is not None:
            result[identifier] = analysis
    return result


def build_l1_candidates(
    anomalies: list[dict[str, Any]],
    holding_news: dict[str, list[dict[str, Any]]],
    technical_positions: list[dict[str, Any]],
) -> tuple[list[str], dict[str, L1Facts]]:
    """Candidate identifiers for L1 analysis + their public facts, built from
    the same serialized shapes `generate_report` already holds
    (`ctx.price_anomalies`, `ctx.holding_news`, `ctx.technical_positions`) —
    design doc §4.3: identifiers that triggered a window anomaly this report,
    or that holding-news recall matched.

    Theme-merged anomalies (`window_data._merge_theme_anomalies` — entries
    with a non-empty `constituents` list) are expanded to one candidate PER
    CONSTITUENT, never a candidate under the theme slug itself (round 3
    review bug): the merged entry's own `window_net_pct`/`current_price`/
    `prev_price` are value-weighted by the CALLING USER's own holdings mix
    (weighted/picked by `Holding.current_value`), so two holders of the same
    theme with different constituent weightings would get different
    numbers — exactly the per-user-data-in-a-shared-cache violation §4.3
    forbids. Each constituent's own `pct_change` IS safe (global — set from
    `compute_global_moves` before any per-user merge/weighting happens);
    `max_day_pct`/prices/technicals have no per-constituent global source at
    this level and are left absent rather than inheriting the theme-level
    (per-user) figures. An anomaly with an EMPTY `constituents` list is
    treated as a regular single-ticker anomaly, not a broken theme merge:
    `_serialize_anomalies` renders `a.constituents or []`, so a genuine
    single-ticker entry also serializes with `constituents: []` — there is
    no reliable signal to tell the two apart from an empty list alone, and
    `_merge_theme_anomalies` never actually produces a theme entry with zero
    constituents (a theme bucket only exists if it has >=1 member).

    `holding_news` (round 4 review bug): `generate_report`'s upstream
    recall/targeted-search is still keyed by the theme slug
    (`anomaly_ids`/`_targeted_anomaly_queries` both read `a["identifier"]`
    directly, unaware of the constituent expansion below), so `holding_news`
    arrives here keyed by "gold" etc, not by constituent ticker. Every
    constituent gets the theme slug's headlines (in addition to any headlines
    recalled under its own ticker), and the theme slug itself is tracked in
    `theme_slugs` so the news-only loop below never re-adds it as its own
    candidate — the first draft of the round-3 fix missed this, letting the
    news-only loop silently reintroduce the exact theme-slug candidate the
    anomaly loop had just excluded.

    Ordering: anomaly identifiers first, in their given order (already
    sorted by |move| descending — see `window_data.select_user_anomalies`),
    then any holding-news-only identifiers (no anomaly, so no natural move
    ranking) appended after. This ordering is what `get_l1_intel_batch`'s
    daily-cap truncation uses to prioritize the most-moved identifiers.
    """
    technical_by_ticker = {t["ticker"]: t for t in technical_positions if t.get("ticker")}

    def _apply_technical(facts_obj: L1Facts, identifier: str) -> None:
        tech = technical_by_ticker.get(identifier, {})
        facts_obj.pct_vs_sma50 = tech.get("pct_vs_sma50")
        facts_obj.pct_vs_sma200 = tech.get("pct_vs_sma200")
        facts_obj.pct_in_52w_range = tech.get("pct_in_52w_range")
        facts_obj.vol_20d_annualized = tech.get("vol_20d_annualized")

    order: list[str] = []
    facts: dict[str, L1Facts] = {}
    # Theme slugs must never surface as their own L1 candidate (round 3 fix)
    # — but upstream recall/targeted-search in generate_report is still
    # keyed by the theme slug (a["identifier"]), so holding_news arrives
    # here keyed by "gold" etc, not by constituent ticker. Track slugs seen
    # so the news-only loop below excludes them too (round 4 review bug:
    # without this, "gold" news reintroduced "gold" as its own candidate
    # via that loop, undoing the anomaly-loop fix in the common case).
    theme_slugs: set[str] = set()

    for a in anomalies:
        identifier = a.get("identifier")
        if not identifier:
            continue
        constituents = a.get("constituents") or []
        if constituents:
            theme_slugs.add(identifier)
            theme_headlines = [n.get("title", "") for n in holding_news.get(identifier, [])]
            for c in constituents:
                c_identifier = c.get("identifier")
                if not c_identifier or c_identifier in facts:
                    continue
                order.append(c_identifier)
                own_headlines = [n.get("title", "") for n in holding_news.get(c_identifier, [])]
                c_facts = L1Facts(
                    net_pct=c.get("pct_change"),
                    news_headlines=own_headlines + theme_headlines,
                )
                _apply_technical(c_facts, c_identifier)
                facts[c_identifier] = c_facts
            continue
        if identifier in facts:
            continue
        order.append(identifier)
        new_facts = L1Facts(
            net_pct=a.get("window_net_pct"),
            max_day_pct=a.get("max_day_pct"),
            trigger=a.get("trigger") or "",
            market=a.get("market") or "",
            current_price=a.get("current_price"),
            prev_price=a.get("prev_price"),
            latest_date=a.get("latest_date") or "",
            news_headlines=[n.get("title", "") for n in holding_news.get(identifier, [])],
        )
        _apply_technical(new_facts, identifier)
        facts[identifier] = new_facts

    for identifier, items in holding_news.items():
        if identifier in facts or identifier in theme_slugs:
            continue
        order.append(identifier)
        news_only_facts = L1Facts(news_headlines=[n.get("title", "") for n in items])
        _apply_technical(news_only_facts, identifier)
        facts[identifier] = news_only_facts

    return order, facts
