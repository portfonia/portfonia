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

HOW that constraint is enforced (design doc §4.8 addendum, 2026-08-15) —
this is the part that took four review rounds to get right, so do not
"simplify" it back:

    per-user anomalies --l1_identifiers_for_user()--> list[str] --+
                                                                  |
    global HoldingMove (window_data.resolve_global_moves) --------+
                                                                  |
                                          build_l1_facts() -------+--> L1Facts

The pipeline used to be `global data -> per-user transform -> shared cache`:
`build_l1_candidates` read its numbers straight out of `ctx.price_anomalies`,
which `select_user_anomalies`/`_merge_theme_anomalies` had already
threshold-judged (per the calling user's `asset_class`) and value-weighted
(per that user's `Holding.current_value`). Every field of that structure is
therefore a potential per-user contaminant, and review rounds 2, 3 and 4
each found a different one that had reached the shared cache (`trigger`;
then the weighted `window_net_pct`/`current_price`/`prev_price`; then the
theme slug itself, via news routing). Auditing fields one at a time was
losing that race.

The fix is a type boundary, not a discipline: the per-user anomaly list may
contribute EXACTLY ONE thing — which identifiers are worth analyzing —
through `l1_identifiers_for_user`, whose return type is `list[str]`. Every
number then comes from `HoldingMove`, which is global by construction (see
its docstring in window_data.py: no threshold judgment, no per-holding
fields). A future edit that reintroduces the old shape is a type error at
the `build_l1_facts` call site rather than a fourth review finding.

The same rule generalizes to A3 (`macro_event_intel`) and A4: a consumer
writing into a cross-user shared cache may consume only globally-typed
artifacts. Selection (WHICH keys to analyze) may legitimately be per-user —
each user contributes their own candidates to a shared cache — but VALUES
must not be.

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

from app.compliance.output_scan import _scan_forbidden_output, _strip_markers
from app.core.config import get_settings
from app.models.ticker_intel import TickerIntel
from app.services._yfinance import _normalize_hk_ticker
from app.services.email_sender import send_ops_alert
from app.services.llm_errors import is_retryable
from app.services.report_llm import _call_llm, _openrouter_client
from app.services.report_prompts import _COMPLIANCE_SYSTEM_PREFIX
from app.services.window_data import HoldingMove

logger = logging.getLogger(__name__)

# Bumping this deliberately stops serving old cache entries under a new
# prompt contract (design doc §4.4) — it is part of the table's unique key,
# not just an audit column. v1 -> v2 (design doc §4.8, second addendum):
# the facts and prompt wording changed from a per-user window's cumulative
# change to a single trading day's change — a v1 row's `analysis` describes
# a different (and per-user-contaminated) quantity, must never be served
# under the new contract.
_PROMPT_VERSION = "l1-v2"

# Per-day cap on FRESH LLM analyses (design doc §4.3) — cache hits are free
# and never count against this, so a broad anomaly day degrades to "some
# identifiers have no L1 intel" rather than an unbounded cost spike. Counted
# in ATTEMPTS (`SUM(attempt_count)`), not rows: a key retried under #160 must
# not get its extra attempts for free, or this cap silently loosens by a
# factor of _MAX_ATTEMPTS_PER_KEY on exactly the day it matters most.
_MAX_L1_ANALYSES_PER_DAY = 15

# How many times the SYSTEM — not each user — may attempt one identifier in
# one trade_date before its marker row becomes final (issue #160). N=3
# (initial + 2 retries): anything that reaches this module's handler has
# already survived `_call_llm`'s own backoff sequence (up to 30s+90s for a
# connection fault), so the remaining value of a retry is covering the case
# where the blip cleared between two users of the same fan-out — a couple of
# extra chances, not a persistent hammer. A non-retryable failure or a
# compliance block writes this value directly and locks the key immediately.
_MAX_ATTEMPTS_PER_KEY = 3

_L1_SYSTEM = _COMPLIANCE_SYSTEM_PREFIX + (
    "\nYou are writing a short SHARED factual briefing about a single security "
    "or theme for an internal cache. This text will be reused verbatim across "
    "every user in the system who holds it — it must contain NOTHING specific "
    "to any one user: no position size, portfolio weight, account value, or "
    "how many people hold it. Describe ONLY what happened to this security on "
    "this trading day, using the facts provided.\n"
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

    Every field here is either a single-trading-day price-move fact (shared,
    identical across every user who holds the identifier — sourced from A1's
    `HoldingMove`, computed over `window_data.day_window_bounds(trade_date)`,
    never any user's own report window), a captured headline, or a
    descriptive OHLCV fact (`technical_position.py`). None of it is
    holdings-derived beyond the identifier string itself.

    `day_pct` (design doc §4.8, second addendum, 2026-08-15) replaced
    `net_pct`/`max_day_pct`: the first draft's window was a user's own
    `[period_start, period_end]`, which is per-user by construction
    (`period_start = user_watermark(user)`) — two users analyzing the same
    identifier on the same `trade_date` could get different windows, and
    whichever one's `generate_report` call reached L1 first would cache
    ITS window's numbers for everyone else that day. The fix is not a
    better window definition; it's dropping the window concept from L1
    entirely — L1 now describes exactly one trading day (`trade_date`),
    which has no ambiguity to leak. Under a strictly one-day window,
    "cumulative change" and "largest single-day move" are the same number,
    so `max_day_pct` is gone too, not just renamed. Multi-day continuity
    across a user's own report window is deliberately NOT synthesized here
    — see `report_generator.py`'s L1 wiring comment for where that job
    lives (plain day-by-day listing for now, per product decision).

    `trigger` (single_day vs cumulative) was a field here until an earlier
    pass of the same redesign and is deliberately gone too: it is derived
    from the CALLING USER's own `asset_class` threshold, so two holders of
    one identifier can classify the identical move differently — the same
    per-user-contamination class as the window, just a different field.
    """

    day_pct: float | None = None
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
            "day_pct": self.day_pct,
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
    lines = [f"Identifier: {identifier}", ""]
    if facts.day_pct is not None:
        lines.append(f"Price change vs. the prior trading day's close: {facts.day_pct:+.2%}")
    if facts.market:
        lines.append(f"Market: {facts.market}")
    if facts.current_price is not None and facts.prev_price is not None:
        lines.append(f"Price: {facts.prev_price} -> {facts.current_price}")
    if facts.latest_date:
        lines.append(f"Trading date: {facts.latest_date}")
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
        lines.append("Related headlines published this trading day:")
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
    (row present, analysis None) to avoid re-attempting the latter.

    `populate_existing=True` is load-bearing since #160, not tidiness: the
    whole fan-out shares ONE Session (`generate_incremental_report` opens it
    once and hands it to every user's `generate_report`), and `_write_cache`
    updates `attempt_count` through a Core upsert, which does NOT refresh an
    already-identity-mapped ORM instance. Without this, the third user in a
    fan-out would re-read the SECOND user's stale in-memory row, see one
    attempt fewer than really happened, and keep re-attempting past the cap —
    the exact unbounded-retry failure this issue's design set out to avoid.
    """
    return session.execute(
        select(TickerIntel)
        .where(
            TickerIntel.identifier == identifier,
            TickerIntel.trade_date == trade_date,
            TickerIntel.prompt_version == _PROMPT_VERSION,
        )
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()


def _attempts_today(session: Session, trade_date: date) -> int:
    """Total LLM attempts made today, NOT rows written — one retried key
    consumes as much of the daily budget as two distinct keys would."""
    return int(
        session.execute(
            select(func.coalesce(func.sum(TickerIntel.attempt_count), 0)).where(
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
    attempt_count: int,
) -> None:
    """`analysis=None` writes the "attempted, no usable result" marker row
    (see TickerIntel's docstring); `attempt_count` says whether that marker
    is final (`>= _MAX_ATTEMPTS_PER_KEY`) or still leaves a later caller a
    chance.

    Upsert rather than the pre-#160 `on_conflict_do_nothing`: a retry has to
    be able to raise an existing marker's `attempt_count`, and a retry that
    finally succeeds has to replace the marker with the real analysis. The
    `analysis IS NULL` guard keeps that from cutting the other way — once a
    real analysis is stored, no concurrent writer or redelivered attempt can
    overwrite it with a marker.
    """
    stmt = pg_insert(TickerIntel).values(
        identifier=identifier,
        trade_date=trade_date,
        prompt_version=_PROMPT_VERSION,
        model=model,
        analysis=analysis,
        facts=facts.to_jsonb(),
        attempt_count=attempt_count,
    )
    session.execute(
        stmt.on_conflict_do_update(
            constraint="uq_ticker_intel_identifier_date_version",
            set_={
                "model": stmt.excluded.model,
                "analysis": stmt.excluded.analysis,
                "facts": stmt.excluded.facts,
                "attempt_count": stmt.excluded.attempt_count,
            },
            where=TickerIntel.analysis.is_(None),
        )
    )
    session.flush()


def _generate(
    session: Session,
    identifier: str,
    trade_date: date,
    facts: L1Facts,
    usage_sink: list[dict[str, Any]] | None = None,
    attempts_so_far: int = 0,
) -> str | None:
    """Call the LLM, gate the output through the compliance scan, and ALWAYS
    write a row — either the real analysis, or (on an LLM failure or a
    compliance block) a null-analysis marker row, so a systematically
    failing/blocked identifier is attempted a bounded number of times per day
    rather than once per user in the fan-out (review round 1 fix: the first
    draft wrote nothing on failure, so the daily cap never saw these attempts
    and could be bypassed entirely). Returns None (never raises) on either
    failure mode — both degrade to "no L1 intel this run", not a report
    failure (design doc §4.3).

    How final that marker is depends on WHY the call failed (issue #160):

    - Retryable per `llm_errors.is_retryable` (connection, 429, provider 5xx,
      empty 200): record this attempt and leave the rest of the key's budget
      for a later caller. `_call_llm` has already burned its own backoff by
      the time it re-raises, so the case worth covering is a blip that
      cleared between two users of the same fan-out.
    - Not retryable (bad key, malformed request) or blocked by the compliance
      scan: write `_MAX_ATTEMPTS_PER_KEY` and lock the key on the spot. An
      identical call reproduces the first exactly, and a re-run of a blocked
      generation re-risks the same violation and re-alerts ops for nothing.

    `usage_sink` (round 2 review finding): forwarded straight to `_call_llm`,
    same as Pass 1/Pass 2 already do — without it, L1's per-identifier spend
    was invisible in `report_inputs.llm_calls`, so a report's total LLM cost
    audit silently excluded whatever L1 analysis it triggered.
    """
    settings = get_settings()
    model = settings.LOW_COST_LLM_MODEL
    prompt = _build_l1_prompt(identifier, facts)
    this_attempt = attempts_so_far + 1
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
    except Exception as exc:
        logger.exception(
            "ticker_intel: L1 analysis call failed for %s (attempt %d/%d)",
            identifier,
            this_attempt,
            _MAX_ATTEMPTS_PER_KEY,
        )
        recorded = this_attempt if is_retryable(exc) else _MAX_ATTEMPTS_PER_KEY
        _write_cache(session, identifier, trade_date, model, None, facts, recorded)
        return None

    # Strip stray citation/provenance/disclaimer noise before scanning, same
    # as Pass 2's `cleaned = _strip_markers(raw_body)` (report_generator.py) —
    # otherwise a model-emitted disclaimer line (legitimately containing
    # advisory-sounding wording) could false-trip the scan and permanently
    # blacklist this identifier for the day (round 7 review finding).
    cleaned = _strip_markers(analysis)

    violations = _scan_forbidden_output(cleaned)
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
        # Locked immediately, not retried: #160's retry budget is for
        # transport faults, and re-running a generation that already produced
        # forbidden vocabulary just re-risks it and re-alerts ops.
        _write_cache(session, identifier, trade_date, model, None, facts, _MAX_ATTEMPTS_PER_KEY)
        return None

    _write_cache(session, identifier, trade_date, model, cleaned, facts, this_attempt)
    return cleaned


def get_l1_intel_batch(
    session: Session,
    identifiers: list[str],
    trade_date: date,
    facts_by_identifier: dict[str, L1Facts],
    usage_sink: list[dict[str, Any]] | None = None,
) -> dict[str, str]:
    """Read-through cache over `identifiers` (expected ordered by |move|
    descending — see `l1_identifiers_for_user`). A cache HIT holding a real
    analysis never re-calls the LLM. A HIT holding a null "attempted, no
    result" marker (see TickerIntel's docstring) re-calls only while the key
    still has attempts left under `_MAX_ATTEMPTS_PER_KEY` — see `_generate`
    for which failures leave any (issue #160). A cache MISS beyond the day's
    remaining fresh-analysis budget is simply skipped (that identifier has no
    L1 intel this run — degrade, don't fail).

    `fresh_budget` is decremented on EVERY fresh attempt via `_generate`,
    regardless of outcome (review round 1 fix): counting only successful
    writes let a systematically-blocked or -failing identifier bypass the
    daily cap entirely, since every user in the fan-out would see "not yet
    attempted" and retry it.

    `usage_sink` (round 2 review finding): forwarded to every fresh
    `_generate` call — a cache HIT makes no LLM call, so nothing to record.
    Pass `ctx.llm_calls` from `generate_report` so a day's L1 spend shows up
    in the SAME report_inputs.llm_calls list Pass 1/Pass 2 already use.

    A candidate with `day_pct is None` (headline-only — `build_l1_facts`'s
    own degrade path for an identifier with no captured close yet for
    `trade_date`, e.g. a manual run before today's market close) is
    skipped WITHOUT calling `_generate` at all (round 6 review finding,
    design doc §4.8): caching such an analysis under the day's unique key
    would make it FINAL for the day — the real after_close batch, running
    later with a genuine `day_pct` available, would hit the cache and
    never re-analyze, so every user that day would be permanently served
    the numberless, pre-close briefing. Skipping BEFORE `_generate` (rather
    than having `_generate` itself decline to write) means no cache row of
    any kind is left behind — not even a null "attempted" marker, which
    would ALSO permanently block a later, better-informed retry the same
    day. This skip does not consume `fresh_budget` either: it never
    attempted anything, so it must not count against the identifiers that
    genuinely did.

    Sequential fan-out (`generate_incremental_report` processes users one at
    a time, not concurrently) is what actually makes "shared across users"
    hold here: the first user's call caches to the DB before the second
    user's call ever runs, so no in-memory batch cache (unlike A1's
    moves_cache) is needed for the sharing property itself — see design doc
    §4.1 UAT-4 and test_ticker_intel.py's cache-hit tests.
    """
    result: dict[str, str] = {}
    fresh_budget = max(0, _MAX_L1_ANALYSES_PER_DAY - _attempts_today(session, trade_date))
    for identifier in identifiers:
        cached = _fetch_cached(session, identifier, trade_date)
        attempts_so_far = 0
        if cached is not None:
            if cached.analysis is not None:
                result[identifier] = cached.analysis
                continue
            attempts_so_far = cached.attempt_count
            if attempts_so_far >= _MAX_ATTEMPTS_PER_KEY:
                continue
        facts = facts_by_identifier.get(identifier)
        if facts is None or facts.day_pct is None:
            continue
        if fresh_budget <= 0:
            logger.info(
                "ticker_intel: daily L1 analysis cap (%d) reached, skipping %s",
                _MAX_L1_ANALYSES_PER_DAY,
                identifier,
            )
            continue
        analysis = _generate(session, identifier, trade_date, facts, usage_sink, attempts_so_far)
        fresh_budget -= 1
        if analysis is not None:
            result[identifier] = analysis
    return result


def l1_identifiers_for_user(anomalies: list[dict[str, Any]]) -> list[str]:
    """The per-user -> global firewall: WHICH identifiers this user's report
    makes worth analyzing, as plain strings and nothing else.

    `anomalies` is `ctx.price_anomalies` — per-user through and through
    (threshold-judged against this user's `asset_class`, theme entries
    value-weighted by this user's `Holding.current_value`). Returning
    `list[str]` is the whole point: no field of that structure can travel
    into the shared cache, because nothing but the identifier survives the
    call. See this module's docstring for why a field-by-field audit was
    the wrong tool.

    Theme-merged entries (a non-empty `constituents` list — the merge
    `window_data._merge_theme_anomalies` performs) expand to their real
    constituent identifiers; the theme slug itself is NEVER a candidate.
    A theme slug is a per-user merge artifact, not a security: it has no
    row in `price_snapshots`, so no global move, and caching under it
    would key shared intel by something A4 can never look up. Theme-level
    narrative is A3/L2's job (design doc §5), decided 2026-08-15.

    ORDERING is deliberately left per-user: the caller's list is already
    sorted by that user's |move|, and constituents keep their given order.
    Ordering only decides who survives the daily fresh-analysis cap — a
    SELECTION concern, which is per-user by design (every user contributes
    candidates to one shared cache). It is values, not selection, that must
    be global.
    """
    out: list[str] = []
    seen: set[str] = set()
    for a in anomalies:
        identifier = a.get("identifier")
        if not identifier:
            continue
        constituents = a.get("constituents") or []
        # An empty `constituents` means "not a theme merge": _serialize_anomalies
        # renders `a.constituents or []`, so a genuine single-ticker anomaly
        # serializes the same way, and _merge_theme_anomalies never produces a
        # theme entry with zero members (a bucket exists only if it has >= 1).
        for candidate in (
            [c.get("identifier") for c in constituents] if constituents else [identifier]
        ):
            if candidate and candidate not in seen:
                seen.add(candidate)
                out.append(candidate)
    return out


def build_l1_facts(
    identifiers: list[str],
    day_moves: dict[str, HoldingMove],
    headlines: dict[str, list[str]],
    technical_positions: list[dict[str, Any]],
) -> dict[str, L1Facts]:
    """Assemble each candidate's public facts from GLOBAL, DAY-SCOPED
    sources only.

    `day_moves` MUST come from `window_data.resolve_global_moves` called
    with `window_data.day_window_bounds(trade_date)` — never a user's own
    `[period_start, period_end]` (design doc §4.8, second addendum). Under
    day bounds, `HoldingMove.net_pct` (baseline = the close immediately
    before `trade_date`, latest = `trade_date`'s own close) already IS the
    single trading day's move; a window-scoped call would instead give a
    per-user-contaminated cumulative figure (the round-5 review bug this
    redesign closes) — the parameter is named `day_moves`, not `moves`, so
    that mistake is visible at the call site, not just in a docstring.
    Taking `dict[str, HoldingMove]` rather than the serialized per-user
    anomaly dicts is what makes "a per-user number reached the shared
    cache" a type error instead of a review finding in the first place.

    An identifier with no `day_moves` entry (no close captured yet for
    `trade_date`, or no prior close to baseline against) gets a
    headline-only briefing rather than inheriting numbers from anywhere
    else — degrade, never fabricate. One with neither a move nor a headline
    is dropped entirely: there is nothing to brief on, and a candidate with
    empty facts would still consume one of the day's fresh-analysis slots.

    `technical_positions[].ticker` is normalized to match `identifiers`'
    casing before use as a lookup key: it comes straight from
    `HoldingValue.ticker` (`portfolio_calculator.py`), the RAW
    `Holding.ticker` value, whereas `identifiers` (and `day_moves`' keys)
    are always `_normalize_hk_ticker(...).upper()`'d — `select_user_anomalies`
    and `compute_global_moves` apply that normalization before either one
    ever produces a key. A holding whose `ticker` reached the DB without
    going through `holding_parser._postprocess` (e.g. `POST
    /holdings/confirm` posted directly — see CLAUDE.md's cash/wmf section)
    can carry an un-normalized raw suffix like "700.HK", which would
    otherwise never match the "0700.HK" key everything else in this
    pipeline uses, silently dropping technical facts for exactly the
    holdings most likely to need the normalization in the first place.
    """
    technical_by_ticker = {
        _normalize_hk_ticker(t["ticker"]).upper(): t for t in technical_positions if t.get("ticker")
    }
    facts: dict[str, L1Facts] = {}

    for identifier in identifiers:
        move = day_moves.get(identifier)
        titles = headlines.get(identifier, [])
        if move is None and not titles:
            continue
        entry = L1Facts(news_headlines=list(titles))
        if move is not None:
            entry.day_pct = float(move.net_pct)
            entry.market = move.market
            entry.current_price = float(move.current_price)
            entry.prev_price = float(move.prev_price)
            entry.latest_date = move.latest_date.isoformat()
        tech = technical_by_ticker.get(identifier, {})
        entry.pct_vs_sma50 = tech.get("pct_vs_sma50")
        entry.pct_vs_sma200 = tech.get("pct_vs_sma200")
        entry.pct_in_52w_range = tech.get("pct_in_52w_range")
        entry.vol_20d_annualized = tech.get("vol_20d_annualized")
        facts[identifier] = entry

    return facts
