"""L3 day-level cross-identifier synthesis (issue #128 quality gate — design
doc §6.7 item 1, Hermes/Portfonia/Docs/Ring 1-A design.md).

WHAT THIS LAYER IS FOR
----------------------
Three overlay comparisons against the same real 26-holding report (design doc
§6.6) established that assembly does NOT inherently lose per-name depth: when
L1 carried dated facts, the assembled body restated them. What it could not
produce was a cross-name conclusion — "TSM, ASML, AAOI and MUU moved with one
mechanism today, against the rate move" — and the reason is structural, not a
model or prompt deficiency:

    L1 is keyed per identifier      -> can say what happened to ONE name
    L2 is keyed per event           -> can say an event bears on a CLASS
    (nothing)                       -> "these names, today, one mechanism"

Pass 2 performs that join implicitly, inside its single per-user call, because
it sees the whole corpus at once. Assembly is contractually forbidden to add
edges L1/L2 never wrote (`report_assembly._ASSEMBLY_SYSTEM`), and relaxing
that would restore Pass 2's token curve and its licence to invent — the
explicit wrong fix recorded in §6.7. So the join has to exist as a fact
BEFORE assembly runs, which is what this module produces: one inference per
trading day, shared by every user, costing one call the whole fan-out splits.

THE TYPE BOUNDARY, STRONGER THAN L1/L2's
----------------------------------------
Design doc §4.8's rule — a cross-user shared cache may consume only globally
typed artifacts; SELECTION may be per-user, VALUES must not be — applies here
with an unusual simplification. L1 and L2 each needed a narrow per-user
selection channel (`l1_identifiers_for_user`, `l2_event_keys_for_user`,
both returning `list[str]`) because "which identifiers/events are worth
analyzing" is a per-user question. Here it is not: what this layer analyzes is
"every identifier the system briefed today", which is already a global fact
readable from `ticker_intel`. So `get_day_synthesis` takes `(session,
trade_date)` and nothing else — there is no parameter through which a
watermark, a portfolio, an anomaly list or a user_id COULD arrive, and a test
asserts that on the signature rather than trusting review attention.

WHERE THIS LAYER'S OWN LEAK WOULD BE, AND WHY THE OUTPUT IS SHAPED THIS WAY
--------------------------------------------------------------------------
A2/A3 had to keep per-user values OUT of a shared cache. A4 (assembly) has the
mirror risk: another user's shared rows reaching THIS user's report. L3 sits
on the A4 side of that line, and its risk is sharper than assembly's, because
its whole product is a statement about a GROUP of identifiers drawn from every
user's book at once.

The resolution is that the stored output must be decomposable:

    clusters: [{identifiers: [...], mechanism: <closed enum>,
                summary: <mechanism prose, NO names>, confidence: <label>}]

`identifiers` is structured, so `clusters_for_user` can intersect it with the
reader's own L1 keys. `summary` is required to describe the MECHANISM without
naming identifiers, because free text is not filterable — a day-level
paragraph naming everything analyzed today would carry other users' holdings
into this user's report body no matter how the list beside it were narrowed.
A prompt rule alone would not be enough (it is an instruction, not a
guarantee), so `clusters_for_user` drops any cluster whose summary names an
identifier the reader does not hold. Cost of that guard when it fires: one
missing sentence. Cost of not having it: design doc §1.3's cross-user leak,
arriving as prose.

WHY THE CACHE KEY CARRIES AN INPUT FINGERPRINT
----------------------------------------------
§6.7 specified `(trade_date, prompt_version)`, "one row a day (or an
equivalent global key)". A date-only key has a failure mode this codebase has
already paid for once: in a fan-out, the first user's `generate_report` writes
its L1 rows and then triggers the synthesis, freezing the day's conclusion to
whatever THAT book covered. Every later user reads a conclusion that
structurally cannot mention any of their names — the "early write locks the
day" shape round 6 found one layer down in L1's headline-only path (§4.8,
addendum 4). Adding a fingerprint of the global input set keeps the key global
(it is derived from `ticker_intel` rows, which have no user_id — never from
who asked) while letting a genuinely richer input set buy a better synthesis.
`_MAX_SYNTHESES_PER_DAY` plus `shared_budget.fair_share_budget` bound what
that can cost, and over cap the day degrades to the most recent stored
synthesis rather than to nothing.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.compliance.output_scan import _scan_forbidden_output, _strip_markers
from app.models.cross_name_intel import CrossNameIntel
from app.models.macro_event_intel import MacroEventIntel
from app.models.ticker_intel import TickerIntel
from app.services.email_sender import send_ops_alert
from app.services.holding_news import load_entity_aliases
from app.services.llm_errors import is_retryable
from app.services.macro_event_intel import _PROMPT_VERSION as _L2_PROMPT_VERSION
from app.services.report_llm import _call_llm, _openrouter_client
from app.services.report_prompts import _COMPLIANCE_SYSTEM_PREFIX
from app.services.shared_budget import fair_share_budget
from app.services.ticker_intel import _PROMPT_VERSION as _L1_PROMPT_VERSION
from app.services.transmission_taxonomy import VALID_TRANSMISSIONS

logger = logging.getLogger(__name__)

# Part of the unique key, not an audit column (same contract as
# `ticker_intel._PROMPT_VERSION` / `macro_event_intel._PROMPT_VERSION`):
# bumping it retires every stored row rather than serving an older contract's
# clusters under a new one.
_PROMPT_VERSION = "l3-v1"

# Same model and reasoning setting as L1 and assembly (design doc §6.6/§6.7:
# `openai/gpt-5.6-luna`, explicit effort, never the `-pro` suffix, which
# re-injects prior reasoning tokens and bills them again — Homepage
# reasoning-eval). `effort=none` is the starting point; §6.7 item 5 reserves
# raising it for THIS one call (never per-L1) if the compare is still thin.
_L3_MODEL = "openai/gpt-5.6-luna"
_L3_REASONING_EFFORT = "none"

# Fresh syntheses allowed per trading day, counted in ATTEMPTS
# (`SUM(attempt_count)`) rather than rows for the same reason L1/L2 count that
# way (issue #160): a retried key must not get its extra attempts free, or the
# ceiling loosens by a factor of `_MAX_ATTEMPTS_PER_KEY` on exactly the day it
# matters.
#
# MUST stay a multiple of `_MAX_ATTEMPTS_PER_KEY` well above 1x (PR #167
# review round 3): the original value (3, equal to `_MAX_ATTEMPTS_PER_KEY`)
# meant a SINGLE non-retryable failure or compliance block on the day's
# FIRST fingerprint wrote `attempt_count=3` in one shot — the entire daily
# budget — so every later, genuinely different fingerprint that trading day
# (a later fan-out user's newly-written L1 rows, the whole reason the
# fingerprint mechanism exists) hit `budget <= 0` and degraded to "serve the
# most recent stored synthesis", which on that day had none to serve. One
# incident lost the WHOLE DAY's cross-name conclusion for every user, not
# just the one who hit it.
#
# 9 = 3x `_MAX_ATTEMPTS_PER_KEY`: one lock still leaves 2x its own cost of
# headroom for later fingerprints, matching L2's forward-event ratio (15:3 =
# 5x) closely enough for L3's much smaller expected fingerprint count per
# day (identifier-set changes are comparatively rare within one trading
# day's fan-out) without being needlessly generous like L1's 15:3 (calibrated
# for potentially dozens of identifiers, not a handful of fingerprints).
#
# THIS IS THE CHEAPER OF TWO FIXES CONSIDERED, NOT A FULL FIX (design doc
# §6.7, PR #167 review round 3) — recorded here so a future session hitting
# the same shape does not re-litigate the tradeoff from scratch. The
# alternative — stop `attempt_count` from meaning both "real LLM calls
# spent" and "shared daily-budget slots consumed" at once — was rejected for
# THIS PR because both ways to actually separate them reopen a version of
# the problem #160 already closed:
#   (a) Give locks their own flag (e.g. a `locked: bool` column) so a lock
#       only ever records `attempt_count=1` against the daily sum. This is
#       the more surgical version of "just count separately" and DOES
#       shrink the blast radius of one lock — but it requires a schema
#       change, and it explicitly undoes a stated design decision in
#       `CrossNameIntel`'s own docstring ("one integer expresses both
#       states, so there is no second 'permanent' flag column to drift") —
#       and unless `TickerIntel`/`MacroEventIntel` get the same column, L3's
#       `attempt_count` and its two siblings would silently mean different
#       things despite being "one mechanism applied three times" by design.
#   (b) Make the daily cap count successful syntheses or distinct
#       fingerprints instead of `SUM(attempt_count)`. This is worse: it
#       means a failure that lands on a NEW fingerprint (which happens
#       naturally as the day's L1 data grows) never counts against the cap
#       at all — a bad provider day could retry unboundedly across an
#       ever-changing fingerprint stream with zero rate-limiting. That is
#       exactly the original pre-#160 bug ("a retried key gets extra
#       attempts free"), just relocated from identifier-granularity to
#       fingerprint-granularity.
# Bumping the constant keeps `attempt_count`'s meaning, the schema, and the
# three-table symmetry all unchanged; its only real cost is that it does not
# fully eliminate the failure mode (several locks in one day can still
# collectively exhaust 9), only bound it — a monotonic, easy-to-reason-about
# risk, unlike (a)/(b) above. Revisit if operational experience shows 9 is
# still not enough headroom, or if a future session wants to invest in (a)
# for all three tables at once (never for L3 alone).
_MAX_SYNTHESES_PER_DAY = 9

# Attempts the SYSTEM (not each user) may spend on one key before its marker
# row is final. Same value and same reasoning as `ticker_intel` and
# `macro_event_intel`: whatever reaches this handler already survived
# `_call_llm`'s own backoff, so the retry covers a blip that cleared between
# two users of one fan-out. Keep all three in step.
_MAX_ATTEMPTS_PER_KEY = 3

# A cross-name conclusion needs at least two names, on both the input side
# (nothing to join) and the output side (a one-name "cluster" is an L1 row).
_MIN_CLUSTER_SIZE = 2

# Bounds on what one call is fed. The input is one day's briefings, so it is
# naturally small, but a broad day across a growing user base should degrade
# by truncation rather than by an unbounded prompt.
_MAX_INPUT_IDENTIFIERS = 25
_MAX_ANALYSIS_CHARS = 1200
_MAX_INPUT_EVENTS = 12
_MAX_EVENT_CHARS = 600

# The three labels every causal attribution in this product must end with
# (CLAUDE.md, report content features). Ordered weakest-first is irrelevant
# here; what matters is that an unrecognized label falls back to the weakest
# rather than being dropped or silently upgraded.
_VALID_CONFIDENCE = ("Established", "Probable", "Speculative")
_WEAKEST_CONFIDENCE = "Speculative"
# Case-folded lookup back to the canonical spelling (PR #167 review round 1,
# nit): comparison is case-insensitive, but what gets STORED is always one of
# `_VALID_CONFIDENCE`'s exact strings, never the model's raw casing.
_CONFIDENCE_BY_CASEFOLD = {label.casefold(): label for label in _VALID_CONFIDENCE}

_L3_SYSTEM = _COMPLIANCE_SYSTEM_PREFIX + (
    "\nYou are performing ONE joint inference for an internal SHARED cache, "
    "over briefings that were each written about a single security today. "
    "Your output is reused for every user in the system, so it must contain "
    "NOTHING specific to any one user: no position size, portfolio weight, "
    "account value, or how many people hold anything.\n"
    "Your question is narrow: which of the supplied identifiers moved TODAY "
    "for the SAME underlying mechanism, and what is that mechanism? Group only "
    "what the supplied briefings actually support. A group may be same-"
    "direction or opposite-direction as long as one mechanism explains both "
    "sides (a rate move lifting one exposure and pressuring another is ONE "
    "mechanism). Identifiers that share nothing but a sector are not a group.\n"
    "Reply with a JSON object and nothing else:\n"
    '  {"clusters": [{"identifiers": [...], "mechanism": "...", '
    '"summary": "...", "confidence": "..."}]}\n'
    '  "identifiers": two or more, drawn ONLY from the supplied list, spelled '
    "exactly as supplied.\n"
    '  "mechanism": exactly one value from this closed set: '
    + ", ".join(sorted(VALID_TRANSMISSIONS))
    + "\n"
    '  "summary": one or two sentences naming the mechanism and the evidence '
    "for it from the supplied briefings. Write about the MECHANISM ONLY — do "
    "NOT name, list or allude to any identifier, ticker or company in this "
    "text; the identifiers are carried in the field above. No headings, no "
    "citations, no disclaimer.\n"
    '  "confidence": exactly one of Established (a named mechanism or citable '
    "event ties the group together), Probable (partial evidence), Speculative "
    "(hypothesis consistent with the moves).\n"
    "Return an empty clusters list rather than inventing a connection. Do not "
    "introduce events, figures or catalysts that appear nowhere in the "
    "supplied briefings."
)


@dataclass
class L3Facts:
    """The global inputs one synthesis was built from, kept for audit and for
    re-rendering the reasoning behind a stored cluster set.

    Both fields are global by construction: `briefings` comes from
    `ticker_intel` and `events` from `macro_event_intel`, neither of which has
    a user_id column to filter on even by accident.
    """

    briefings: dict[str, str]
    events: dict[str, str]

    def to_jsonb(self) -> dict[str, Any]:
        return {"briefings": self.briefings, "events": self.events}


def _load_day_briefings(session: Session, trade_date: date) -> dict[str, str]:
    """Every servable L1 analysis written for `trade_date`, system-wide, UNDER
    L1's CURRENT prompt contract only.

    Null-analysis marker rows are excluded: they mean "attempted, nothing to
    serve", so they carry no text to reason from and must not count toward the
    two-identifier floor either. Ordering is by identifier so the prompt (and
    therefore the fingerprint of what was fed) is deterministic.

    Filtering on `_L1_PROMPT_VERSION` (PR #167 review round 1, suggestion): a
    prior draft selected every non-null analysis for the date with no version
    filter at all. `ticker_intel` is unique on `(identifier, trade_date,
    prompt_version)`, so a same-day prompt-contract bump — or, as observed in
    production, a stale `l1-v3` row for an identifier sitting alongside its
    real `l1-v4` replacement — means two rows for one identifier can coexist
    on the same date. Without this filter, L3 could hash and synthesize from
    a RETIRED stub, reintroducing the "do not serve an older contract under a
    new one" rule ticker_intel's own unique key encodes, violated one layer up
    by this consumer.
    """
    rows = (
        session.execute(
            select(TickerIntel)
            .where(
                TickerIntel.trade_date == trade_date,
                TickerIntel.prompt_version == _L1_PROMPT_VERSION,
                TickerIntel.analysis.is_not(None),
            )
            .order_by(TickerIntel.identifier)
        )
        .scalars()
        .all()
    )
    out: dict[str, str] = {}
    for row in rows:
        analysis = (row.analysis or "").strip()
        if analysis and row.identifier not in out:
            out[row.identifier] = analysis[:_MAX_ANALYSIS_CHARS]
    return dict(list(out.items())[:_MAX_INPUT_IDENTIFIERS])


def _load_day_events(session: Session, trade_date: date) -> dict[str, str]:
    """Every servable L2 analysis for `trade_date`, system-wide, UNDER L2's
    CURRENT prompt contract only — the macro half of the join. Without it the
    model can observe that names moved together but has no vocabulary for
    WHY. Same `prompt_version` filter and rationale as `_load_day_briefings`
    above (PR #167 review round 1)."""
    rows = (
        session.execute(
            select(MacroEventIntel)
            .where(
                MacroEventIntel.trade_date == trade_date,
                MacroEventIntel.prompt_version == _L2_PROMPT_VERSION,
                MacroEventIntel.analysis.is_not(None),
            )
            .order_by(MacroEventIntel.event_key)
        )
        .scalars()
        .all()
    )
    out: dict[str, str] = {}
    for row in rows:
        analysis = (row.analysis or "").strip()
        if analysis and row.event_key not in out:
            out[row.event_key] = analysis[:_MAX_EVENT_CHARS]
    return dict(list(out.items())[:_MAX_INPUT_EVENTS])


def _fingerprint(briefings: dict[str, str], events: dict[str, str]) -> str:
    """Stable hash of WHICH identifiers and events this synthesis was built
    from — not of their text.

    Hashing the identifier/key sets rather than the analyses is deliberate:
    the point of the fingerprint is to notice that a later user contributed
    names the stored synthesis could not have covered. Re-running because an
    unrelated L1 row's wording changed would buy nothing.
    """
    payload = json.dumps(
        {"identifiers": sorted(briefings), "events": sorted(events)}, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _build_l3_prompt(facts: L3Facts) -> str:
    lines: list[str] = ["=== TODAY'S PER-SECURITY BRIEFINGS ==="]
    for identifier, analysis in facts.briefings.items():
        lines.append(f"{identifier}:")
        lines.append(f"  {analysis}")
    lines.append("")
    lines.append("=== TODAY'S MACRO EVENT ANALYSES ===")
    if facts.events:
        for key, analysis in facts.events.items():
            lines.append(f"{key}:")
            lines.append(f"  {analysis}")
    else:
        lines.append("(none analyzed today)")
    lines.append("")
    lines.append(
        "Return the JSON object described in your system instructions. Group only "
        "identifiers listed above, and only where the briefings support a shared "
        "mechanism."
    )
    return "\n".join(lines)


def _loads_or_none(text: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(text)
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def _parse_clusters(raw: str, allowed: set[str]) -> list[dict[str, Any]] | None:
    """Parse + validate the model's JSON against the closed sets. None means
    "unusable" (the caller turns that into a marker row); an empty list means
    "parsed fine, no connection found today" — a legitimate answer that is
    cached so the day is not re-analyzed once per user.

    Validation is deliberately asymmetric between the two closed sets:

    * An identifier outside `allowed` is dropped from its cluster, because the
      rest of the cluster is still a real conclusion. An invented ticker would
      not error downstream — it would print a name nobody analyzed into a
      report as if it had been.
    * An out-of-taxonomy `mechanism` drops the WHOLE cluster, because here the
      mechanism IS the conclusion. (Contrast A3's `_filter_to_taxonomy`, where
      dropping one asset-class label still leaves a usable event.)
    * An unrecognized `confidence` falls back to the weakest label rather than
      dropping the cluster: a label typo should cost precision, not the
      conclusion — but it must never be silently upgraded.
    """
    text = raw.strip()
    if text.startswith("```"):
        text = "\n".join(ln for ln in text.splitlines() if not ln.strip().startswith("```")).strip()
    parsed = _loads_or_none(text)
    if parsed is None:
        # Second chance on the outermost {...} span, same as
        # `macro_event_intel._parse_l2_response` (its round-1 review finding):
        # a model that prefaces its JSON with a sentence is a formatting
        # habit, and treating it as failure costs the day's only cross-name
        # conclusion for every user.
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            parsed = _loads_or_none(text[start : end + 1])
    if parsed is None:
        logger.warning("cross_name_intel: no usable JSON object in the synthesis response")
        return None

    raw_clusters = parsed.get("clusters")
    if not isinstance(raw_clusters, list):
        logger.warning(
            "cross_name_intel: 'clusters' was %s, not a list", type(raw_clusters).__name__
        )
        return None

    out: list[dict[str, Any]] = []
    for entry in raw_clusters:
        if not isinstance(entry, dict):
            continue
        mechanism = entry.get("mechanism")
        if not isinstance(mechanism, str) or mechanism.strip() not in VALID_TRANSMISSIONS:
            logger.warning(
                "cross_name_intel: dropping cluster with out-of-taxonomy mechanism %r", mechanism
            )
            continue
        summary = entry.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            logger.warning("cross_name_intel: dropping cluster with no summary text")
            continue
        raw_identifiers = entry.get("identifiers")
        if not isinstance(raw_identifiers, list):
            continue
        identifiers: list[str] = []
        dropped: list[str] = []
        for value in raw_identifiers:
            name = value.strip() if isinstance(value, str) else ""
            if name in allowed:
                if name not in identifiers:
                    identifiers.append(name)
            elif value:
                dropped.append(str(value))
        if dropped:
            logger.warning(
                "cross_name_intel: dropped identifier(s) %s that were not briefed today", dropped
            )
        if len(identifiers) < _MIN_CLUSTER_SIZE:
            continue
        confidence = entry.get("confidence")
        raw_label = confidence.strip() if isinstance(confidence, str) else ""
        # Case-folded lookup (PR #167 review round 1, nit): a case-sensitive
        # `in` check treated a real, correctly-spelled answer like "probable"
        # as unrecognized and silently downgraded it — conservative, but a
        # real conclusion mislabeled. The canonical (title-cased) spelling is
        # what gets stored, never the model's raw casing, so downstream
        # consumers can keep comparing against the exact `_VALID_CONFIDENCE`
        # strings.
        label = _CONFIDENCE_BY_CASEFOLD.get(raw_label.casefold(), _WEAKEST_CONFIDENCE)
        out.append(
            {
                "identifiers": identifiers,
                "mechanism": mechanism.strip(),
                "summary": summary.strip(),
                "confidence": label,
            }
        )
    return out


def _fetch_cached(session: Session, trade_date: date, fingerprint: str) -> CrossNameIntel | None:
    """`populate_existing=True` for the reason `ticker_intel._fetch_cached`
    documents at length: the whole fan-out shares ONE Session, and the Core
    upsert in `_write_cache` does not refresh an already-identity-mapped
    instance, so without it a later user re-reads a stale `attempt_count` and
    keeps attempting past the cap."""
    return session.execute(
        select(CrossNameIntel)
        .where(
            CrossNameIntel.trade_date == trade_date,
            CrossNameIntel.prompt_version == _PROMPT_VERSION,
            CrossNameIntel.input_fingerprint == fingerprint,
        )
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()


def _latest_stored(session: Session, trade_date: date) -> list[dict[str, Any]] | None:
    """The most recent servable synthesis for the day, whatever input set
    produced it.

    Used when the day's budget is spent: a slightly stale conclusion built
    from a subset of today's identifiers is worth more than nothing, and
    `clusters_for_user` narrows it to the reader's own names anyway, so a
    cluster that covers none of them simply disappears.
    """
    row = session.execute(
        select(CrossNameIntel)
        .where(
            CrossNameIntel.trade_date == trade_date,
            CrossNameIntel.prompt_version == _PROMPT_VERSION,
            CrossNameIntel.clusters.is_not(None),
        )
        .order_by(CrossNameIntel.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    return list(row.clusters) if row is not None and row.clusters is not None else None


def _attempts_today(session: Session, trade_date: date) -> int:
    """Attempts made today, NOT rows written (issue #160) — one retried key
    consumes as much of the day's budget as two distinct input sets would."""
    return int(
        session.execute(
            select(func.coalesce(func.sum(CrossNameIntel.attempt_count), 0)).where(
                CrossNameIntel.trade_date == trade_date,
                CrossNameIntel.prompt_version == _PROMPT_VERSION,
            )
        ).scalar_one()
    )


def _write_cache(
    session: Session,
    trade_date: date,
    fingerprint: str,
    model: str,
    clusters: list[dict[str, Any]] | None,
    facts: L3Facts,
    attempt_count: int,
) -> None:
    """`clusters=None` writes the "attempted, no usable result" marker row.

    Upsert with a `clusters IS NULL` guard, exactly as `ticker_intel` and
    `macro_event_intel` do: a retry must be able to raise a marker's
    `attempt_count` and to replace it with a real result, while a stored
    result can never be overwritten by a later marker.
    """
    stmt = pg_insert(CrossNameIntel).values(
        trade_date=trade_date,
        prompt_version=_PROMPT_VERSION,
        input_fingerprint=fingerprint,
        model=model,
        clusters=clusters,
        facts=facts.to_jsonb(),
        attempt_count=attempt_count,
    )
    session.execute(
        stmt.on_conflict_do_update(
            constraint="uq_cross_name_intel_date_version_fingerprint",
            set_={
                "model": stmt.excluded.model,
                "clusters": stmt.excluded.clusters,
                "facts": stmt.excluded.facts,
                "attempt_count": stmt.excluded.attempt_count,
            },
            where=CrossNameIntel.clusters.is_(None),
        )
    )
    session.flush()


def _generate(
    session: Session,
    trade_date: date,
    fingerprint: str,
    facts: L3Facts,
    usage_sink: list[dict[str, Any]] | None = None,
    attempts_so_far: int = 0,
) -> tuple[list[dict[str, Any]] | None, int]:
    """One synthesis call, gated by the compliance scan, always writing a row.

    Failure handling is L1/L2's contract verbatim (issue #160): a retryable
    fault records this attempt and leaves the rest of the key's allowance for
    a later caller in the same fan-out; a non-retryable fault, unusable JSON,
    or a compliance block writes `_MAX_ATTEMPTS_PER_KEY` and locks the key.
    Unparseable JSON counts as retryable for the reason A3 gives — the model
    is non-deterministic even at temperature 0, and `_parse_clusters` has
    already spent its free, no-new-call second chance on the same text.

    Returns `(clusters, budget_charged)` where `budget_charged` is exactly how
    much the write moved `SUM(attempt_count)`, so the budget this call spends
    and the budget the next caller recomputes from the SUM are the same
    quantity (PR #162 review round 1, the same trap in L1/L2).

    `with_holdings=False`: the payload is per-security briefings and macro
    analyses, none of it holdings-derived beyond the identifier strings — the
    same call shape L1 uses. `data_collection=deny` stays enforced (no BYOK
    exception) because those identifiers are still holdings-derived in origin.
    """
    prompt = _build_l3_prompt(facts)
    this_attempt = attempts_so_far + 1
    try:
        client = _openrouter_client()
        raw = _call_llm(
            client,
            _L3_MODEL,
            _L3_SYSTEM,
            prompt,
            with_holdings=False,
            reasoning_effort=_L3_REASONING_EFFORT,
            usage_sink=usage_sink,
        )
    except Exception as exc:
        logger.exception(
            "cross_name_intel: synthesis call failed for %s (attempt %d/%d)",
            trade_date,
            this_attempt,
            _MAX_ATTEMPTS_PER_KEY,
        )
        recorded = this_attempt if is_retryable(exc) else _MAX_ATTEMPTS_PER_KEY
        _write_cache(session, trade_date, fingerprint, _L3_MODEL, None, facts, recorded)
        return None, recorded - attempts_so_far

    clusters = _parse_clusters(raw, set(facts.briefings))
    if clusters is None:
        _write_cache(session, trade_date, fingerprint, _L3_MODEL, None, facts, this_attempt)
        return None, this_attempt - attempts_so_far

    # Strip markers BEFORE scanning, same as Pass 2 and (since L1's round-7
    # finding) L1: a model-emitted disclaimer line legitimately contains
    # advisory-sounding wording, and letting it false-trip the scan would
    # blacklist the day's only cross-name conclusion for every user.
    for cluster in clusters:
        cluster["summary"] = _strip_markers(cluster["summary"]).strip()
    clusters = [c for c in clusters if c["summary"]]

    violations = _scan_forbidden_output("\n".join(c["summary"] for c in clusters))
    if violations:
        logger.error(
            "cross_name_intel: synthesis for %s BLOCKED by compliance scan: %s",
            trade_date,
            violations,
        )
        send_ops_alert(
            subject=f"[Portfonia] L3 cross-name synthesis BLOCKED for compliance — {trade_date}",
            body=(
                f"The day-level cross-identifier synthesis for {trade_date} tripped the "
                f"forbidden-vocabulary scan: {violations}\n\n"
                f"The text was NOT stored or served. A null-clusters marker row was "
                f"written for (trade_date={trade_date}, prompt_version={_PROMPT_VERSION}, "
                f"input_fingerprint={fingerprint[:12]}...), so this input set will not be "
                f"re-attempted today. Reports for the affected users continue without a "
                f"cross-name conclusion; every other section is unaffected."
            ),
            idempotency_key=f"ops-l3-blocked-{trade_date}-{fingerprint[:12]}",
        )
        # Locked immediately rather than retried, same as L1/L2: #160's retry
        # budget is for transport faults, and re-running a generation that
        # already produced forbidden vocabulary re-risks it and re-alerts ops.
        _write_cache(
            session, trade_date, fingerprint, _L3_MODEL, None, facts, _MAX_ATTEMPTS_PER_KEY
        )
        return None, _MAX_ATTEMPTS_PER_KEY - attempts_so_far

    _write_cache(session, trade_date, fingerprint, _L3_MODEL, clusters, facts, this_attempt)
    return clusters, this_attempt - attempts_so_far


def get_day_synthesis(
    session: Session,
    trade_date: date,
    usage_sink: list[dict[str, Any]] | None = None,
    users_remaining: int = 1,
) -> list[dict[str, Any]]:
    """The day's cross-name clusters, computed once per input set and shared.

    Note what is NOT in this signature: no `user_id`, no window, no portfolio,
    no anomaly list. The inputs are read here from `ticker_intel` and
    `macro_event_intel`, both global tables — see the module docstring on why
    this layer needs no per-user selection channel at all, unlike L1 and L2.

    Degradation, in the order it is decided:

    * Fewer than two servable briefings today -> `[]`, and NOTHING is written,
      not even an attempt marker (design doc §4.8 addendum 4's rule): a later
      user in the same fan-out may well raise the count past two, and a marker
      would lock that out for the rest of the day.
    * Cache hit on this exact input set -> served, no LLM call.
    * Marker at the attempt cap -> `[]` for this input set.
    * Day's budget spent -> the most recent stored synthesis, which
      `clusters_for_user` narrows anyway.

    `users_remaining` applies `shared_budget.fair_share_budget` for the same
    reason A4 threaded it through L1/L2: a shared capped daily resource
    consumed in the fixed `active_user_ids` order starves the same users every
    day. `1` (a manual run, a test, any pre-fan-out call site) means no
    restriction.
    """
    briefings = _load_day_briefings(session, trade_date)
    if len(briefings) < _MIN_CLUSTER_SIZE:
        logger.info(
            "cross_name_intel: %d briefing(s) for %s — too few to join, skipping",
            len(briefings),
            trade_date,
        )
        return []

    events = _load_day_events(session, trade_date)
    facts = L3Facts(briefings=briefings, events=events)
    fingerprint = _fingerprint(briefings, events)

    cached = _fetch_cached(session, trade_date, fingerprint)
    attempts_so_far = 0
    if cached is not None:
        if cached.clusters is not None:
            return list(cached.clusters)
        attempts_so_far = cached.attempt_count
        if attempts_so_far >= _MAX_ATTEMPTS_PER_KEY:
            return _latest_stored(session, trade_date) or []

    budget = fair_share_budget(
        _MAX_SYNTHESES_PER_DAY - _attempts_today(session, trade_date), users_remaining
    )
    if budget <= 0:
        logger.info(
            "cross_name_intel: daily synthesis cap (%d) reached for %s, "
            "serving the most recent stored synthesis",
            _MAX_SYNTHESES_PER_DAY,
            trade_date,
        )
        return _latest_stored(session, trade_date) or []

    clusters, _charged = _generate(
        session, trade_date, fingerprint, facts, usage_sink, attempts_so_far
    )
    if clusters is None:
        return _latest_stored(session, trade_date) or []
    return clusters


def day_briefed_identifiers(session: Session, trade_date: date) -> list[str]:
    """Every identifier with a servable L1 briefing on `trade_date`, under
    L1's current prompt contract — the full universe the L3 synthesis prompt
    exposed the model to, whether or not a given identifier ended up assigned
    to a returned cluster.

    This is what `clusters_for_user`'s leak guard is built from (PR #167
    review round 1, bug 2): the model saw every one of these names in its
    prompt, so a leak is not confined to "an identifier that was in THIS
    cluster before filtering" — it can be any name the model was shown,
    whether or not the model chose to group it into any cluster at all.
    """
    return list(_load_day_briefings(session, trade_date))


def _mentions(text: str, identifier: str) -> bool:
    """Whether `text` names `identifier` as a standalone token.

    Word-boundary matching, and `.`/`^` are escaped by `re.escape`, so
    "513650.SS" does not match on its numeric prefix alone and "TSM" does not
    fire inside "TSMC" — the false-positive direction costs a real sentence,
    so it is worth being precise about.
    """
    return (
        re.search(rf"(?<![\w.]){re.escape(identifier)}(?![\w.])", text, re.IGNORECASE) is not None
    )


def _denylist_terms(identifier: str, entity_aliases: dict[str, list[str]]) -> list[str]:
    """Every text form `_mentions` should treat as naming `identifier`: the
    identifier itself, its un-suffixed stem (`"513650.SS"` -> `"513650"`),
    and any configured ENTITY-NAME alias (`"MUU"` -> `"Micron"`) — so a leak
    cannot route around the raw ticker by using the company name or a bare
    A-share/HK code the model was equally free to write (PR #167 review
    round 1, bug 2, part 2).

    `entity_aliases` (PR #167 review round 2, suggestion) is
    `holding_news.load_entity_aliases`'s table — a STRICT SUBSET of the
    recall-purpose `holding_news_keywords.yml` `holdings` key L1's own
    recall uses, not that table itself. The full recall table mixes real
    entity names with theme/technology tokens ("gold", "lithography",
    "Nasdaq") that a genuinely name-free mechanism summary is EXPECTED to
    use; dumping the whole thing in here silently dropped legitimate
    cross-name sentences for any reader missing an unrelated identifier
    that merely shares a theme word that day. Only a real company/entity
    name is grounds to say the prose "names" the identifier."""
    terms = [identifier]
    stem = identifier.split(".", 1)[0]
    if stem != identifier:
        terms.append(stem)
    terms.extend(entity_aliases.get(identifier, []))
    return terms


def clusters_for_user(
    clusters: list[dict[str, Any]],
    l1_keys: list[str],
    all_briefed_identifiers: list[str],
) -> list[dict[str, Any]]:
    """Narrow a day's global clusters to what this reader may see.

    Takes no `Session` — with no DB handle it cannot widen its own input to
    "every cluster cached today", only filter what the per-user caller passed
    (the same argument `report_assembly` makes for its own signature).
    `all_briefed_identifiers` (from `day_briefed_identifiers`, above) is
    itself just a plain list the caller already has a `Session` to fetch —
    passing it in, rather than letting this function query for it, keeps that
    boundary intact while still giving the leak guard below the full picture.

    Two rules, and the second is the one that matters:

    1. Keep only identifiers this user actually has L1 intel for, and drop a
       cluster left with fewer than two: one name is not a cross-name
       conclusion, and "this holding belongs to a group" whose other members
       the reader does not hold is both useless and disclosive.
    2. Drop the cluster entirely if its summary NAMES an identifier this
       reader does not hold. The identifier list is filterable; prose is
       not. The system prompt tells the model to write name-free mechanism
       summaries, but a prompt is an instruction, not a guarantee — and the
       failure it guards against is another user's holding appearing in this
       user's report body.

    `all_briefed_identifiers` — not just this cluster's own filtered-out
    members — is the denylist's source (PR #167 review round 1, bug 2, part
    1): a summary can name ANY identifier the model was shown that day,
    including one that belongs to a DIFFERENT cluster in the same set, or
    one the model chose not to cluster at all. Checking only "this cluster's
    own excluded members" left both of those unrouted. The denylist is also
    expanded through `_denylist_terms` (part 2): a raw-ticker-only check
    missed a company name or an un-suffixed stem naming the same excluded
    identifier.
    """
    keys = {k.upper() for k in l1_keys if k}
    universe = {str(i).upper() for i in all_briefed_identifiers if i}
    excluded = sorted(universe - keys)
    entity_aliases = load_entity_aliases()
    denylist = [
        term for identifier in excluded for term in _denylist_terms(identifier, entity_aliases)
    ]

    out: list[dict[str, Any]] = []
    for cluster in clusters:
        identifiers = [i for i in cluster.get("identifiers", []) if str(i).upper() in keys]
        if len(identifiers) < _MIN_CLUSTER_SIZE:
            continue
        summary = str(cluster.get("summary", ""))
        if any(_mentions(summary, term) for term in denylist):
            logger.warning(
                "cross_name_intel: dropping a cluster whose summary names an identifier "
                "this reader does not hold"
            )
            continue
        out.append({**cluster, "identifiers": identifiers})
    return out
