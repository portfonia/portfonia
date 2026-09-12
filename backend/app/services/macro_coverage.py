"""Macro coverage continuity (issue #440): read back what a user's own past
reports actually told them about a macro development, so the next report can
say what changed instead of repeating the explanation or dropping a
continuing story once it stops making headlines.

Three responsibilities, deliberately kept in one small module rather than
split across report_prompts.py/report_generator.py (this is a self-contained
mechanism, unlike report_context.py's shape which mirrors the whole pipeline):

  1. READ  `load_recent_macro_coverage` — bounded, success-only, per-user.
  2. RENDER `render_macro_continuity_block` — the prompt text both Pass 2 and
     the A4 assembly pass inject (report_prompts.py wires this in; see its
     "both generation paths satisfy the same macro content contract"
     invariant, Contract constraints).
  3. WRITE  `extract_macro_sidecar` (parse what the model rendered) +
     `persist_macro_coverage` (delete-then-insert, so a regenerate's dropped
     topic never survives as a stale row — Contract constraints "Regenerate
     after a topic was removed: No stale coverage for that report").

Eligibility is a READ-time policy (`Report.status == "success"`, the
accepted default for D4 "success-available coverage eligibility") — writes
always persist whatever the model actually rendered, regardless of the
report's own final status, keeping the write path uniform. A `needs_review`/
`failed` report's coverage rows simply never surface in a later report's
continuity read.

`_CONTINUITY_LOOKBACK_REPORTS` and the excerpt/list-length caps below are
routine engineering parameters within the accepted design, not additional
product policy — Design §8/owner decision 2026-09-12 explicitly declines to
freeze every fine threshold up front and asks that they be revisited only if
real report output shows a problem.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models.macro_coverage import (
    VALID_MACRO_COVERAGE_MODES,
    VALID_MACRO_DEPTH_TIERS,
    MacroCoverage,
)
from app.models.report import Report

logger = logging.getLogger(__name__)

# How many of a user's own most recent SUCCESSFUL reports contribute
# continuity candidates. ~2-3 weeks of history at the MWF cadence (issue
# #191) — long enough that a topic which goes quiet for a report or two is
# still recognized as continuing, short enough that continuity does not
# silently retain something the reader has long since moved past. A routine
# parameter, not one of Design D1-D7's blocking decisions.
_CONTINUITY_LOOKBACK_REPORTS = 8

# How many distinct development_keys the continuity block surfaces to the
# prompt, and how long a prior-paragraph excerpt it carries. Bounds prompt
# size; coverage rows beyond this are simply not offered as continuity this
# run, not deleted.
_MAX_CONTINUITY_ITEMS = 10
_EXCERPT_CHARS = 320
_MAX_LIST_ITEMS_PER_FIELD = 8
_MAX_ITEMS_PER_SIDECAR = 6

_DEV_KEY_WS_RE = re.compile(r"\s+")

_SIDECAR_START = "<!--MACRO_COVERAGE"
_SIDECAR_END = "MACRO_COVERAGE-->"
_SIDECAR_RE = re.compile(re.escape(_SIDECAR_START) + r"(.*?)" + re.escape(_SIDECAR_END), re.DOTALL)


def _normalize_development_key(raw: str) -> str:
    return _DEV_KEY_WS_RE.sub(" ", raw).strip().lower()


def _clean_str_list(value: Any, *, limit: int = _MAX_LIST_ITEMS_PER_FIELD) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for entry in value[:limit]:
        text = str(entry).strip()
        if text:
            out.append(text)
    return out


# ---------------------------------------------------------------------------
# 1. READ — continuity candidates for the NEXT report
# ---------------------------------------------------------------------------


def load_recent_macro_coverage(
    session: Session,
    user_id: uuid.UUID,
    *,
    exclude_report_id: uuid.UUID | None = None,
    lookback_reports: int = _CONTINUITY_LOOKBACK_REPORTS,
) -> list[dict[str, Any]]:
    """Most-recent coverage row per `development_key`, for this user, from
    this user's own last `lookback_reports` SUCCESSFUL reports (excluding
    `exclude_report_id` — self-exclusion for a regenerate's own row, Design
    §5 "Analyze regeneration excludes the current report from historical
    context").

    Returns plain dicts, newest `as_of` first, capped at
    `_MAX_CONTINUITY_ITEMS` distinct development_keys. Never raises on a
    cold user (no report history yet) — returns `[]`.
    """
    recent_report_ids_stmt = (
        select(Report.id)
        .where(Report.user_id == user_id, Report.status == "success")
        .order_by(Report.period_end.desc().nullslast())
        .limit(lookback_reports + (1 if exclude_report_id is not None else 0))
    )
    recent_report_ids = [
        row_id for row_id in session.execute(recent_report_ids_stmt).scalars().all()
    ]
    if exclude_report_id is not None:
        recent_report_ids = [rid for rid in recent_report_ids if rid != exclude_report_id][
            :lookback_reports
        ]
    if not recent_report_ids:
        return []

    rows = (
        session.execute(
            select(MacroCoverage)
            .where(MacroCoverage.report_id.in_(recent_report_ids))
            .order_by(MacroCoverage.as_of.desc())
        )
        .scalars()
        .all()
    )

    by_key: dict[str, MacroCoverage] = {}
    for row in rows:
        # Rows already come newest-first (query ordering); keep the first
        # (= most recent) row seen per development_key.
        by_key.setdefault(row.development_key, row)

    ordered = sorted(by_key.values(), key=lambda r: r.as_of, reverse=True)
    out: list[dict[str, Any]] = []
    for row in ordered[:_MAX_CONTINUITY_ITEMS]:
        out.append(
            {
                "development_key": row.development_key,
                "theme_keys": list(row.theme_keys or []),
                "as_of": row.as_of.isoformat(),
                "coverage_mode": row.coverage_mode,
                "depth_tier": row.depth_tier,
                "open_questions": list(row.open_questions or []),
                "observables": list(row.observables or []),
                "affected_identifiers": list(row.affected_identifiers or []),
                "paragraph_excerpt": (row.paragraph_text or "")[:_EXCERPT_CHARS],
            }
        )
    return out


# ---------------------------------------------------------------------------
# 2. RENDER — the prompt block both Pass 2 and assembly inject
# ---------------------------------------------------------------------------


def render_macro_continuity_block(items: list[dict[str, Any]]) -> str:
    """Render the MACRO COVERAGE CONTINUITY prompt block, or "" when there is
    nothing to offer (a cold user, or a user with no eligible prior
    coverage) — an empty block is simply omitted, exactly like every other
    optional block in this prompt (see `_build_investor_preferences_block`).
    """
    if not items:
        return ""
    lines = [
        "",
        "=== MACRO COVERAGE CONTINUITY (what this reader has already been "
        "told — never restate it verbatim) ===",
    ]
    for item in items:
        as_of = str(item.get("as_of", ""))[:10]
        lines.append(
            f"development_key: {item.get('development_key', '')} "
            f"(last covered {as_of}, depth: {item.get('depth_tier', '')}, "
            f"mode: {item.get('coverage_mode', '')})"
        )
        open_qs = item.get("open_questions") or []
        if open_qs:
            lines.append("  open question(s) left for this reader: " + "; ".join(open_qs))
        observables = item.get("observables") or []
        if observables:
            lines.append("  observable(s) flagged to watch: " + "; ".join(observables))
        excerpt = item.get("paragraph_excerpt") or ""
        if excerpt:
            lines.append(f"  prior treatment (excerpt, for your reference only): {excerpt}")
    lines.append(
        "A development_key above remains ELIGIBLE for this report even with no new "
        "headline this period — a continuing condition your own evidence still "
        "confirms is not manufactured novelty. If you cover it again, say plainly "
        "what changed (or was confirmed) since the prior treatment above; do not "
        "reintroduce it as if this reader is seeing it for the first time, and do "
        "not repeat the prior explanation's background at the same depth. If "
        "nothing has changed and it does not warrant fresh space, omit it rather "
        "than pad this report with an unchanged restatement."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Sidecar contract — how the model reports back what it actually rendered
# ---------------------------------------------------------------------------


def build_macro_sidecar_instruction() -> str:
    """The instruction appended once, at the very end of the §2-writing
    prompt (Pass 2 and assembly both use this — Contract constraints "Pass 2
    and assembly receive equivalent macro plan").

    D7 (structured sidecar, not hidden inline markers): the model appends
    ONE machine-readable block, delimited so it is trivially stripped
    BEFORE any rendering/translation ever sees it (report_generator.py
    extracts it off the raw body first thing) — unlike the old per-sentence
    marker suggestion this issue's Exploration explicitly does not carry
    forward, nothing here is ever exposed to the reader.
    """
    return (
        "\n\nMACRO COVERAGE RECORD (required, machine-readable, never shown to the "
        "reader): after writing §2/§3/§4 above, append exactly one block in this "
        f"exact form, with nothing after it:\n{_SIDECAR_START}\n"
        '{"items": [{"development_key": "<a short, stable, lowercase slug for this '
        "development — the SAME slug you or a future report would use for this "
        'exact story or continuing question>", "theme_keys": ["<theme>"], '
        '"coverage_mode": "NEW|UPDATE|STATE_REVIEW", "depth_tier": '
        '"anchor|update|context", "facts_covered": ["<fact you stated>"], '
        '"explanations_covered": ["<mechanism/explanation you gave>"], '
        '"open_questions": ["<question left open for a future report>"], '
        '"observables": ["<concrete, checkable thing to watch>"], '
        '"affected_identifiers": ["<held/watched identifier you actually discussed>"], '
        '"paragraph_excerpt": "<first ~2 sentences of what you wrote about it>"}'
        f", ...]}}\n{_SIDECAR_END}\n"
        "One item per development_key actually covered in §2 this report — not "
        "every candidate you considered, and nothing from §3/§4. Emit "
        '{"items": []} if §2 covered no distinct macro development this report.'
    )


class MacroCoverageItem:
    """One validated sidecar item, ready to persist."""

    __slots__ = (
        "affected_identifiers",
        "coverage_mode",
        "depth_tier",
        "development_key",
        "explanations_covered",
        "facts_covered",
        "observables",
        "open_questions",
        "paragraph_excerpt",
        "theme_keys",
    )

    def __init__(
        self,
        development_key: str,
        theme_keys: list[str],
        coverage_mode: str,
        depth_tier: str,
        facts_covered: list[str],
        explanations_covered: list[str],
        open_questions: list[str],
        observables: list[str],
        affected_identifiers: list[str],
        paragraph_excerpt: str,
    ) -> None:
        self.development_key = development_key
        self.theme_keys = theme_keys
        self.coverage_mode = coverage_mode
        self.depth_tier = depth_tier
        self.facts_covered = facts_covered
        self.explanations_covered = explanations_covered
        self.open_questions = open_questions
        self.observables = observables
        self.affected_identifiers = affected_identifiers
        self.paragraph_excerpt = paragraph_excerpt


def _parse_sidecar_item(raw: Any) -> MacroCoverageItem | None:
    if not isinstance(raw, dict):
        return None
    development_key = _normalize_development_key(str(raw.get("development_key") or ""))
    coverage_mode = str(raw.get("coverage_mode") or "").strip().upper()
    depth_tier = str(raw.get("depth_tier") or "").strip().lower()
    if not development_key:
        return None
    if coverage_mode not in VALID_MACRO_COVERAGE_MODES:
        logger.warning("macro sidecar: dropping item with invalid coverage_mode %r", coverage_mode)
        return None
    if depth_tier not in VALID_MACRO_DEPTH_TIERS:
        logger.warning("macro sidecar: dropping item with invalid depth_tier %r", depth_tier)
        return None
    return MacroCoverageItem(
        development_key=development_key,
        theme_keys=_clean_str_list(raw.get("theme_keys")),
        coverage_mode=coverage_mode,
        depth_tier=depth_tier,
        facts_covered=_clean_str_list(raw.get("facts_covered")),
        explanations_covered=_clean_str_list(raw.get("explanations_covered")),
        open_questions=_clean_str_list(raw.get("open_questions")),
        observables=_clean_str_list(raw.get("observables")),
        affected_identifiers=_clean_str_list(raw.get("affected_identifiers")),
        paragraph_excerpt=str(raw.get("paragraph_excerpt") or "").strip()[:_EXCERPT_CHARS],
    )


def extract_macro_sidecar(raw_body: str) -> tuple[str, list[MacroCoverageItem]]:
    """Split `raw_body` into (visible_body, parsed_items).

    `visible_body` has the sidecar block removed regardless of whether it
    parsed — a malformed sidecar must never leak into the rendered report.
    A missing sidecar (older prompt version, or a model that ignored the
    instruction) returns the body unchanged and `[]` — log-only, never
    raises: this is enrichment, not a report-generation dependency, the
    same non-fatal posture as the §3 proportionality check
    (report_generator._render_full_md).
    """
    match = _SIDECAR_RE.search(raw_body)
    if not match:
        return raw_body, []
    visible = (raw_body[: match.start()] + raw_body[match.end() :]).rstrip()
    payload = match.group(1).strip()
    # Models sometimes wrap the JSON in a markdown fence despite the exact
    # form requested — same defensive strip as Pass 1's query parsing
    # (report_generator.py).
    if payload.startswith("```"):
        payload = "\n".join(
            ln for ln in payload.splitlines() if not ln.strip().startswith("```")
        ).strip()
    try:
        parsed = json.loads(payload)
    except Exception:
        logger.warning("macro sidecar: could not parse JSON — no coverage recorded this report")
        return visible, []
    raw_items = parsed.get("items") if isinstance(parsed, dict) else None
    if not isinstance(raw_items, list):
        return visible, []
    items: list[MacroCoverageItem] = []
    for raw_item in raw_items[:_MAX_ITEMS_PER_SIDECAR]:
        item = _parse_sidecar_item(raw_item)
        if item is not None:
            items.append(item)
    return visible, items


# ---------------------------------------------------------------------------
# 3. WRITE — atomic with the report's own status/body commit
# ---------------------------------------------------------------------------


def persist_macro_coverage(
    session: Session,
    *,
    report_id: uuid.UUID,
    user_id: uuid.UUID,
    as_of: datetime,
    items: list[MacroCoverageItem],
) -> None:
    """Replace every `macro_coverage` row for `report_id` with `items`.

    Delete-then-insert, not upsert: a regenerate that drops a topic must not
    leave its old row behind (Contract constraints "Regenerate after a
    topic was removed: No stale coverage for that report"). Idempotent for
    every caller — a fresh generation writes no prior rows for this
    report_id to delete; a retry/resume or `regenerate_report(mode=
    "analyze")` replaces its own prior attempt's rows.

    Caller is responsible for calling this BEFORE `session.commit()` in the
    same transaction as the report row's own status/body write (Contract
    constraints "Generate/retry transaction failure: No finalized
    report/coverage half-commit") — this function only `flush()`es.
    """
    session.execute(delete(MacroCoverage).where(MacroCoverage.report_id == report_id))
    for item in items:
        session.add(
            MacroCoverage(
                user_id=user_id,
                report_id=report_id,
                development_key=item.development_key,
                theme_keys=item.theme_keys,
                as_of=as_of,
                coverage_mode=item.coverage_mode,
                depth_tier=item.depth_tier,
                facts_covered=item.facts_covered,
                explanations_covered=item.explanations_covered,
                open_questions=item.open_questions,
                observables=item.observables,
                affected_identifiers=item.affected_identifiers,
                paragraph_text=item.paragraph_excerpt or None,
            )
        )
    session.flush()
