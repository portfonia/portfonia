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
# blacktomb42 PR #444 review, four rounds, each CHANGES_REQUESTED, each
# found a real defect verified independently before fixing:
#
# Round 2: the FIRST cut encoded one narrow surface form per term instead of
# the underlying directive/own-voice MEANING, with each family (action-
# directive vs. rating/forecast) using its own bespoke, non-reusable regex —
# missing natural possessive pronouns ("your exposure"), a nested modal
# ("should consider setting"), and using a bare "we"/"i"/"portfonia" token as
# a stand-in for "grammatical subject" (missing possessive "our"; falsely
# firing on any pronoun immediately followed by a reporting verb regardless
# of what came after).
#
# Round 3: the reporting-verb-adjacency fix was still not evidence of a
# genuine third party — a reporting verb (or none at all, e.g. "According to
# our source, ...") immediately after the pronoun proves nothing on its own.
# Also found the sentence-initial branch missed a named instrument ("NVDA is
# a strong buy.") and `_SENTENCE_START` missed indented Markdown list items.
#
# Round 4: round 3's "at least one intervening word proves a subject change"
# rule was itself too loose — "We STRONGLY expect it will fall to $80" and
# "Our TEAM believes NVDA will rise to $200" both have an intervening word
# before the attribution verb, but neither is a genuine third party: an
# adverb modifying the SAME verb the pronoun governs, or a generic noun
# that's still Portfonia's own referent after "our"/"my", isn't a subject
# change. Also found the sentence-initial branch recreated the original
# false-positive class it was meant to fix: "UBS's rating remains a strong
# buy" (a real third-party possessive) matched the same generic-subject
# pattern as "NVDA is a strong buy" (a real own-voice claim).
#
# Fixed by building every action-directive pattern from `_directive_pattern()`
# (one modal vocabulary, one determiner grammar, reused per verb) and every
# rating/forecast pattern from `_own_voice_or_bare_assertion()`, itself built
# on: `_own_voice_anchor()` (requires a word NOT in the enumerable, closed
# `_SELF_REFERENT_WORDS` set — not just "any word" — to sit directly before
# an `_ATTRIBUTION_VERBS` token, proving that verb's subject is a genuinely
# different, named party); a generic `_BARE_SUBJECT` guarded by
# `_THIRD_PARTY_POSSESSIVE` (excludes "X's rating/view/..." from the bare-
# assertion match); and `_SENTENCE_START` (generated for a 0-3-space
# Markdown list-item indent, since a Python lookbehind must be fixed-width
# and can't express `{0,3}` directly). Adding a new modal, determiner,
# pronoun, self-referent filler, or attribution verb/noun going forward is a
# one-line change to one shared constant, not a per-pattern hunt across five
# regexes.
# ---------------------------------------------------------------------------


# Zero-width "start of a sentence, OR start of a Markdown list item" anchor.
# Shared by every sentence-initial bare-declarative/imperative branch below
# (recommend*, the three action-directive patterns, and the two own-voice
# rating/forecast patterns) so a fix to this primitive (like the Markdown
# bullet case blacktomb42's round-2 review caught: "- This is a strong buy."
# was unscanned because "This" isn't at position 0 or right after ". ")
# applies everywhere at once instead of drifting between five
# separately-maintained copies. A Python lookbehind must be fixed-width, so
# a {0,3}-space indent (round-3 review: "  - This is..." still bypassed the
# unindented-only version) is spelled out as one fixed-width alternative per
# depth rather than a single variable-width lookbehind.
def _sentence_start() -> str:
    parts = [r"(?<=^)", r"(?<=\n)", r"(?<=[.!?]\s)"]
    for line_start in (r"^", r"\n"):
        for indent in range(4):  # 0-3 leading spaces: common list-nesting range
            spaces = " " * indent
            parts.append(rf"(?<={line_start}{spaces}[-*+]\s)")
            parts.append(rf"(?<={line_start}{spaces}\d\.\s)")
    return "(?:" + "|".join(parts) + ")"


_SENTENCE_START = _sentence_start()

# Verbs that describe what a THIRD PARTY does with a rating/forecast (a bank
# HAS a rating, an analyst EXPECTS a price, a desk MAINTAINS a rating) — as
# opposed to a bare copula/"will" that the model itself uses to assert the
# claim directly ("NVDA IS a strong buy", "it WILL rise to $200"). Evidence
# of third-party content requires one of these immediately after a subject
# that ISN'T self-referential (`_SELF_REFERENT_WORDS` below) — deliberately
# excludes "rate(s)"/"view(s)"/"carrie(s)" even though a bank could
# grammatically take them too ("we RATE this a strong buy" / "Portfonia
# VIEWS the name with a bullish rating" must stay the model's own voice —
# those are exactly the verbs the model uses to phrase its own claim, so
# treating them as third-party evidence would open the same hole this
# fixes).
_ATTRIBUTION_VERBS = r"has|have|expects?|believes?|maintains?|says?|holds?|sets?|gives?"

# Words that can sit between the pronoun and an `_ATTRIBUTION_VERBS` token
# WITHOUT being a genuine third-party subject — either they're common
# adverbs modifying the SAME verb the pronoun governs ("we STRONGLY
# expect"), or they're a generic noun that's still Portfonia's own referent
# when it follows "our"/"my" ("our TEAM believes", "our ANALYSIS says").
# blacktomb42 PR #444 round-4 review: round 3's "at least one intervening
# word" rule treated ANY word here as proof of a subject change, which these
# adverbs/nouns falsify — a real third-party subject is a NAME, not one of
# these closed, enumerable self-referential fillers.
_SELF_REFERENT_WORDS = (
    r"we|i|our|my|portfonia"
    r"|now|currently|still|already|also|strongly|really|clearly|firmly|confidently"
    r"|actively|recently|generally|typically"
    r"|team|analysis|source|view|opinion|take|read|model|assessment|outlook|estimate|case"
)


def _own_voice_anchor(phrase: str, *, gap: int) -> str:
    """First-person/product-voice subject anchor for a specific rating/
    forecast `phrase` (ratings, price forecasts — a noun phrase, not a verb;
    see `recommend*` above for the verb-adjacency equivalent, which doesn't
    need this since a verb's own subject sits immediately before it).

    Matches "we"/"i"/"our"/"portfonia" (the possessive "our" catches "In our
    view, ..." / "Our base case is ..." with no bare "we"/"i" token present)
    UNLESS a word that is NOT in `_SELF_REFERENT_WORDS` sits directly before
    an `_ATTRIBUTION_VERBS` token, itself before `phrase` — that shape ("we
    note that UBS HAS a strong buy rating...", "according to our source,
    UBS HAS...") means a genuinely different, named subject governs the
    attribution verb, so the pronoun only introduces/frames third-party
    content. Requiring that word to be non-self-referential (not an adverb,
    not a generic own-referent noun) is load-bearing: "we STRONGLY expect it
    will fall" and "our TEAM believes NVDA will rise" both have a word
    between the pronoun and the attribution verb, but neither is a genuine
    subject change — "strongly" modifies "we expect" itself, and "team" is
    still Portfonia's own team. Only a real name ("UBS", "Morningstar")
    passes the exclusion, exactly as intended.
    """
    third_party_subject = (
        rf"\b(?!(?:{_SELF_REFERENT_WORDS})\b)[A-Za-z][\w.&'-]*(?:'s)?"
        rf"\s+(?:{_ATTRIBUTION_VERBS})\b"
    )
    return (
        rf"\b(?:we|i|our|portfonia)\b"
        rf"(?![^.\n]{{0,{gap}}}?{third_party_subject}[^.\n]{{0,{gap}}}?\b(?:{phrase})\b)"
    )


# Generic sentence-initial subject for a bare, unattributed rating/forecast
# declarative — 1-2 words so it covers a named instrument ("NVDA is a strong
# buy.") as well as the closed pronoun set ("This is...", "The stock will
# rise..."), without so wide a span that it swallows an attribution clause
# ahead of the phrase ("UBS expects the" is 3 words — capped at 2 so it
# cannot absorb "UBS expects" as if it were the "subject" of "will rise to").
_BARE_SUBJECT = r"[A-Za-z][\w.&'-]*(?:\s+[A-Za-z][\w.&'-]*)?"

# A named third party's own possessive rating/view/forecast ("UBS's rating
# remains a strong buy", "Morningstar's view is a strong buy") is Layer-1/2
# attribution — X holds the rating, it isn't the model's own claim about the
# instrument. blacktomb42 PR #444 round-4 review: `_BARE_SUBJECT` alone
# can't tell "NVDA is a strong buy" (bare instrument, block) from "UBS'S
# RATING is a strong buy" (third-party possessive, allow) since both are
# "1-2 words + copula + phrase" — this pattern names the specific shape to
# exclude from the sentence-initial branch.
_ATTRIBUTION_NOUN = r"rating|view|forecast|outlook|estimate|opinion|target"
_THIRD_PARTY_POSSESSIVE = rf"[A-Za-z][\w.&'-]*'s\s+(?:{_ATTRIBUTION_NOUN})\s+"


def _own_voice_or_bare_assertion(phrase: str, bare_verb: str = "", *, gap: int = 40) -> str:
    """Scan pattern for a RATING/FORECAST noun phrase.

    Blocks: (a) `phrase` within `gap` characters of `_own_voice_anchor`, or
    (b) a bare sentence-initial declarative — any short subject (`
    _BARE_SUBJECT`, so "This"/"It"/"NVDA"/"Apple" are all covered) plus
    `bare_verb` (empty for forecasts, whose `phrase` already starts with
    "will"; a copula clause like ``"is\\s+a"`` for ratings) plus `phrase`,
    UNLESS that subject is a `_THIRD_PARTY_POSSESSIVE` ("UBS's rating...").
    Allows third-party attribution ("the bank has a strong buy rating...",
    "analysts expect it will rise to $200...", "UBS's rating remains a
    strong buy...") by construction.
    """
    anchor = _own_voice_anchor(phrase, gap=gap)
    verb_gap = rf"{bare_verb}\s+" if bare_verb else ""
    return (
        rf"{anchor}[^.\n]{{0,{gap}}}?\b(?:{phrase})\b"
        r"|"
        rf"{_SENTENCE_START}(?!{_THIRD_PARTY_POSSESSIVE}){_BARE_SUBJECT}\s+{verb_gap}(?:{phrase})\b"
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
        r"(?:is|was|remains|carries|looks\s+like)\s+a",
    ),
    _own_voice_or_bare_assertion(r"will\s+(?:rise|fall)\s+to"),
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
