"""Single source of truth for compliance vocabulary.

Both the output-side scan (FORBIDDEN_OUTPUT_PATTERNS) and the LLM system-prompt
blacklist (PROMPT_VOCAB_STRING) are derived from this module. Keeping them
co-located prevents drift where the prompt forbids a term the scan misses or
vice versa.

Adding a term propagates automatically to both the scan and the prompt — edit
``config/compliance_vocab.yml`` (issue #90: the Chinese-language term/pattern
data lives there now, out of this module's source; this file is the loading +
compilation logic and its public API is unchanged). Run the compliance-scan
regression tests (``pytest app/tests/test_report_generator.py -k scan``)
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
# ---------------------------------------------------------------------------
_EN_REGEX_PATTERNS: tuple[str, ...] = (
    r"\brecommend(?:s|ed|ing)?\b",
    r"\bshould\s+(buy|sell|hold)\b",
    r"\breduce\s+exposure\b",
    r"\bincrease\s+(your\s+)?position\b",
    r"\bstop[-\s]?loss\b",
    r"\btarget\s+price\b",
    r"\bentry\s+point\b",
    r"\boversold\b",
    r"\boverbought\b",
    r"\bstrong\s+buy\b",
    r"\b(bullish|bearish)\s+rating\b",
    r"\bwill\s+(rise|fall)\s+to\b",
)

# Human-readable EN terms for injection into the LLM system prompt.
# Mirrors the intent of _EN_REGEX_PATTERNS; also covers "exit" and "hold"
# which the scan intentionally omits to avoid bare-word false positives.
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
    context_scan_term: str


def _get_vocab_path() -> Path:
    override = get_settings().COMPLIANCE_VOCAB_PATH
    return Path(override) if override else _DEFAULT_VOCAB_FILE


def _load_zh_vocab(path: Path | None = None) -> _ZhVocab:
    """Load the Chinese-language compliance vocabulary from compliance_vocab.yml."""
    target = path or _get_vocab_path()
    with target.open(encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    return _ZhVocab(
        scan_terms=tuple(entry["term"] for entry in raw["scan_terms"]),
        scan_regex_patterns=tuple(raw["scan_regex_patterns"]),
        prompt_only_terms=tuple(entry["term"] for entry in raw["prompt_only_terms"]),
        context_scan_term=raw["context_scan_term"],
    )


_zh_vocab = _load_zh_vocab()
_ZH_SCAN_TERMS: tuple[str, ...] = _zh_vocab.scan_terms
_ZH_SCAN_REGEX_PATTERNS: tuple[str, ...] = _zh_vocab.scan_regex_patterns
_ZH_PROMPT_ONLY_TERMS: tuple[str, ...] = _zh_vocab.prompt_only_terms
_ZH_CONTEXT_SCAN_TERMS: tuple[str, ...] = (_zh_vocab.context_scan_term,)

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
