"""Tests for app/compliance/output_scan.py (Layer-4 output backstop).

Split out of test_report_generator.py (#37).
"""

from __future__ import annotations

from app.compliance import output_scan as scan
from app.services.i18n_glossary import load_i18n_glossary

# ---------------------------------------------------------------------------
# Tests: _scan_forbidden_output
# ---------------------------------------------------------------------------


def test_scan_forbidden_output_flags_advisory_language() -> None:
    assert scan._scan_forbidden_output("You should buy more AAPL.") == ["should buy"]
    assert scan._scan_forbidden_output("We recommend reducing exposure.")  # non-empty
    assert scan._scan_forbidden_output("Set a stop-loss near 100.")
    assert scan._scan_forbidden_output("止损位在 100。")


def test_scan_forbidden_output_no_false_positives() -> None:
    """Factual prose with substrings of forbidden words must stay clean."""
    clean = (
        "## §3 Holdings Intelligence\n"
        "The company announced a buyback; households increased savings. "
        "AAPL exits the index. Threshold breached. "
        "[For information only — not investment advice]"
    )
    assert scan._scan_forbidden_output(clean) == []


def test_scan_flags_advisory_action_language() -> None:
    # Unambiguously direct advisory/action terms must trip the scan backstop.
    # "target price" / "entry point" / "目标价" / "增持" / "减持" / "入场" are
    # prompt-only (high FP risk in factual news context) — not scanned (issue #65).
    # "止损" is context-scanned (see test_scan_zh_stoploss_* below), not a bare
    # literal, so it is excluded from this generic list.
    for phrase in ("stop-loss", "strong buy", "强烈买入", "投资建议", "清仓"):
        assert scan._scan_forbidden_output(f"set a {phrase} near 100") != [], (
            f"expected scan to flag: {phrase!r}"
        )


def test_scan_zh_stoploss_flags_advisory_directive() -> None:
    # Bare literal "止损" was too broad — it flagged reports that merely
    # describe OTHER market participants' stop-loss orders triggering a
    # sell-off (report 9b61b18e). The scan now only fires when 止损 appears
    # as a directive to the user.
    for phrase in (
        "建议止损",
        "应该止损",
        "止损位在 100",
        "止损点设在95",
        "止损价100",
        "立即止损",
        "马上止损",
        "跌破91.50止损",  # bare imperative directive, no modal verb / 位点价 (issue #74)
        # A first fix tightened the modal-verb gap to 0-2 chars to exclude
        # "应该注意到..." (see next test), which silently broke these two —
        # real advisory phrasing with a 3+ char subject/adverb (issue #74
        # follow-up: negative-lookahead exclusion instead of a gap cut).
        "建议投资者止损",
        "应该马上进行止损",
        # PR review (#75) found the negative-lookahead exclusion above used
        # unbounded `.`, which could see PAST 止损 into a following clause —
        # these multi-clause directives were wrongly excluded until the
        # lookahead was rewritten as a per-character walk confined to the
        # gap between the modal verb and 止损.
        "建议止损。注意风险",
        "建议止损并注意流动性",
        "立刻止损",  # temporal adverb missing from the original set (nit, #75)
        "低于90止损",
        "跌破9150点止损",
    ):
        assert scan._scan_forbidden_output(phrase) != [], f"expected scan to flag: {phrase!r}"


def test_scan_zh_stoploss_allows_market_mechanism_description() -> None:
    # Layer-1/2 factual description of third-party stop-loss orders triggering
    # must not trip the scan (issue triggered by report 9b61b18e).
    for phrase in (
        "这种模式与早盘被迫平仓或止损驱动的抛售一致，随后被买家吸纳",  # noqa: RUF001
        "触发止损盘引发短线抛压",
        "止损单集中涌现",
        "应该注意到止损盘大量涌现",  # Layer-3 "worth watching", not a directive (issue #74)
        "跌破91.50触发止损盘",  # level-break + descriptive noun, not a directive (issue #74)
        "立即触发止损盘",  # temporal adverb + descriptive noun, not a directive (issue #74)
        "建议投资者关注止损盘涌现现象",  # hedge phrasing with a subject, still not a directive
        "马上触发止损单",  # temporal adverb + descriptive noun, longer subject variant
        # PR review (#75): level-breaking pattern originally only excluded a
        # fixed set of trailing nouns, missing "止损线" and, more importantly,
        # bare descriptive sentences with no price at all. Fixed by requiring
        # a digit between the level verb and 止损 (a concrete price is the
        # actual directive signal) rather than growing the exclusion list.
        "触及止损线",
        "跌破止损线",
        "跌破后触发止损",  # no price in the gap — generic market narration
        "价格触及止损",  # no price in the gap — generic market narration
        "立刻触发止损盘",
        "跌破支撑位后触发止损单",
    ):
        assert scan._scan_forbidden_output(phrase) == [], (
            f"scan should not flag descriptive use: {phrase!r}"
        )


def test_scan_allows_high_fp_zh_terms_in_factual_context() -> None:
    # Terms that routinely appear in financial news as third-party descriptions
    # must not trigger the scan backstop (issue #65).
    for phrase in ("目标价", "增持", "减持", "入场"):
        assert scan._scan_forbidden_output(f"机构将{phrase}下调至100") == [], (
            f"scan should not flag descriptive use of: {phrase!r}"
        )


def test_scan_allows_ta_observation_vocabulary() -> None:
    # Descriptive TA terms (where price sits) are observation language, not advice.
    # The Layer-3 prompt and disclaimer cover the advisory boundary — the scan
    # backstop is reserved for direct action/recommendation language only.
    zh_ta_terms = [t["zh-Hans"] for t in load_i18n_glossary().ta_observation_terms.values()]
    for phrase in (
        "support level",
        "resistance level",
        "golden cross",
        "breakout",
        *zh_ta_terms,
    ):
        assert scan._scan_forbidden_output(f"the {phrase} held") == [], (
            f"unexpectedly flagged: {phrase!r}"
        )


def test_scan_allows_descriptive_price_structure() -> None:
    body = (
        "NVDA closed 6% below its 50-day moving average and sits in the lower third "
        "of its 52-week range; 20-day annualized volatility is 42%."
    )
    assert scan._scan_forbidden_output(body) == []


# ---------------------------------------------------------------------------
# Tests: _strip_markers (unit, no DB) — #9: inline tags/citations are removed
# ---------------------------------------------------------------------------


def test_strip_markers_removes_news_citations() -> None:
    text = "The Fed raised rates [S1] and markets reacted [S12]."
    result = scan._strip_markers(text)
    assert "[S1]" not in result
    assert "[S12]" not in result
    assert "新闻" not in result  # no replacement marker is introduced either
    assert "The Fed raised rates and markets reacted." in result


def test_strip_markers_removes_consecutive_citation_run() -> None:
    text = "Markets moved [S6][S7][S8] [S9][S10] sharply."
    result = scan._strip_markers(text)
    assert "S6" not in result and "S10" not in result
    assert "Markets moved sharply." in result


def test_strip_markers_removes_compliance_suffix() -> None:
    text = f"Rates may pressure valuations. {scan._COMPLIANCE_MARKER}"
    result = scan._strip_markers(text)
    assert "For information only" not in result
    assert result.strip() == "Rates may pressure valuations."


def test_strip_markers_removes_provenance_tags() -> None:
    text = "AAPL fell 9% [行情] on weak demand [新闻]; rates may pressure it [分析]."
    result = scan._strip_markers(text)
    for tag in ("[行情]", "[新闻]", "[分析]"):
        assert tag not in result
    assert "AAPL fell 9% on weak demand; rates may pressure it." in result


def test_strip_markers_removes_macro_theme_tag() -> None:
    text = "Cerebras rose [宏观主题数据] on chip-strategy support."
    result = scan._strip_markers(text)
    assert "宏观主题数据" not in result
    assert "Cerebras rose on chip-strategy support." in result


def test_strip_markers_noop_on_clean_text() -> None:
    text = "Rates rose and tech sold off."
    assert scan._strip_markers(text) == text


def test_strip_markers_removes_model_emitted_disclaimer() -> None:
    """The model sometimes appends its own disclaimer paragraph despite the system
    prompt; it must be dropped (the footer owns the single disclaimer) — otherwise
    its '投资建议' / 'investment advice' wording false-trips the compliance scan."""
    body = (
        "## §4 Risk Radar\n\n"
        "USD exposure is 68.7%.\n\n"
        "---\n\n"
        "*本报告仅供信息参考，不构成任何投资建议或买卖指令。This report is for "  # noqa: RUF001
        "informational purposes only and does not constitute investment advice.*"
    )
    result = scan._strip_markers(body)
    assert "投资建议" not in result
    assert "investment advice" not in result
    assert "USD exposure is 68.7%." in result  # real content kept
    assert scan._scan_forbidden_output(result) == []  # no longer trips the scan
    assert not result.rstrip().endswith("---")  # orphaned rule trimmed


def test_strip_body_disclaimer_runs_post_translation() -> None:
    """A disclaimer the translator re-adds (after the pre-translation strip) must
    still be removed by the standalone post-translation pass."""
    translated = (
        "## §4 风险雷达\n\n美元敞口为 68.7%。\n\n---\n\n"
        "*本报告仅供参考，不构成投资建议。*"  # noqa: RUF001
    )
    out = scan._strip_body_disclaimer(translated)
    assert "投资建议" not in out
    assert "美元敞口为 68.7%。" in out
    assert scan._scan_forbidden_output(out) == []
