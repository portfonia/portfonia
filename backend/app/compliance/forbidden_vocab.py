"""Single source of truth for compliance vocabulary.

Both the output-side scan (FORBIDDEN_OUTPUT_PATTERNS) and the LLM system-prompt
blacklist (PROMPT_VOCAB_STRING) are derived from this module. Keeping them
co-located prevents drift where the prompt forbids a term the scan misses or
vice versa.

Adding a term here propagates automatically to both the scan and the prompt.
Run the corpus check (pytest tests/test_compliance.py -k corpus) before
promoting new terms to production.
"""

import re

# ---------------------------------------------------------------------------
# English patterns (regex strings compiled at module load).
# Deliberately high-precision: bare "buy"/"sell"/"hold" are excluded to avoid
# false positives on factual prose ("buyback", "Holdings", "threshold").
# ---------------------------------------------------------------------------
_EN_REGEX_PATTERNS: tuple[str, ...] = (
    r"\brecommend\w*",
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

# ---------------------------------------------------------------------------
# Chinese terms for the OUTPUT SCAN (literal strings; matched via re.escape).
#
# Principle: the scan is a backstop for unambiguous direct-advisory language
# only. Terms that routinely appear in factual financial news as descriptions
# of third-party actions (e.g. "机构增持XX", "银行下调目标价") are NOT scanned —
# they belong in the LLM prompt blacklist instead, where the model is told to
# avoid them, but a factual reference does not trigger a needs_review hold.
#
# The four moderate-risk terms (目标价, 增持, 减持, 入场) were moved to
# _ZH_PROMPT_ONLY_TERMS after report 11797c1c was incorrectly held for quoting
# third-party bank/institution actions (issue #65).
# ---------------------------------------------------------------------------
_ZH_SCAN_TERMS: tuple[str, ...] = (
    # Core advisory — unambiguous in any context
    "止损",  # stop-loss directive
    "强烈买入",  # strong buy rating
    "投资建议",  # investment advice
    "清仓",  # liquidate entire position
    # Overbought / oversold — specific TA recommendation terms, low FP risk
    "超买",  # overbought
    "超卖",  # oversold
)

# ---------------------------------------------------------------------------
# Chinese terms for PROMPT injection ONLY (not scanned).
#
# The LLM is instructed to avoid these, but they are not scanned in output
# because they appear routinely in Layer-1/2 factual descriptions of what
# third parties did (banks set target prices, institutions added positions,
# buyers entered at lows). Scanning for them generates false positives that
# suppress legitimate reports without blocking any actual advisory output.
# ---------------------------------------------------------------------------
_ZH_PROMPT_ONLY_TERMS: tuple[str, ...] = (
    "目标价",  # target price — appears in news: "银行下调目标价"
    "增持",  # add to position — appears in news: "机构增持XX"
    "减持",  # reduce position — appears in news: "大股东减持"
    "入场",  # entry point — appears in news: "买家视低点为入场时机"
)

# Combined set for the LLM prompt (scan terms + prompt-only terms).
_ZH_LITERAL_TERMS: tuple[str, ...] = _ZH_SCAN_TERMS + _ZH_PROMPT_ONLY_TERMS

# ---------------------------------------------------------------------------
# Derived artefacts — import these into report_generator.py
# ---------------------------------------------------------------------------


def build_scan_patterns() -> list[re.Pattern[str]]:
    """Compile regex patterns for the output-side compliance scan.

    Only includes _ZH_SCAN_TERMS (unambiguous advisory terms). High-FP Chinese
    terms that appear in factual news are in _ZH_PROMPT_ONLY_TERMS and reach
    the LLM via build_prompt_vocab_string() only.
    """
    patterns: list[re.Pattern[str]] = []
    for p in _EN_REGEX_PATTERNS:
        patterns.append(re.compile(p, re.IGNORECASE))
    for term in _ZH_SCAN_TERMS:
        patterns.append(re.compile(re.escape(term)))
    return patterns


def build_prompt_vocab_string() -> str:
    """Comma-separated vocabulary string for injection into the LLM system prompt."""
    en = ", ".join(_EN_PROMPT_TERMS)
    zh = ", ".join(_ZH_LITERAL_TERMS)
    return f"{en}, {zh}"


# Module-level singletons — constructed once at import.
FORBIDDEN_OUTPUT_PATTERNS: list[re.Pattern[str]] = build_scan_patterns()
PROMPT_VOCAB_STRING: str = build_prompt_vocab_string()
