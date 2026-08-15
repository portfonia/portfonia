"""Layer-4 output-side compliance backstop: scan a report body for forbidden
advisory language, and strip citation/provenance/disclaimer noise before the
scan runs so it doesn't false-trip on the model's own redundant disclaimers.

Split out of report_generator.py (#37) — co-located with forbidden_vocab.py
(same package) since both are the same "compliance scaffolding" concern
documented in this repo's CLAUDE.md, not report-rendering machinery.
"""

from __future__ import annotations

import re

from app.compliance.forbidden_vocab import FORBIDDEN_OUTPUT_PATTERNS as _FORBIDDEN_OUTPUT_PATTERNS
from app.services.i18n_glossary import load_i18n_glossary

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


# Stray provenance/disclaimer tags the LLM may still emit despite the system
# prompt forbidding them. They are pure noise in the rendered report (the
# S-numbers don't resolve, the per-line disclaimer duplicates the footer), so we
# strip them as a backstop. Covers EN and the zh tags produced before #9 — the
# zh set loads from i18n_glossary.yml's legacy_removed_markers_zh, frozen into
# this compiled-pattern list at import (restart to pick up a YAML edit —
# same caveat as report_sections._RELEASE_DELAY_TERMS).
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
# at import, same restart caveat as _STRAY_TAGS above and
# report_sections._RELEASE_DELAY_TERMS. i18n_glossary.py's loader rejects an empty
# body_disclaimer_regex_terms_zh at load time, so this can't silently become a
# match-everything pattern via a leading empty regex alternative (PR #91 review).
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
