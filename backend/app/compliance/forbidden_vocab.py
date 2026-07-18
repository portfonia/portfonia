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
#
# "止损" was moved to _ZH_SCAN_REGEX_PATTERNS (below) after report 9b61b18e
# was incorrectly held: "…这种模式与早盘被迫平仓或止损驱动的抛售一致…" describes
# OTHER market participants' stop-loss orders triggering a sell-off (Layer-1/2
# market-mechanism observation), not a directive to the user. Bare literal
# matching cannot tell "止损驱动的抛售" (describes what happened) apart from
# "建议止损" (tells the user what to do) — see the context-aware pattern.
# ---------------------------------------------------------------------------
_ZH_SCAN_TERMS: tuple[str, ...] = (
    # Core advisory — unambiguous in any context
    "强烈买入",  # strong buy rating
    "投资建议",  # investment advice
    "清仓",  # liquidate entire position
    # Overbought / oversold — specific TA recommendation terms, low FP risk
    "超买",  # overbought
    "超卖",  # oversold
)

# ---------------------------------------------------------------------------
# Chinese patterns requiring context to disambiguate advisory-directive usage
# from factual market-mechanism description. Unlike _ZH_SCAN_TERMS (bare
# literal match), these only fire when 止损 appears as an instruction to the
# user, not as a description of stop-loss orders triggering in the market.
#
# Blocks: "建议止损" / "应该止损" / "止损位/点/价在100" / "立即止损" /
#         "跌破91.50止损" / "建议投资者止损" / "应该马上进行止损" /
#         "建议止损。注意风险" / "建议止损并注意流动性"
# Allows: "止损驱动的抛售" / "触发止损盘" / "止损盘涌现" / "应该注意到止损盘涌现" /
#         "立即触发止损盘" / "触及止损线" / "跌破后触发止损" / "价格触及止损"
#         (third-party market action, or Layer-3 "worth watching" phrasing)
#
# History (issue #74):
# v1 tightened the modal-verb gap from 0-6 to 0-2 chars to exclude "应该注意
# 到止损盘涌现" — silently dropped real advisory phrasing with a 3+ char
# subject/adverb ("建议投资者止损" / "应该马上进行止损").
# v2 kept the 0-6 gap and added a negative lookahead `(?!.{0,6}?(关注|...))`
# instead — but `.` in a lookahead is unbounded, so it could see PAST 止损
# into the next clause: "建议止损。注意风险" was wrongly excluded because 注意
# appears 3 chars after 止损, not because it appears between 建议 and 止损.
# v3 (this version) uses a per-character walk `(?:(?!关注|...)[^。,，、\n]){0,6}`  # noqa: RUF003
# so the hedge-verb exclusion only applies to the gap BETWEEN the modal verb
# and 止损, never past it — caught in PR review before merge (GitHub PR #75).
#
# The level-breaking pattern similarly went through two iterations: v1 only
# excluded a fixed set of trailing nouns (位/点/价/驱动/盘/单), which still
# flagged "触及止损线" (线 not in the set) and, more importantly, flagged bare
# descriptive sentences with no price at all ("跌破后触发止损" / "价格触及止损").
# v2 (this version) requires a digit to appear between the level verb and
# 止损 — a concrete price/level is the actual signal that distinguishes a
# directive ("跌破91.50止损") from generic market narration, so this is a
# structural fix rather than another entry in the trailing-noun list.
# ---------------------------------------------------------------------------
_ZH_SCAN_REGEX_PATTERNS: tuple[str, ...] = (
    r"(建议|应该|需要|请)(?:(?!关注|留意|注意|警惕)[^。,，、\n]){0,6}止损",  # noqa: RUF001
    r"止损[位点价]",
    r"(立即|立刻|马上|赶紧)[^。,，、\n]{0,2}止损(?!位|点|价|驱动|盘|单)",  # noqa: RUF001
    r"(跌破|涨破|触及|低于|高于)(?=[^。,，、\n]{0,10}\d)[^。,，、\n]{0,10}止损(?!位|点|价|线|驱动|盘|单)",  # noqa: RUF001
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

# Human-readable name for the context-scanned term, for prompt injection only
# (the prompt lists the bare word; the scan itself uses the regex patterns
# above, which only fire on the advisory-directive framing).
_ZH_CONTEXT_SCAN_TERMS: tuple[str, ...] = ("止损",)

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
