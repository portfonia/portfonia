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
# Chinese terms (literal strings; matched via re.escape in the scan).
#
# False-positive risk is noted inline — terms that frequently appear in
# *factual* financial news (e.g. "机构减持公告") carry moderate risk and
# should be monitored against a report corpus before Ring 1 multi-user launch.
# "建议" alone is excluded: too broad for high-precision scan (appears in news
# as third-party attribution: "分析师建议关注…"). 投资建议 covers the primary form.
# ---------------------------------------------------------------------------
_ZH_LITERAL_TERMS: tuple[str, ...] = (
    # Core advisory — low false-positive risk
    "止损",  # stop-loss
    "强烈买入",  # strong buy
    "目标价",  # target price
    "投资建议",  # investment advice / advisory recommendation
    # Position-action verbs — moderate risk: also appear in factual news
    # ("机构增持XX", "大股东减持"). Monitor after adding.
    "减持",  # reduce / trim holding
    "增持",  # add to / increase holding
    "清仓",  # liquidate / exit entire position
    # Entry/timing signal terms — low-to-moderate risk
    "入场",  # enter a position / entry point
    # Overbought / oversold in Chinese — low risk (specific TA terms)
    "超买",  # overbought
    "超卖",  # oversold
)

# ---------------------------------------------------------------------------
# Derived artefacts — import these into report_generator.py
# ---------------------------------------------------------------------------


def build_scan_patterns() -> list[re.Pattern[str]]:
    """Compile regex patterns for the output-side compliance scan."""
    patterns: list[re.Pattern[str]] = []
    for p in _EN_REGEX_PATTERNS:
        patterns.append(re.compile(p, re.IGNORECASE))
    for term in _ZH_LITERAL_TERMS:
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
