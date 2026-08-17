"""L2 shared macro-event intel cache (issue #128, Ring 1 A3 — design doc
§5, Hermes/Portfonia/Docs/Ring 1-A design.md).

What this adds: one LLM inference per (event_key, trade_date,
prompt_version) answering "what is this macro event, and which asset
classes / sectors does it bear on", cached and reused across every user
whose report touches that event that day. The per-user half is then pure
set arithmetic — `user_event_exposure` intersects the cached
`affected_asset_classes` with the user's own `portfolio.by_asset_class`
keys and makes no LLM call at all.

Two event vocabularies share one table behind a prefixed key:

    theme:<name>   a `macro_detector` ThemeHit (keyword table:
                   config/macro_keywords.yml)
    fwd:<uuid>     a `forward_events` row (already uniquely keyed by
                   `uq_forward_events_key`, so its id is a stable event id)

`forward_events`' existing shape — global read (`load_forward_events`) then
per-user mapping (`report_sections._forward_exposure`) — is the pattern
design doc §5.3 points at, and it holds up on re-inspection: the table is
read with no `user_id` filter anywhere, and nothing from the per-user
mapping flows back. A3 generalizes it to macro themes and inserts the
shared inference in between.

WHY THE SIGNATURES LOOK THE WAY THEY DO (A2's four-review-round lesson,
design doc §4.8 — do not "simplify" this back):

    per-user macro_signals --l2_event_keys_for_user()--> list[str] --+
                                                                     |
    global day news / forward rows --build_l2_facts(session, ---------+--> L2Facts
                                      event_keys, trade_date)

A2 shipped `global data -> per-user transform -> shared cache` and paid for
it three review rounds running: the per-user structure it fed the cache
(`_merge_theme_anomalies`' output) carried a threshold flag, then
value-weighted prices, then a theme-slug news key, each found separately.
The rule that came out of it: **a consumer writing into a cross-user shared
cache may consume only globally-typed artifacts — SELECTION may be
per-user, VALUES must not be.**

L2 obeys it more strictly than L1 could:

- `l2_event_keys_for_user` is the ONLY channel from per-user state
  (`ctx.macro_signals`, whose themes AND backing articles come from
  `load_news_window(..., user_id)`) into this module, and its return type is
  `list[str]`.
- `build_l2_facts(session, event_keys, trade_date)` takes a Session, plain
  strings and a date — there is no parameter through which a caller COULD
  pass a watermark, an anomaly list, a portfolio or a news window. It
  re-derives every fact itself from `load_day_news` and the
  `forward_events` table.

And the second principle (design doc §4.8, third addendum): the window a
shared row describes is a pure function of `trade_date`.
`load_day_news(session, trade_date)` (no `user_id`, no `news_surfaced`
ledger) is the theme evidence; a forward event's facts are the immutable
calendar row. Nothing here reads `period_start`/`period_end`, so the
round-5 failure mode — user A's watermark deciding what user B reads — has
no surface to recur on.

Compliance: a cached entry ships to every user touching that event, so a
forbidden-vocabulary slip has an N-user blast radius. The output is
`_strip_markers`'d first (same as Pass 2, and as A2's round-7 fix) and then
scanned; a violation is never cached, ops is alerted, and the caller simply
gets no L2 intel for that event. `data_collection=deny` stays enforced —
forward-event keys are derived from the holdings universe, so the BYOK
exception scoped to Pass 1/translation must not be reused here.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.compliance.output_scan import _scan_forbidden_output, _strip_markers
from app.core.config import get_settings
from app.models.macro_event_intel import MacroEventIntel
from app.services.asset_class_config import VALID_ASSET_CLASSES
from app.services.email_sender import send_ops_alert
from app.services.forward_events import FORWARD_WINDOW_DAYS, load_forward_events
from app.services.llm_errors import is_retryable
from app.services.macro_detector import detect_macro_signals
from app.services.report_llm import _call_llm, _openrouter_client
from app.services.report_prompts import _COMPLIANCE_SYSTEM_PREFIX
from app.services.sector_taxonomy import OTHER, VALID_SECTORS
from app.services.window_data import load_day_news

logger = logging.getLogger(__name__)

# Part of the unique key, not an audit column (same contract as
# ticker_intel._PROMPT_VERSION): bumping it retires every existing row rather
# than serving an older classification under a new prompt.
_PROMPT_VERSION = "l2-v1"

_THEME_PREFIX = "theme:"
_FORWARD_PREFIX = "fwd:"

# Per-day caps on FRESH inferences, counted SEPARATELY PER EVENT KIND. Cache
# hits are free and never count against them, so a busy day degrades to "some
# events have no L2 intel" instead of an unbounded cost spike.
#
# Two budgets rather than one (round-1 review finding, blacktomb42 on PR
# #157): a single shared cap is consumed in whichever order the day's first
# non-quiet user happens to present its candidates, and the two kinds are not
# symmetric — every user sees the same `fwd:` calendar (global), while
# `theme:` keys are per-user. So a first user who hit no themes could spend
# the entire day's budget on calendar events and leave every later user's
# themes unanalyzed until tomorrow. Deterministic global ORDERING (see
# `l2_event_keys_for_user`) does not fix that on its own: it makes one user's
# list stable, not the union across users whose lists differ.
#
# Theme count is bounded by the keyword table (8 entries today), so the theme
# budget has headroom for a couple of additions and effectively never binds.
# The forward budget is the one that genuinely can (earnings season) — that
# is a cost ceiling, not a fairness defect: it truncates the same global list
# for everyone.
#
# Both budgets are counted in ATTEMPTS (`SUM(attempt_count)`), not rows, so a
# key retried under #160 spends as much of its kind's budget as two distinct
# keys would — otherwise the ceiling silently loosens by a factor of
# `_MAX_ATTEMPTS_PER_KEY` on exactly the day that ceiling matters most.
_MAX_L2_THEME_ANALYSES_PER_DAY = 10
_MAX_L2_FORWARD_ANALYSES_PER_DAY = 15

# Attempts the SYSTEM (not each user) may spend on one event_key in one
# trade_date before its marker row is final — same contract, same value, and
# for the same reason as `ticker_intel._MAX_ATTEMPTS_PER_KEY`: whatever
# reaches this handler already survived `_call_llm`'s own backoff, so the
# retry only exists to cover a blip that cleared between two users of one
# fan-out. Keep the two in step; they are one mechanism applied twice.
_MAX_ATTEMPTS_PER_KEY = 3

# `OTHER` is the bucket an UNCLASSIFIABLE holding falls into (see
# sector_taxonomy.map_yf_sector), not a sector an event can meaningfully bear
# on. Accepting it from the model would sweep every holding with an unknown
# sector into that event's exposure.
_ASSIGNABLE_SECTORS: frozenset[str] = VALID_SECTORS - {OTHER}

_L2_SYSTEM = _COMPLIANCE_SYSTEM_PREFIX + (
    "\nYou are classifying ONE macro event for an internal SHARED cache. Your "
    "output is reused verbatim for every user in the system whose portfolio "
    "touches this event — it must contain NOTHING specific to any one user: no "
    "position size, portfolio weight, account value, holding name, or how many "
    "people are exposed to it.\n"
    "Reply with a JSON object and nothing else, with exactly these keys:\n"
    '  "analysis": 2-4 sentences describing what the event IS and which broad '
    "exposures it bears on, grounded ONLY in the facts given. End any causal "
    "attribution with [Established], [Probable] or [Speculative]. For a "
    "scheduled future event, describe it as a calendar fact and what it will "
    "measure — never forecast its outcome or market direction. No headings, no "
    "citations, no disclaimer.\n"
    '  "affected_asset_classes": a list drawn ONLY from this closed set, '
    "possibly empty: {asset_classes}\n"
    '  "affected_sectors": a list drawn ONLY from this closed set, possibly '
    "empty: {sectors}\n"
    "Never invent a category outside those sets; if none fits, return an empty "
    "list."
).format(
    asset_classes=", ".join(sorted(VALID_ASSET_CLASSES)),
    sectors=", ".join(sorted(_ASSIGNABLE_SECTORS)),
)


@dataclass
class L2Facts:
    """Public, non-per-user facts about one macro event.

    Every field is either a global keyword-detection result over ONE
    trading day's news (`load_day_news`, no `user_id`, no `news_surfaced`
    ledger) or an immutable published calendar fact. Nothing here is
    holdings-derived beyond an earnings event's own ticker, which is a
    property of the calendar row itself.

    There is deliberately no field for "how many users hold this", "which
    holdings are exposed" or "this user's window" — the per-user half of L2
    lives entirely in `user_event_exposure`, downstream of the cache.
    """

    event_kind: str  # "macro_theme" | "forward_event"
    label: str  # theme name, or event name
    keywords_found: list[str] = field(default_factory=list)  # macro_theme only
    news_headlines: list[str] = field(default_factory=list)  # macro_theme only
    event_type: str = ""  # forward_event only: "macro" | "earnings"
    ticker: str = ""  # forward_event only
    scheduled_date: str = ""  # forward_event only

    def to_jsonb(self) -> dict[str, Any]:
        return {
            "event_kind": self.event_kind,
            "label": self.label,
            "keywords_found": self.keywords_found,
            "news_headlines": self.news_headlines,
            "event_type": self.event_type,
            "ticker": self.ticker,
            "scheduled_date": self.scheduled_date,
        }


def _build_l2_prompt(event_key: str, facts: L2Facts) -> str:
    lines: list[str] = []
    if facts.event_kind == "forward_event":
        lines.append(f"Scheduled calendar event: {facts.label}")
        if facts.event_type:
            lines.append(f"Event type: {facts.event_type}")
        if facts.ticker:
            lines.append(f"Company ticker: {facts.ticker}")
        if facts.scheduled_date:
            lines.append(f"Scheduled date: {facts.scheduled_date}")
    else:
        lines.append(f"Macro theme triggered by this trading day's news: {facts.label}")
        if facts.keywords_found:
            lines.append(f"Matched keywords: {', '.join(facts.keywords_found)}")
        if facts.news_headlines:
            lines.append("")
            lines.append("Headlines published this trading day:")
            lines.extend(f"- {h}" for h in facts.news_headlines)
    lines.append("")
    lines.append("Return the JSON object described in your system instructions.")
    return "\n".join(lines)


def _filter_to_taxonomy(
    values: object, allowed: frozenset[str], event_key: str, kind: str
) -> list[str]:
    """Keep only labels inside the closed taxonomy, preserving order and
    dropping duplicates.

    Design doc §5.3 (and Concept & Design §7.1.2's second gate): the model
    picks from a predefined enum, it does not generate one. An invented
    synonym would not error anywhere downstream — it would just intersect
    with nothing, turning a real exposure into a silent miss — so the value
    is dropped HERE, before it is ever written, and the drop is logged.
    """
    if not isinstance(values, list):
        logger.warning(
            "macro_event_intel: %s returned a non-list %s field (%r) — treating as empty",
            event_key,
            kind,
            type(values).__name__,
        )
        return []
    kept: list[str] = []
    dropped: list[str] = []
    for raw in values:
        value = raw.strip() if isinstance(raw, str) else ""
        if value in allowed:
            if value not in kept:
                kept.append(value)
        else:
            dropped.append(str(raw))
    if dropped:
        logger.warning(
            "macro_event_intel: %s proposed out-of-taxonomy %s value(s) %s — dropped",
            event_key,
            kind,
            dropped,
        )
    return kept


def _loads_or_none(text: str) -> dict[str, Any] | None:
    """Parse `text` as a JSON object, or None. Anything that is valid JSON but
    not an object (a bare list, string or number) is treated as a miss too —
    the caller only ever wants the object form, and folding the check in here
    keeps its second-chance retry from having to re-check the shape."""
    try:
        parsed = json.loads(text)
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def _parse_l2_response(event_key: str, raw: str) -> tuple[str, list[str], list[str]] | None:
    """Parse + taxonomy-validate the model's JSON. None means "unusable".

    A model that answers in prose instead of JSON, or omits the analysis,
    has produced nothing servable; the caller turns that into the same
    null-analysis marker row an outright API failure gets, so it is not
    re-attempted once per user for the rest of the day.
    """
    text = raw.strip()
    if text.startswith("```"):
        text = "\n".join(ln for ln in text.splitlines() if not ln.strip().startswith("```")).strip()
    parsed = _loads_or_none(text)
    if parsed is None:
        # Second chance on the outermost {...} span before giving up (round-1
        # review finding, blacktomb42 on PR #157): a model that prefaces its
        # JSON with a sentence is a formatting habit, but the failure it used
        # to cause is a null marker row — FINAL for that event, for every
        # user, until tomorrow. Too expensive an outcome to hang on wording
        # the prompt can ask for but not guarantee.
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            parsed = _loads_or_none(text[start : end + 1])
    if parsed is None:
        logger.warning("macro_event_intel: %s returned no usable JSON object", event_key)
        return None
    analysis = parsed.get("analysis")
    if not isinstance(analysis, str) or not analysis.strip():
        logger.warning("macro_event_intel: %s returned no analysis text", event_key)
        return None
    classes = _filter_to_taxonomy(
        parsed.get("affected_asset_classes", []), VALID_ASSET_CLASSES, event_key, "asset_class"
    )
    sectors = _filter_to_taxonomy(
        parsed.get("affected_sectors", []), _ASSIGNABLE_SECTORS, event_key, "sector"
    )
    return analysis.strip(), classes, sectors


# ---------------------------------------------------------------------------
# Selection (per-user in, strings out) and facts (global in, global out)
# ---------------------------------------------------------------------------


def l2_event_keys_for_user(
    session: Session, trade_date: date, macro_signals: dict[str, Any]
) -> list[str]:
    """The per-user -> global firewall: WHICH events this user's report makes
    worth analyzing, as plain strings and nothing else.

    `macro_signals` is `ctx.macro_signals` — per-user through and through:
    `detect_macro_signals` ran over `load_news_window(..., user_id)`, so both
    the theme set and each theme's `top_articles` depend on this user's own
    watermark and `news_surfaced` ledger. Only the theme NAME survives this
    call; the evidence backing the shared row is re-derived globally in
    `build_l2_facts`.

    Forward-calendar keys are added from the global `forward_events` table
    for the same day-scoped horizon `report_generator` renders in §2.5 — a
    scheduled event is equally real for every user, so there is nothing
    per-user to filter it by at this stage (who is EXPOSED to it is
    `user_event_exposure`'s job, after the shared inference).

    ORDERING is deliberately global and deterministic — sorted theme keys,
    then forward events by (scheduled_date, name, id) — unlike
    `l1_identifiers_for_user`, which keeps the caller's own |move| order.
    The daily cap is consumed in list order, and L2's candidates are
    near-identical across users, so ordering by whichever user the fan-out
    reached first would systematically starve the users behind them: the
    same shape as the Tavily-budget fairness problem A1 handed forward. L1
    keeps per-user ordering because its candidates genuinely differ per user
    (each user's own holdings); L2's do not.
    """
    hits = macro_signals.get("hits", []) if isinstance(macro_signals, dict) else []
    theme_keys = sorted(
        {
            f"{_THEME_PREFIX}{h['theme']}"
            for h in hits
            if isinstance(h, dict) and isinstance(h.get("theme"), str) and h["theme"]
        }
    )
    forward_keys = [
        f"{_FORWARD_PREFIX}{row['id']}" for row in _load_forward_rows(session, trade_date)
    ]
    return theme_keys + forward_keys


def _load_forward_rows(session: Session, trade_date: date) -> list[dict[str, str]]:
    """Forward-calendar rows for the day-scoped horizon, soonest first.

    A pure function of `trade_date` (plus the global table): no `user_id`
    anywhere in `load_forward_events`, verified rather than assumed — see
    this module's docstring on why §5.3's "already the shape L2 wants"
    claim was re-checked instead of inherited.
    """
    rows = load_forward_events(
        session, trade_date, trade_date + timedelta(days=FORWARD_WINDOW_DAYS)
    )
    return sorted(rows, key=lambda r: (r["scheduled_date"], r["name"], r["id"]))


def build_l2_facts(session: Session, event_keys: list[str], trade_date: date) -> dict[str, L2Facts]:
    """Assemble each candidate's facts from GLOBAL, DAY-SCOPED sources only.

    The signature is the guarantee: a Session, plain strings, and a date.
    There is no parameter through which a per-user artifact could arrive, so
    the class of bug that took A2 three review rounds to chase down
    (per-user numbers reaching a shared cache) is a compile-time
    impossibility here rather than something a reviewer has to notice.

    Theme evidence comes from re-running the keyword detector over
    `load_day_news(session, trade_date)` — the same global, ledger-free
    source L1 uses. A theme the CALLER hit but the day's global news does
    not support gets NO entry at all: caching a factless briefing would
    burn the day's single cache slot for that key and lock every later,
    better-informed run out of it (A2's round-6 lesson, generalized).

    Forward events need no such guard — the calendar row itself is the
    fact, so a `fwd:` key that resolves to a row is always complete.
    """
    keys = set(event_keys)
    facts: dict[str, L2Facts] = {}

    theme_keys = {k for k in keys if k.startswith(_THEME_PREFIX)}
    if theme_keys:
        day_hits = {
            hit.theme: hit for hit in detect_macro_signals(load_day_news(session, trade_date)).hits
        }
        for key in theme_keys:
            hit = day_hits.get(key[len(_THEME_PREFIX) :])
            if hit is None:
                logger.info(
                    "macro_event_intel: %s has no global coverage on %s — no shared entry",
                    key,
                    trade_date,
                )
                continue
            facts[key] = L2Facts(
                event_kind="macro_theme",
                label=hit.theme,
                keywords_found=list(hit.keywords_found),
                news_headlines=[a.title for a in hit.articles],
            )

    forward_keys = {k for k in keys if k.startswith(_FORWARD_PREFIX)}
    if forward_keys:
        for row in _load_forward_rows(session, trade_date):
            key = f"{_FORWARD_PREFIX}{row['id']}"
            if key not in forward_keys:
                continue
            facts[key] = L2Facts(
                event_kind="forward_event",
                label=row["name"],
                event_type=row["event_type"],
                ticker=row["ticker"],
                scheduled_date=row["scheduled_date"],
            )

    return facts


# ---------------------------------------------------------------------------
# Read-through cache
# ---------------------------------------------------------------------------


def _fetch_cached(session: Session, event_key: str, trade_date: date) -> MacroEventIntel | None:
    """Returns the whole row: a row with `analysis IS NULL` is an "attempted,
    nothing servable" marker, and the caller must distinguish it from "never
    attempted today" (no row) so a failing event is not retried by every
    user in the fan-out.

    `populate_existing=True` for the reason spelled out in
    `ticker_intel._fetch_cached`: the whole fan-out shares one Session, and
    `_write_cache`'s Core upsert does not refresh an already-identity-mapped
    instance — a stale `attempt_count` would let attempts run past the cap.
    """
    return session.execute(
        select(MacroEventIntel)
        .where(
            MacroEventIntel.event_key == event_key,
            MacroEventIntel.trade_date == trade_date,
            MacroEventIntel.prompt_version == _PROMPT_VERSION,
        )
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()


def _attempts_today(session: Session, trade_date: date, prefix: str) -> int:
    """LLM attempts already made today for ONE event kind (see the budget
    constants for why the two kinds are counted apart) — attempts, not rows,
    so a retried key does not get its extra attempts for free."""
    return int(
        session.execute(
            select(func.coalesce(func.sum(MacroEventIntel.attempt_count), 0)).where(
                MacroEventIntel.trade_date == trade_date,
                MacroEventIntel.prompt_version == _PROMPT_VERSION,
                MacroEventIntel.event_key.like(f"{prefix}%"),
            )
        ).scalar_one()
    )


def _write_cache(
    session: Session,
    event_key: str,
    trade_date: date,
    model: str,
    analysis: str | None,
    classes: list[str],
    sectors: list[str],
    facts: L2Facts,
    attempt_count: int,
) -> None:
    """Upsert rather than the pre-#160 `on_conflict_do_nothing`: a retry must
    be able to raise an existing marker's `attempt_count`, and a retry that
    succeeds must replace the marker with the real inference. The
    `analysis IS NULL` guard keeps that one-directional — a stored inference
    is never overwritten by a later marker."""
    stmt = pg_insert(MacroEventIntel).values(
        event_key=event_key,
        trade_date=trade_date,
        prompt_version=_PROMPT_VERSION,
        model=model,
        analysis=analysis,
        affected_asset_classes=classes,
        affected_sectors=sectors,
        facts=facts.to_jsonb(),
        attempt_count=attempt_count,
    )
    session.execute(
        stmt.on_conflict_do_update(
            constraint="uq_macro_event_intel_key_date_version",
            set_={
                "model": stmt.excluded.model,
                "analysis": stmt.excluded.analysis,
                "affected_asset_classes": stmt.excluded.affected_asset_classes,
                "affected_sectors": stmt.excluded.affected_sectors,
                "facts": stmt.excluded.facts,
                "attempt_count": stmt.excluded.attempt_count,
            },
            where=MacroEventIntel.analysis.is_(None),
        )
    )
    session.flush()


def _generate(
    session: Session,
    event_key: str,
    trade_date: date,
    facts: L2Facts,
    usage_sink: list[dict[str, Any]] | None = None,
    attempts_so_far: int = 0,
) -> dict[str, Any] | None:
    """Infer, validate, compliance-gate, and ALWAYS write a row — the real
    result, or a null-analysis marker on any failure mode (API error,
    unparseable output, compliance block). Returns None on all of those, and
    never raises: no L2 intel for one event degrades that event, it does not
    fail a report.

    The marker row is what makes the daily cap honest — counting only
    successful writes would let a systematically failing event be retried by
    every user in the fan-out (A2 review round 1's finding, inherited here
    rather than rediscovered).

    How final the marker is depends on WHY it failed (issue #160, mirroring
    `ticker_intel._generate`):

    - Retryable per `llm_errors.is_retryable`, or unusable JSON: record the
      attempt and leave the rest of the key's budget to a later caller.
      Unparseable output belongs on this side because the taxonomy classifies
      INVALID_JSON retryable — the model is non-deterministic even at
      temperature 0, and `_parse_l2_response` has already spent its
      free (no-new-call) second chance on the same text before giving up.
    - Not retryable, or blocked by the compliance scan: write
      `_MAX_ATTEMPTS_PER_KEY` and lock the key immediately.
    """
    settings = get_settings()
    model = settings.LOW_COST_LLM_MODEL
    prompt = _build_l2_prompt(event_key, facts)
    this_attempt = attempts_so_far + 1
    try:
        client = _openrouter_client()
        raw = _call_llm(
            client,
            model,
            _L2_SYSTEM,
            prompt,
            with_holdings=False,
            disable_reasoning=True,
            usage_sink=usage_sink,
        )
    except Exception as exc:
        logger.exception(
            "macro_event_intel: L2 inference call failed for %s (attempt %d/%d)",
            event_key,
            this_attempt,
            _MAX_ATTEMPTS_PER_KEY,
        )
        recorded = this_attempt if is_retryable(exc) else _MAX_ATTEMPTS_PER_KEY
        _write_cache(session, event_key, trade_date, model, None, [], [], facts, recorded)
        return None

    parsed = _parse_l2_response(event_key, raw)
    if parsed is None:
        _write_cache(session, event_key, trade_date, model, None, [], [], facts, this_attempt)
        return None
    analysis, classes, sectors = parsed

    # Strip stray citation/provenance/disclaimer noise BEFORE scanning, same
    # as Pass 2's `cleaned = _strip_markers(raw_body)`: a disclaimer line the
    # model added against instructions legitimately contains advisory-sounding
    # wording, and letting it reach the scan would blacklist this event's only
    # cache slot for the whole day (A2 round-7 finding).
    cleaned = _strip_markers(analysis)

    violations = _scan_forbidden_output(cleaned)
    if violations:
        logger.error(
            "macro_event_intel: L2 output for %s (%s) BLOCKED by compliance scan: %s",
            event_key,
            trade_date,
            violations,
        )
        send_ops_alert(
            subject=f"[Portfonia] L2 shared macro intel BLOCKED for compliance — {event_key}",
            body=(
                f"L2 shared-cache inference for event {event_key} on {trade_date} tripped "
                f"the forbidden-vocabulary scan: {violations}\n\n"
                f"The forbidden text was NOT stored or served — a null-analysis marker row "
                f"was written for (event_key={event_key}, trade_date={trade_date}, "
                f"prompt_version={_PROMPT_VERSION}) instead, so this event will not be "
                f"re-attempted today (a manual retry would just hit the same unique-key "
                f"row). Report generation for the affected user(s) continues without L2 "
                f"intel for this event."
            ),
            idempotency_key=f"ops-l2-blocked-{event_key}-{trade_date}",
        )
        # Locked on the spot, not retried — see `_generate`'s docstring.
        _write_cache(
            session, event_key, trade_date, model, None, [], [], facts, _MAX_ATTEMPTS_PER_KEY
        )
        return None

    _write_cache(
        session, event_key, trade_date, model, cleaned, classes, sectors, facts, this_attempt
    )
    return {
        "analysis": cleaned,
        "affected_asset_classes": classes,
        "affected_sectors": sectors,
    }


def get_l2_intel_batch(
    session: Session,
    event_keys: list[str],
    trade_date: date,
    facts_by_key: dict[str, L2Facts],
    usage_sink: list[dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Read-through cache over `event_keys`, in the caller's (globally
    ordered — see `l2_event_keys_for_user`) sequence.

    A cache HIT holding a real inference never re-calls the LLM; a HIT
    holding a null marker re-calls only while the key has attempts left under
    `_MAX_ATTEMPTS_PER_KEY` (issue #160 — see `_generate` for which failures
    leave any). A MISS with no facts is skipped WITHOUT calling the model
    and WITHOUT writing any row — not even an "attempted" marker, which
    would itself lock the day's single slot for that key against a later run
    that does have coverage. A MISS beyond the day's remaining fresh budget
    is likewise skipped (that event has no L2 intel this run).

    `usage_sink` is forwarded to every fresh inference so L2's spend lands in
    the same `report_inputs.llm_calls` list Pass 1/Pass 2/L1 already use;
    cache hits make no call and record nothing.

    Sharing across users relies on the fan-out being sequential
    (`generate_incremental_report` processes users one at a time), exactly as
    L1's does: the first user's inference is committed to the DB before the
    next user's report reaches this function.
    """
    result: dict[str, dict[str, Any]] = {}
    fresh_budget = {
        _THEME_PREFIX: max(
            0,
            _MAX_L2_THEME_ANALYSES_PER_DAY - _attempts_today(session, trade_date, _THEME_PREFIX),
        ),
        _FORWARD_PREFIX: max(
            0,
            _MAX_L2_FORWARD_ANALYSES_PER_DAY
            - _attempts_today(session, trade_date, _FORWARD_PREFIX),
        ),
    }
    for event_key in event_keys:
        cached = _fetch_cached(session, event_key, trade_date)
        attempts_so_far = 0
        if cached is not None:
            if cached.analysis is not None:
                result[event_key] = {
                    "analysis": cached.analysis,
                    "affected_asset_classes": list(cached.affected_asset_classes),
                    "affected_sectors": list(cached.affected_sectors),
                }
                continue
            attempts_so_far = cached.attempt_count
            if attempts_so_far >= _MAX_ATTEMPTS_PER_KEY:
                continue
        facts = facts_by_key.get(event_key)
        if facts is None:
            continue
        kind = _THEME_PREFIX if event_key.startswith(_THEME_PREFIX) else _FORWARD_PREFIX
        if fresh_budget[kind] <= 0:
            logger.info(
                "macro_event_intel: daily L2 inference cap for '%s' events reached, skipping %s",
                kind,
                event_key,
            )
            continue
        intel = _generate(session, event_key, trade_date, facts, usage_sink, attempts_so_far)
        fresh_budget[kind] -= 1
        if intel is not None:
            result[event_key] = intel
    return result


# ---------------------------------------------------------------------------
# Per-user mapping (design doc §5.3): pure set arithmetic, zero LLM
# ---------------------------------------------------------------------------


def user_event_exposure(
    intel_by_key: dict[str, dict[str, Any]], by_asset_class: dict[str, Any]
) -> dict[str, list[str]]:
    """Intersect each cached event's affected asset classes with the classes
    this user actually holds (`portfolio_summary["by_asset_class"]`).

    This is the whole per-user half of L2 and it makes no LLM call — the
    expensive judgment ("what does this event bear on") was made once,
    globally; personalization is set membership.

    Reads `affected_asset_classes` ONLY. `affected_sectors` is stored for the
    forward-event holding-relevance mapping that already runs on `sector`
    (`report_sections._forward_exposure`) — CLAUDE.md's single sanctioned use
    of that column — and A3 deliberately does not widen it into a second
    exposure dimension here.

    An event with no overlap is omitted rather than carried as an empty
    entry: the caller's "which events touch me" question is answered by key
    presence.
    """
    held = set(by_asset_class)
    exposure: dict[str, list[str]] = {}
    for event_key, intel in intel_by_key.items():
        classes = intel.get("affected_asset_classes", [])
        overlap = [c for c in classes if c in held]
        if overlap:
            exposure[event_key] = overlap
    return exposure
