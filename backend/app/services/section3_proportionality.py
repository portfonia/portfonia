"""Issue #173: code-level check of §3 per-holding analysis length against
that holding's (weight, evidence-strength) pairing.

Ring 1 stage B / B1's original ask (issue #128, 2026-08-22 follow-up) was
"an explicit check: if a holding's analysis length is clearly out of step
with its weight-and-evidence-strength pairing, reject and reallocate."
`config/analysis_framework.yml`'s item 3 (PORTFOLIO SHAPE — WEIGHTED, NOT
FLATTENED) shipped only the prompt-level self-audit half of that — this
module is the code-level enforcement of the CHECK (not yet the
reject-and-reallocate action; see the log-only note below).

DESIGN DECISIONS (recorded on issue #173, product owner, 2026-09-10):

1. Evidence-strength scoring is a keyword/structural heuristic, no second
   LLM call (Requirements item 2). `EVIDENCE_CATEGORIES` mirrors
   `analysis_framework.yml` item 2's five structural-evidence categories.
   The score counts how many CATEGORIES have >=1 match, never raw item
   counts or price-move magnitude.

2. §3 segmentation is paragraph + identifier-match (issue #173 Design
   item 2, resolved against actual code 2026-09-10 — see the issue
   comment thread): `report_prompts.py`'s Pass 2 instructions and
   `report_assembly.py`'s assembly instructions both write §3 as ONE
   flowing prose block naming holdings inline — there is no per-holding
   markdown heading to parse. A paragraph naming exactly one holding
   attributes its full length to that holding; a paragraph naming more
   than one is excluded from any single holding's length (to avoid
   misattributing shared text) but counted as "mixed" for observability;
   a paragraph naming no known holding is ignored. This performs pure
   post-hoc text analysis on the already-generated Markdown — §3's
   prompt/output format is completely untouched.

3. Failure mode is LOG-ONLY (issue #173 Design item 3 / Contract
   constraints invariant): a mismatch is written to the log with the
   holding identifier, weight, evidence score, actual length, and
   expected range. This module never mutates report content, email
   delivery, or report status, and never re-prompts, truncates, or
   otherwise alters the generated body. `check_section3_proportionality`
   returns an int (mismatch count) purely for test/metric convenience —
   callers must not branch on it to change report behavior.

4. Weight is an EXPLICIT function parameter on `HoldingCheckInput`, never
   read internally from a holding's real position size (issue #173
   Design item 4). The caller wiring this into `report_generator.py`
   computes a holding's REAL weight and passes it in; issue #421's
   watched/zero-holding entries are expected to pass a config-driven
   target weight instead — this module has no way to tell the
   difference, by design.

Two-pass isolation (Contract constraints invariant): this module only
ever runs against Pass 2/assembly-stage output (§3 Markdown) and
Pass 2/assembly-stage material (news/anomalies already gathered for a
holding) — never against Pass 1's search-query generation. Nothing here
is wired into `_build_pass1_prompt`.
"""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from app.services.macro_detector import _make_pattern

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Evidence-strength scoring (Design item 1)
# ---------------------------------------------------------------------------

# Mirrors config/analysis_framework.yml item 2 (STRUCTURAL EVIDENCE EARNS
# DEPTH)'s five categories. Terms are a first-pass heuristic — tuned against
# real report output as the log-only data accumulates (issue #173 Design
# item 3's stated purpose), not an exhaustive NLP classifier.
EVIDENCE_CATEGORIES: dict[str, tuple[str, ...]] = {
    "third_party_reliance": (
        "customer",
        "partner",
        "supplier",
        "regulator",
        "licensing",
        "relies on",
        "reliance on",
        "客户",
        "合作伙伴",
        "供应商",
        "监管机构",
    ),
    "sustained_capital_commitment": (
        "capital expenditure",
        "capex",
        "continued to invest",
        "sustained investment",
        "expansion",
        "buildout",
        "build-out",
        "committed capital",
        "资本开支",
        "持续投资",
        "扩产",
    ),
    "insider_buying": (
        "insider purchase",
        "insider buying",
        "buyback",
        "share repurchase",
        "management bought",
        "内部人增持",
        "股票回购",
    ),
    "milestone_confirmation": (
        "certification",
        "regulatory approval",
        "cleared for",
        "milestone",
        "confirmed",
        "commercial launch",
        "go-live",
        "里程碑",
        "获批",
        "认证",
    ),
    "competitive_landscape_change": (
        "market share",
        "competitor",
        "displaced",
        "new entrant",
        "rival",
        "competitive position",
        "competitive landscape",
        "竞争格局",
        "市场份额",
        "竞争对手",
    ),
}


def score_evidence_strength(material_text: str) -> int:
    """0-5: how many of `EVIDENCE_CATEGORIES` have >=1 keyword match in
    `material_text` (the news/anomaly material gathered for one holding —
    never §3's own output; see module docstring point 4's boundary).

    Counts CATEGORIES matched, never raw term occurrences — three mentions
    of "customer" in the same material still score 1 for
    third_party_reliance, not 3 (Requirements item 2: "not from raw item
    counts").
    """
    if not material_text:
        return 0
    score = 0
    for terms in EVIDENCE_CATEGORIES.values():
        if any(_make_pattern(term).search(material_text) for term in terms):
            score += 1
    return score


# ---------------------------------------------------------------------------
# §3 extraction + segmentation (Design item 2)
# ---------------------------------------------------------------------------

_SECTION3_HEADING_RE = re.compile(r"^##\s*§3\b.*$", re.MULTILINE)
_NEXT_HEADING_RE = re.compile(r"^##\s+", re.MULTILINE)


def extract_section3(markdown: str) -> str:
    """The §3 body text between its own heading and the next `## ` heading
    (or end of string). Empty string if no §3 heading is present."""
    match = _SECTION3_HEADING_RE.search(markdown)
    if not match:
        return ""
    start = match.end()
    next_match = _NEXT_HEADING_RE.search(markdown, start)
    end = next_match.start() if next_match else len(markdown)
    return markdown[start:end]


@dataclass(frozen=True)
class Section3Segments:
    """Result of attributing §3's paragraphs to holdings by identifier
    mention (see module docstring point 2)."""

    by_identifier: dict[str, str]
    mixed_paragraph_count: int


def segment_section3_by_holding(
    section3_markdown: str,
    identifier_terms: Mapping[str, Sequence[str]],
) -> Section3Segments:
    """Split `section3_markdown` on blank lines into paragraphs and
    attribute each to the holding(s) it names.

    - A paragraph mentioning exactly one identifier's terms (word-boundary,
      case-insensitive — via `_make_pattern`, same matcher
      `holding_news.py` uses for "does this prose name this holding")
      attributes its FULL text to that holding.
    - A paragraph mentioning more than one holding's terms is excluded
      from every single holding's length (would silently misattribute
      shared text) but counted in `mixed_paragraph_count`.
    - A paragraph mentioning no known holding is ignored.
    """
    paragraphs = [p for p in re.split(r"\n\s*\n", section3_markdown) if p.strip()]
    by_identifier: dict[str, list[str]] = {ident: [] for ident in identifier_terms}
    mixed_count = 0

    for paragraph in paragraphs:
        matched = [
            ident
            for ident, terms in identifier_terms.items()
            if any(_make_pattern(term).search(paragraph) for term in terms if term)
        ]
        if len(matched) == 1:
            by_identifier[matched[0]].append(paragraph)
        elif len(matched) > 1:
            mixed_count += 1

    return Section3Segments(
        by_identifier={ident: "\n\n".join(parts) for ident, parts in by_identifier.items()},
        mixed_paragraph_count=mixed_count,
    )


# ---------------------------------------------------------------------------
# Expected length range (weight, evidence_score) -> (min_chars, max_chars)
# ---------------------------------------------------------------------------

# First-pass heuristic constants (issue #173 Design item 3: this check is
# deliberately the observability step, not the enforcement step — these
# numbers are meant to be tuned against real report data, not treated as a
# calibrated threshold). Both weight (0-1 fraction) and evidence_score
# (0-5) push the range wider, matching analysis_framework.yml item 3's
# "weight AND evidence strength together decide depth" rule.
#
# `min_chars` has NO unconditional base (PR #423 review, blacktomb42): an
# earlier version added a flat _BASE_MIN_CHARS=80 floor regardless of
# weight/evidence, so a holding with near-zero weight and no evidence that
# §3 legitimately never mentions — analysis_framework.yml item 5: "a theme
# with no direct, concrete mapping to an identifier ... does not earn its
# own paragraph" — still warned every time. min_chars now scales purely
# from weight/evidence, floored (not rounded) to 0 at the low end, so an
# immaterial, unevidenced holding's honest absence from §3 is in-range,
# not noise. `max_chars` keeps its base — the ceiling problem (an
# over-long paragraph on a small position) is unrelated to this fix.
_BASE_MAX_CHARS = 250
_WEIGHT_MIN_SCALE = 800
_WEIGHT_MAX_SCALE = 1600
_EVIDENCE_MIN_SCALE = 30
_EVIDENCE_MAX_SCALE = 80


def expected_length_range(weight: float, evidence_score: int) -> tuple[int, int]:
    """Expected §3 character-count range for a holding given its explicit
    (weight, evidence_score) pairing. `weight` is whatever the caller
    passed in (see module docstring point 4) — this function has no
    concept of a holding's "real" weight."""
    min_chars = weight * _WEIGHT_MIN_SCALE + evidence_score * _EVIDENCE_MIN_SCALE
    max_chars = _BASE_MAX_CHARS + weight * _WEIGHT_MAX_SCALE + evidence_score * _EVIDENCE_MAX_SCALE
    return (math.floor(min_chars), round(max_chars))


# ---------------------------------------------------------------------------
# Main check (log-only — Design item 3)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HoldingCheckInput:
    """One holding's input to the proportionality check.

    `weight` is EXPLICIT — the caller computes it (real position weight,
    or, for issue #421's watched entries, a config-driven target weight)
    and passes it in; this dataclass carries no reference to a `Holding`
    row (see module docstring point 4).

    `material_text` is the news/anomaly text already gathered for this
    holding during Pass 2/assembly — evidence is scored from what was
    SUPPLIED to the model, never from §3's own generated output.
    """

    identifier: str
    alias_terms: Sequence[str]
    weight: float
    material_text: str


def check_section3_proportionality(
    report_id: str,
    full_body_markdown: str,
    holdings: Sequence[HoldingCheckInput],
) -> int:
    """Log a mismatch for each holding whose §3 length falls outside the
    expected range for its (weight, evidence_score) pairing.

    Log-only (Design item 3 / Contract constraints invariant): never
    raises, never mutates `full_body_markdown`, never returns anything a
    caller could use to alter report content/status/email. The returned
    int (mismatch count) is for tests/metrics only.
    """
    section3 = extract_section3(full_body_markdown)
    identifier_terms = {h.identifier: h.alias_terms for h in holdings}
    segments = segment_section3_by_holding(section3, identifier_terms)

    mismatches = 0
    for holding in holdings:
        actual_text = segments.by_identifier.get(holding.identifier, "")
        actual_len = len(actual_text)
        evidence_score = score_evidence_strength(holding.material_text)
        exp_min, exp_max = expected_length_range(holding.weight, evidence_score)
        if not (exp_min <= actual_len <= exp_max):
            mismatches += 1
            logger.warning(
                "report %s: §3 proportionality mismatch — holding=%s weight=%.4f "
                "evidence_score=%d actual_len=%d expected_range=(%d,%d)",
                report_id,
                holding.identifier,
                holding.weight,
                evidence_score,
                actual_len,
                exp_min,
                exp_max,
            )

    if segments.mixed_paragraph_count:
        logger.info(
            "report %s: §3 proportionality check — %d paragraph(s) named multiple "
            "holdings, excluded from per-holding length attribution",
            report_id,
            segments.mixed_paragraph_count,
        )

    return mismatches
