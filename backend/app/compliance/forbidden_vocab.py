"""Single source of truth for compliance vocabulary.

Both the output-side scan (FORBIDDEN_OUTPUT_PATTERNS) and the LLM system-prompt
blacklist (PROMPT_VOCAB_STRING) are derived from this module. Keeping them
co-located prevents drift where the prompt forbids a term the scan misses or
vice versa.

Adding a Chinese term propagates automatically to both the scan and the
prompt — edit ``config/compliance_vocab.yml`` (issue #90: the Chinese-language
term/pattern data lives there now, out of this module's source; this file is
the loading + compilation logic and its public API is unchanged). English
patterns stay in ``_EN_REGEX_PATTERNS`` here; ``recommend*`` is context-aware
(issue #375) so third-party house-view attribution does not hold a report.
Issue #443 brought the remaining bare EN literals in line with the ZH
treatment: ``entry point``/``target price`` dropped to prompt-only (mirrors
入场/目标价, #65); ``reduce exposure``/``increase position``/``stop-loss``
kept a directive-context scan (mirrors 止损/清仓, #74/#205); ``oversold``/
``overbought`` dropped from the scan entirely (join the unscanned TA
observation vocabulary); ``strong buy``/``bullish rating``/``bearish
rating``/``will rise to``/``will fall to`` kept a third-party-attribution-
aware scan (mirrors ``recommend*``, #375) — see the comment block above
``_EN_REGEX_PATTERNS`` for the per-term reasoning.
Run the compliance-scan regression tests (``pytest app/tests/test_output_scan.py``)
before promoting new terms to production.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from app.core.config import get_settings

# backend/ = two levels above this file (compliance/forbidden_vocab.py → app/ → backend/)
_BACKEND_DIR = Path(__file__).resolve().parents[2]
_DEFAULT_VOCAB_FILE = _BACKEND_DIR / "config" / "compliance_vocab.yml"

# ---------------------------------------------------------------------------
# English patterns (regex strings compiled at module load).
# Deliberately high-precision: bare "buy"/"sell"/"hold" are excluded to avoid
# false positives on factual prose ("buyback", "Holdings", "threshold").
#
# EN recommend* is context-aware (issue #375), same class as ZH 目标价/增持
# (#65, prompt-only) and 止损/清仓 (#74 / #205, scan_regex_patterns):
# first-person / product-to-user / "recommend you" / sentence-initial
# "recommend buying|selling|holding|reducing" still hold the report.
# Third-party house-view attribution ("UBS … recommends", "the bank
# recommends", "analysts recommend") does not. Prompt-side _EN_PROMPT_TERMS
# still lists "recommend". Accepted residual: a user-directed recommend that
# mimics third-party syntax in the same clause may slip — prefer that over
# holding bank-view attribution.
#
# entry point / target price are DROPPED from this scan entirely (issue
# #443, production hold 73d54b62): they are the direct EN counterparts of ZH
# 入场/目标价, which #65 already moved to prompt_only for the identical
# reason — both appear constantly in factual/analytical prose ("the
# valuation entry point for future commitments", "the bank's target price")
# that names no security/price/timing and directs nobody. They stay in
# _EN_PROMPT_TERMS below so the model is still told to avoid them.
#
# reduce exposure / increase position / stop-loss keep a scan, unlike entry
# point/target price, because a modal-anchored directive with urgency
# ("investors should reduce exposure to NVDA now", "set a stop-loss at $50")
# is a materially more specific Layer-4 instruction — same reasoning ZH
# used to keep 止损/清仓 as context-aware scan_regex_patterns instead of
# demoting them like 目标价/增持/减持 (#74/#205). Each pattern below allows
# third-party/factual narration (past tense, no modal, no reader-directed
# imperative) and blocks: modal directive ("should/must/need to/ought
# to/consider" + the verb), or a bare sentence-initial imperative.
#
# issue #443 scope revision (product-owner direction, 2026-09-12): a cited/
# quoted third-party article routinely carries oversold/overbought/strong
# buy/bullish-bearish rating/price-forecast vocabulary as factual background
# — the same false-positive shape #375 fixed for recommend*, just not yet
# extended past it. oversold/overbought are DROPPED from the scan entirely:
# unlike a verb, a bare state adjective ("the stock is oversold") has no
# directive-shaped use to preserve — they join support/resistance level/
# golden cross/breakout as fully-unscanned Layer-3 TA observation vocabulary
# (see test_scan_allows_ta_observation_vocabulary), while staying in
# _EN_PROMPT_TERMS so the model's own voice is still discouraged from using
# them. strong buy/bullish rating/bearish rating and will rise/fall to are
# ratings/forecasts, not states — closer to recommend* — so they keep the
# same third-party-attribution-aware technique: block only the model's own
# first-person/product voice or a bare sentence-initial declarative; a named
# third party (bank/analyst/desk) holding or citing the rating/forecast is
# not scanned, by construction (it does not match the narrow block anchors).
# Accepted residual, same class as recommend*'s: an unattributed claim
# buried mid-sentence after an intervening clause may slip — prefer that
# over holding third-party attribution.
#
# blacktomb42 PR #444 review round 2: the FIRST cut of the patterns below
# encoded one narrow surface form per term instead of the underlying
# directive/own-voice MEANING, and each family (action-directive vs.
# rating/forecast) had its own bespoke, non-reusable regex. Concretely, that
# first cut (a) missed natural possessive pronouns ("your exposure", "their
# position") and a nested modal ("should consider setting"), because each
# action pattern spelled out exactly one modal token and exactly one object
# form; and (b) used a bare "we"/"i"/"portfonia" token as a stand-in for
# "this IS the rating/forecast's grammatical subject", which is wrong in
# both directions — it missed the possessive "our" (no bare "we"/"i" token:
# "In our view, this is a strong buy"), and it falsely fired when a
# reporting verb put a NAMED THIRD PARTY between the pronoun and the claim
# ("We note that UBS has a strong buy rating..." — "we" reports what UBS
# said, it isn't the subject of "has a ... rating"). Fixed here by building
# every action-directive pattern from `_directive_pattern()` (one modal
# vocabulary, one possessive-aware object grammar, reused per verb) and every
# rating/forecast pattern from `_own_voice_or_bare_assertion()` (one
# `_OWN_VOICE_ANCHOR` — which excludes a pronoun immediately followed by a
# reporting verb — and one `_SENTENCE_START`, which now also recognizes a
# Markdown list-item marker). Adding a new modal, pronoun, or reporting verb
# in the future means editing one constant, not re-deriving five regexes.
# ---------------------------------------------------------------------------

# Zero-width "start of a sentence, OR start of a Markdown list item" anchor.
# Shared by every sentence-initial bare-declarative/imperative branch below
# (recommend*, the three action-directive patterns, and the two own-voice
# rating/forecast patterns) so a fix to this primitive (like the Markdown
# bullet case blacktomb42's review caught: "- This is a strong buy." was
# unscanned because "This" isn't at position 0 or right after ". ") applies
# everywhere at once instead of drifting between five separately-maintained
# copies.
_SENTENCE_START = (
    r"(?:(?<=^)|(?<=\n)|(?<=[.!?]\s)"
    r"|(?<=^[-*+]\s)|(?<=\n[-*+]\s)"
    r"|(?<=^\d\.\s)|(?<=\n\d\.\s))"
)

# Reporting verbs that introduce THIRD-PARTY content even when the sentence's
# own subject is first-person ("we NOTE that UBS has a strong buy rating..."
# is Portfonia reporting what UBS said, not Portfonia's own rating). Used
# only to gate _OWN_VOICE_ANCHOR below — expand this list, not the anchor
# logic itself, if another reporting verb turns up in production.
_REPORTING_VERBS = (
    r"note|notes|noted"
    r"|report|reports|reported"
    r"|mention|mentions|mentioned"
    r"|observe|observes|observed"
    r"|highlight|highlights|highlighted"
    r"|flag|flags|flagged"
)

# First-person/product-voice subject anchor shared by every "own-voice
# assertion" pattern below (ratings, price forecasts). Includes the
# possessive "our" (not just bare "we"/"i") since "In our view, ..." /
# "Our base case is ..." carry the model's own voice with no bare "we"/"i"
# token present. Excludes an anchor immediately followed by a _REPORTING_VERB
# — that shape introduces third-party content, not the model's own claim.
_OWN_VOICE_ANCHOR = rf"\b(?:we|i|our|portfonia)\b(?!\s+(?:{_REPORTING_VERBS})\b)"


def _own_voice_or_bare_assertion(phrase: str, bare_subject_and_verb: str, *, gap: int = 40) -> str:
    """Scan pattern for a RATING/FORECAST noun phrase (not a verb — see
    `recommend*` above for the verb-adjacency equivalent, which doesn't need
    this: a verb's own subject sits immediately before it).

    Blocks: (a) `phrase` within `gap` characters of `_OWN_VOICE_ANCHOR` (the
    model's own voice, however it phrases the assertion), or (b) a bare
    sentence-initial declarative naming the phrase directly
    (`bare_subject_and_verb` supplies the subject+copula, e.g.
    ``"this\\s+is\\s+a"``). Allows third-party attribution ("the bank has a
    strong buy rating...", "analysts expect it will rise to $200...") by
    construction: it matches neither anchor.
    """
    return (
        rf"{_OWN_VOICE_ANCHOR}[^.\n]{{0,{gap}}}?\b(?:{phrase})\b"
        r"|"
        rf"{_SENTENCE_START}{bare_subject_and_verb}\s+(?:{phrase})\b"
    )


# Modal vocabulary shared by every action-directive pattern below. `{1,2}`
# repetitions in `_directive_pattern` lets a nested modal ("should consider
# setting...") match without a bespoke alternative per combination.
_DIRECTIVE_MODAL = r"(?:should|must|need\s+to|ought\s+to|consider)"

# Determiner an object noun commonly carries in a directive ("reduce YOUR
# exposure", "increase THEIR position", "set A stop-loss") — shared so
# adding one (or dropping one) is a one-line change applied to every
# directive pattern built from this helper, not a per-pattern hunt-and-fix.
_DIRECTIVE_DETERMINER = r"(?:a\s+|your\s+|their\s+|its\s+)?"


def _directive_pattern(verb: str, object_noun: str, *, imperative_verb: str) -> str:
    """Scan pattern for a directive ACTION verb + object (reduce exposure,
    increase position, set a stop-loss).

    Blocks: (a) 1-2 `_DIRECTIVE_MODAL` tokens immediately before `verb`
    (covers "should reduce", "consider reducing", and the nested "should
    consider setting"), or (b) a bare sentence-initial imperative
    (`imperative_verb`, always the plain infinitive — "reduce"/"increase"/
    "set", never the gerund, so a gerund-subject sentence like "Reducing
    exposure to tech in Q3 helped the fund limit losses." stays a Layer-1/2
    description, not a directive). Allows third-party/factual narration
    (past tense, no modal, no reader-directed imperative) by construction.
    """
    object_pattern = rf"{_DIRECTIVE_DETERMINER}{object_noun}"
    return (
        rf"\b(?:{_DIRECTIVE_MODAL}\s+){{1,2}}{verb}\s+{object_pattern}\b"
        r"|"
        rf"{_SENTENCE_START}{imperative_verb}\s+{object_pattern}\b"
    )


_EN_REGEX_PATTERNS: tuple[str, ...] = (
    (
        r"(?:(?<=\bwe\s)|(?<=\bwe\swould\s)|(?<=\bwe\sstrongly\s)"
        r"|(?<=\bi\s)|(?<=\bi\swould\s)|(?<=\bi\sstrongly\s)"
        r"|(?<=\bportfonia\s)|(?<=\bthis\sreport\s))"
        r"recommend(?:s|ed|ing)?\b"
        r"|"
        r"\brecommend(?:s|ed|ing)?(?=\s+(?:that\s+)?you\b)"
        r"|"
        rf"{_SENTENCE_START}"
        r"recommend(?:s|ed|ing)?(?=\s+(?:buying|selling|holding|reducing)\b)"
    ),
    r"\bshould\s+(buy|sell|hold)\b",
    _directive_pattern(r"reduc(?:e|ing)", "exposure", imperative_verb="reduce"),
    _directive_pattern(r"increas(?:e|ing)", "position", imperative_verb="increase"),
    _directive_pattern(r"set(?:ting)?", r"stop[-\s]?loss", imperative_verb="set"),
    _own_voice_or_bare_assertion(
        r"strong\s+buy|(?:bullish|bearish)\s+rating",
        r"this\s+(?:is|was|remains|carries|looks\s+like)\s+a",
    ),
    _own_voice_or_bare_assertion(
        r"will\s+(?:rise|fall)\s+to",
        r"(?:this|it|the\s+stock|the\s+name|shares?|the\s+price)",
    ),
)

# Human-readable EN terms for injection into the LLM system prompt.
# Mirrors the intent of _EN_REGEX_PATTERNS; also covers "exit" and "hold"
# which the scan intentionally omits to avoid bare-word false positives.
# "recommend" stays on the prompt list even though the scan is context-aware
# (#375): the model is still told not to emit the verb.
_EN_PROMPT_TERMS: tuple[str, ...] = (
    "recommend",
    "should buy",
    "should sell",
    "should hold",
    "reduce exposure",
    "increase position",
    "exit",
    "stop-loss",
    "target price",
    "will rise to",
    "will fall to",
    "entry point",
    "oversold",
    "overbought",
    "strong buy",
    "bullish rating",
    "bearish rating",
)


@dataclass(frozen=True)
class _ZhVocab:
    scan_terms: tuple[str, ...]
    scan_regex_patterns: tuple[str, ...]
    prompt_only_terms: tuple[str, ...]
    context_scan_terms: tuple[str, ...]


def _get_vocab_path() -> Path:
    override = get_settings().COMPLIANCE_VOCAB_PATH
    return Path(override) if override else _DEFAULT_VOCAB_FILE


def _require_non_empty_str_list(value: object, field_name: str) -> list[str]:
    """Reject anything but a list of non-empty strings.

    Guards specifically against a scalar string sneaking past a bare
    ``not value`` emptiness check: a scalar is truthy and iterable, so
    ``tuple("止损[位点价]")`` would silently character-split into single-char
    "patterns" instead of raising (PR #91 re-review) — most single CJK
    characters still compile as a (silently wrong) regex, so this would not
    even fail loudly downstream.
    """
    if not isinstance(value, list) or not value:
        raise ValueError(f"compliance_vocab.yml: {field_name} must be a non-empty list")
    if not all(isinstance(item, str) and item for item in value):
        raise ValueError(f"compliance_vocab.yml: {field_name} must contain only non-empty strings")
    return value


def _load_zh_vocab(path: Path | None = None) -> _ZhVocab:
    """Load the Chinese-language compliance vocabulary from compliance_vocab.yml.

    Fails loudly on a config gap that would otherwise silently weaken the
    scan: an empty scan_terms/scan_regex_patterns means fewer compiled
    patterns — not a crash, not a false-everything match — so a bad edit
    that empties one of these would ship a quietly-weaker compliance
    backstop with no error anywhere near the mistake (PR #91 review). Also
    compiles every regex pattern here so a malformed one fails at config
    load, not the first time a report happens to hit it.
    """
    target = path or _get_vocab_path()
    with target.open(encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)

    scan_regex_patterns = _require_non_empty_str_list(
        raw["scan_regex_patterns"], "scan_regex_patterns"
    )
    for pattern in scan_regex_patterns:
        re.compile(pattern)
    context_scan_terms = tuple(
        _require_non_empty_str_list(raw["context_scan_terms"], "context_scan_terms")
    )

    scan_terms = tuple(entry["term"] for entry in raw["scan_terms"])
    if not scan_terms or not all(isinstance(t, str) and t for t in scan_terms):
        raise ValueError(
            "compliance_vocab.yml: scan_terms must be a non-empty list of non-empty strings"
        )
    prompt_only_terms = tuple(entry["term"] for entry in raw["prompt_only_terms"])
    if not prompt_only_terms or not all(isinstance(t, str) and t for t in prompt_only_terms):
        raise ValueError(
            "compliance_vocab.yml: prompt_only_terms must be a non-empty list of non-empty strings"
        )

    return _ZhVocab(
        scan_terms=scan_terms,
        scan_regex_patterns=tuple(scan_regex_patterns),
        prompt_only_terms=prompt_only_terms,
        context_scan_terms=context_scan_terms,
    )


_zh_vocab = _load_zh_vocab()
_ZH_SCAN_TERMS: tuple[str, ...] = _zh_vocab.scan_terms
_ZH_SCAN_REGEX_PATTERNS: tuple[str, ...] = _zh_vocab.scan_regex_patterns
_ZH_PROMPT_ONLY_TERMS: tuple[str, ...] = _zh_vocab.prompt_only_terms
_ZH_CONTEXT_SCAN_TERMS: tuple[str, ...] = _zh_vocab.context_scan_terms

# Combined set for the LLM prompt (scan terms + context-scan terms + prompt-only terms).
_ZH_LITERAL_TERMS: tuple[str, ...] = _ZH_SCAN_TERMS + _ZH_CONTEXT_SCAN_TERMS + _ZH_PROMPT_ONLY_TERMS

# ---------------------------------------------------------------------------
# Derived artefacts — import these into report_generator.py
# ---------------------------------------------------------------------------


def build_scan_patterns() -> list[re.Pattern[str]]:
    """Compile regex patterns for the output-side compliance scan.

    Includes _ZH_SCAN_TERMS (unambiguous advisory terms, literal match) and
    _ZH_SCAN_REGEX_PATTERNS (context-dependent terms, regex match). High-FP
    Chinese terms that appear in factual news are in _ZH_PROMPT_ONLY_TERMS and
    reach the LLM via build_prompt_vocab_string() only.
    """
    patterns: list[re.Pattern[str]] = []
    for p in _EN_REGEX_PATTERNS:
        patterns.append(re.compile(p, re.IGNORECASE))
    for term in _ZH_SCAN_TERMS:
        patterns.append(re.compile(re.escape(term)))
    for p in _ZH_SCAN_REGEX_PATTERNS:
        patterns.append(re.compile(p))
    return patterns


def build_prompt_vocab_string() -> str:
    """Comma-separated vocabulary string for injection into the LLM system prompt."""
    en = ", ".join(_EN_PROMPT_TERMS)
    zh = ", ".join(_ZH_LITERAL_TERMS)
    return f"{en}, {zh}"


# Module-level singletons — constructed once at import.
FORBIDDEN_OUTPUT_PATTERNS: list[re.Pattern[str]] = build_scan_patterns()
PROMPT_VOCAB_STRING: str = build_prompt_vocab_string()
