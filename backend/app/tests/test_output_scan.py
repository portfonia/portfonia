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
    # prompt-only (high FP risk in factual news context) — not scanned (issue #65
    # for zh; issue #443 brought EN target price/entry point in line with it).
    # "止损" / "清仓" / EN "stop-loss" / "reduce exposure" / "increase position" /
    # "strong buy" / "bullish/bearish rating" / "will rise/fall to" are
    # context-scanned (see test_scan_zh_stoploss_*, test_scan_zh_qingcang_*,
    # test_scan_en_stoploss_*, test_scan_en_reduce_exposure_*,
    # test_scan_en_increase_position_*, test_scan_en_strong_buy_*,
    # test_scan_en_rating_*, and test_scan_en_price_forecast_* below), not
    # bare literals. "oversold"/"overbought" are dropped from the scan
    # entirely (issue #443 — objective TA states, same class as the already-
    # unscanned support/resistance level below), so they are intentionally
    # absent from this list. "set a stop-loss" is still a directive,
    # sentence-initial imperative shape.
    for phrase in ("stop-loss", "强烈买入", "投资建议"):
        assert scan._scan_forbidden_output(f"set a {phrase} near 100") != [], (
            f"expected scan to flag: {phrase!r}"
        )


def test_scan_en_entry_point_allows_general_valuation_concept() -> None:
    # Production hold 73d54b62-ac48-4c76-8900-4d6b26898201 (issue #443): the
    # user held 100% cash, and this sentence discusses how the rate
    # environment affects the general concept of a future valuation entry
    # point — no security, price, or timing is named, and nothing directs
    # the reader. Layer-3 observation, not a Layer-4 directive. "entry point"
    # dropped to prompt-only (mirrors zh 入场, issue #65) so this no longer
    # holds the report.
    body = (
        "For an investor sitting in cash, the yield environment is a direct "
        "input to the opportunity cost of capital and the valuation entry "
        "point for any future technology-sector commitments."
    )
    assert scan._scan_forbidden_output(body) == []


def test_scan_en_target_price_allows_factual_third_party_mention() -> None:
    # Mirrors zh 目标价 (issue #65): a bank's published target price is
    # Layer-1/2 factual news, not a directive to the reader.
    body = "The bank lowered its target price for the name to $150 after earnings."
    assert scan._scan_forbidden_output(body) == []


def test_scan_en_reduce_exposure_flags_advisory_directive() -> None:
    # "reduce exposure" moved off the bare-literal list to a directive-context
    # regex (issue #443), mirroring stop-loss/止损 rather than the prompt-only
    # demotion given to target price/entry point — a modal-anchored directive
    # naming urgency ("...should reduce exposure to NVDA now") is a
    # materially specific Layer-4 instruction, same reasoning as #74's 止损.
    for phrase in (
        "Investors should reduce exposure to NVDA now.",
        "You must reduce exposure to the name immediately.",
        "Reduce exposure to the sector before the print.",
        "Consider reducing exposure ahead of the event.",
        # blacktomb42 PR #444 review: a natural possessive pronoun ("your
        # exposure") differs from the fixture above only by that pronoun and
        # was returning [] on the prior HEAD — a real backstop miss, not
        # cosmetic regex coverage.
        "You should reduce your exposure to NVDA now.",
    ):
        assert scan._scan_forbidden_output(phrase) != [], f"expected scan to flag: {phrase!r}"


def test_scan_en_reduce_exposure_allows_third_party_description() -> None:
    # Layer-1/2 factual description of what other market participants did,
    # not a directive to the reader.
    for phrase in (
        "Hedge funds reduced exposure to tech stocks after the rally.",
        "Institutional investors are reducing exposure to growth names amid the pullback.",
        "Reducing exposure to tech in Q3 helped the fund limit losses.",
    ):
        assert scan._scan_forbidden_output(phrase) == [], (
            f"scan should not flag descriptive use: {phrase!r}"
        )


def test_scan_en_increase_position_flags_advisory_directive() -> None:
    # Symmetric with reduce exposure (issue #443) — same directive shape,
    # opposite direction.
    for phrase in (
        "Investors should increase position in the name ahead of earnings.",
        "You must increase your position before the announcement.",
        "Increase your position in the name before the print.",
        "Consider increasing your position ahead of the event.",
        # blacktomb42 PR #444 review: possessive "their position" (vs. "your
        # position" in the fixture above) was returning [] on the prior HEAD.
        "Investors should increase their position in NVDA.",
    ):
        assert scan._scan_forbidden_output(phrase) != [], f"expected scan to flag: {phrase!r}"


def test_scan_en_increase_position_allows_third_party_description() -> None:
    for phrase in (
        "Several funds increased their position in the name after earnings.",
        "Institutional buyers are increasing their position across semiconductor names.",
        "Increasing their position ahead of the print paid off for early buyers.",
    ):
        assert scan._scan_forbidden_output(phrase) == [], (
            f"scan should not flag descriptive use: {phrase!r}"
        )


def test_scan_en_stoploss_flags_advisory_directive() -> None:
    # EN counterpart of test_scan_zh_stoploss_flags_advisory_directive
    # (issue #443) — a directive naming an exact exit mechanism is a more
    # specific Layer-4 instruction than a vague position-size directive, so
    # stop-loss keeps a scan (context-aware), same class as reduce exposure/
    # increase position above, not a prompt-only demotion.
    for phrase in (
        "Set a stop-loss near 100.",
        "You should set a stop-loss at $50.",
        "Consider setting a stop-loss below support.",
        "Investors must set a stop-loss before the open.",
        # blacktomb42 PR #444 review: a nested modal ("should consider
        # setting") was returning [] on the prior HEAD — the modal-anchored
        # branch only recognized exactly one modal token immediately before
        # the verb, not "should" followed by "consider".
        "Investors should consider setting a stop-loss below support.",
    ):
        assert scan._scan_forbidden_output(phrase) != [], f"expected scan to flag: {phrase!r}"


def test_scan_en_stoploss_allows_market_mechanism_description() -> None:
    # Layer-1/2 factual description of third-party stop-loss mechanics
    # triggering, not a directive to the reader.
    for phrase in (
        "Stop-loss orders triggered a broad sell-off.",
        "The fund's stop-loss levels were hit, forcing liquidation.",
        "Analysts noted heavy stop-loss selling below the 50-day average.",
    ):
        assert scan._scan_forbidden_output(phrase) == [], (
            f"scan should not flag descriptive use: {phrase!r}"
        )


def test_scan_en_oversold_overbought_never_scanned() -> None:
    # Issue #443 scope revision: oversold/overbought are objective TA states
    # (an RSI or similar indicator reading), not actions — the same class of
    # Layer-3 "signal worth watching" vocabulary as support/resistance level,
    # which this scan already never flags (see
    # test_scan_allows_ta_observation_vocabulary below). A cited article's
    # own TA read ("the technical desk views the name as oversold") is
    # exactly the false-positive shape this issue exists to fix, so both
    # terms are dropped from the scan entirely — dropped, not
    # context-narrowed, since there is no directive-shaped use of a bare
    # state adjective the way there is for a verb like "recommend" or
    # "reduce exposure". They remain in the prompt blacklist (the model is
    # still told not to use them in its own voice).
    for phrase in (
        "The stock is deeply oversold after the pullback.",
        "RSI shows the name is overbought at these levels.",
        "Analysts view the sector as oversold heading into earnings.",
        "You should buy — it's oversold.",  # "should buy" still holds this
    ):
        result = scan._scan_forbidden_output(phrase)
        assert "oversold" not in [r.lower() for r in result]
        assert "overbought" not in [r.lower() for r in result]


def test_scan_en_strong_buy_and_rating_flags_first_person_assertion() -> None:
    # Issue #443: "strong buy"/"bullish rating"/"bearish rating" are ratings,
    # not states — closer to recommend* (#375) than to oversold/overbought.
    # Block only the model's own first-person/product voice or a bare
    # sentence-initial declarative.
    for phrase in (
        "We rate this a strong buy.",
        "This is a strong buy given the setup.",
        "Portfonia views the name with a bullish rating.",
        "This carries a bearish rating in our view.",
        # blacktomb42 PR #444 review: the prior HEAD used a bare "we/i/
        # portfonia" token as a proxy for "this is the grammatical subject
        # of the rating" — the possessive "our" (no bare "we"/"i" token) was
        # returning [] even though it is clearly the model's own voice.
        "In our view, this is a strong buy.",
        "Our base case is a bullish rating on the name.",
        # A Markdown list item is still a sentence for scanning purposes —
        # the prior HEAD's sentence-start anchor didn't recognize a leading
        # "- " bullet marker.
        "- This is a strong buy given the setup.",
        # blacktomb42 PR #444 re-review (round 3): a reporting-verb frame is
        # not itself proof of third-party content — "we note/report THAT"
        # can just as easily wrap the model's own unattributed claim about
        # the instrument. Only an ATTRIBUTION_VERB (has/expects/believes/
        # maintains/says/holds/sets/gives) applied to some OTHER subject
        # before the phrase is real evidence of a third party.
        "We note that NVDA is a strong buy.",
        # A named instrument (not just "this"/"it"/"the stock") is still a
        # bare, unattributed sentence-initial declarative.
        "NVDA is a strong buy.",
        "NVDA carries a bullish rating.",
        # Nested/indented Markdown list item (0-3 leading spaces is the
        # common nesting range).
        "  - This is a strong buy.",
        # blacktomb42 PR #444 re-review (round 4): "at least one intervening
        # word" is not proof of a subject change — an adverb between the
        # pronoun and the attribution verb ("we STRONGLY expect", "Portfonia
        # CURRENTLY gives") still leaves the pronoun as that verb's own
        # subject. Likewise "our TEAM believes"/"our ANALYSIS says" are
        # still Portfonia's own team/analysis, not a separate third party.
        "We currently maintain a strong buy rating on NVDA.",
        "Portfonia currently gives NVDA a strong buy rating.",
        # blacktomb42 PR #444 re-review (round 5): round 4's closed
        # "_SELF_REFERENT_WORDS" blacklist can never enumerate every
        # modifier/own-referent noun — replaced with a positive structural
        # signal (a "that"-clause or "according to X," frame) instead of
        # trying to list every non-third-party filler word.
        "We cautiously maintain a strong buy rating on NVDA.",
        "My analysis says NVDA carries a bullish rating.",
        "This report says NVDA carries a bullish rating.",
    ):
        assert scan._scan_forbidden_output(phrase) != [], f"expected scan to flag: {phrase!r}"


def test_scan_en_strong_buy_and_rating_allows_third_party_attribution() -> None:
    # A cited article's report of a bank/analyst rating is Layer-1/2
    # quotation, not the model's own advice — this is the exact
    # false-positive shape reported by the product owner (2026-09-12): cited
    # articles routinely carry this vocabulary as background.
    for phrase in (
        "The bank has a strong buy rating on the stock.",
        "Analysts maintain a strong buy rating heading into the print.",
        "Morgan Stanley's bullish rating on the name reflects its AI capex thesis.",
        "The desk's bearish rating on the sector predates the guidance cut.",
        # blacktomb42 PR #444 review: the prior HEAD's window treated any
        # "we"/"i"/"portfonia" occurrence as if it were the rating's
        # grammatical subject, regardless of what verb immediately follows —
        # "we note/report that X" introduces third-party content and must
        # not hold the report just because "we" appears nearby.
        "We note that UBS has a strong buy rating on NVDA.",
        # blacktomb42 PR #444 re-review (round 3): "According to X, ..." is
        # third-party attribution even with no "note"/"report" reporting
        # verb at all — the anchor exclusion must key on an ATTRIBUTION_VERB
        # appearing before the phrase, not on a specific reporting-verb
        # wrapper.
        "According to our source, UBS has a strong buy rating on NVDA.",
        # blacktomb42 PR #444 re-review (round 4): an explicit third-party
        # possessive rating/view must not be recreated as a false hold via
        # the sentence-initial branch — "X's rating IS a strong buy" names
        # X as the party HOLDING the rating, not the model's own claim.
        "UBS's rating remains a strong buy.",
        "Morningstar's view is a strong buy.",
        # blacktomb42 PR #444 re-review (round 5): the possessive "'s" is
        # not required for a third-party attribution noun phrase, and the
        # attribution-noun vocabulary was too narrow (missing
        # recommendation/call). ACCEPTED RESIDUAL (documented, matches the
        # reviewer's own suggested fallback): this cannot distinguish "UBS's
        # rating" (a source) from "NVDA's rating" (the instrument itself)
        # without portfolio context this pure-text scanner doesn't have —
        # both read as third-party-shaped and are allowed. See
        # forbidden_vocab.py's `_THIRD_PARTY_POSSESSIVE` docstring.
        "UBS rating remains a strong buy.",
        "UBS view is a strong buy.",
        "UBS's recommendation remains a strong buy.",
        "UBS's call remains a strong buy.",
    ):
        assert scan._scan_forbidden_output(phrase) == [], (
            f"scan should not flag third-party attribution: {phrase!r}"
        )


def test_scan_en_price_forecast_flags_unattributed_assertion() -> None:
    # Issue #443: same third-party-attribution technique as recommend*/strong
    # buy above, applied to "will rise/fall to".
    for phrase in (
        "This will rise to $200 by year-end.",
        "We expect it will fall to $80 on weaker guidance.",
        # blacktomb42 PR #444 review: possessive "our" (no bare "we"/"i")
        # returned [] on the prior HEAD despite being the model's own claim.
        "Our base case is that the stock will rise to $200.",
        # Markdown list item — see the same fixture on the rating test above.
        "- The stock will fall to $80.",
        # blacktomb42 PR #444 re-review (round 3): reporting-verb frame with
        # no attribution verb — see the rating test's equivalent fixture.
        "We report that the stock will rise to $200.",
        # Named instrument, no third-party attribution.
        "NVDA will rise to $200.",
        "- NVDA will fall to $80.",
        # blacktomb42 PR #444 re-review (round 4): adverb/self-referent-noun
        # gap — see the rating test's equivalent fixtures.
        "We strongly expect it will fall to $80.",
        "We now believe NVDA will rise to $200.",
        "Our team believes NVDA will rise to $200.",
        "Our analysis says NVDA will rise to $200.",
        # blacktomb42 PR #444 re-review (round 5): every one of these is a
        # direct first-person/product assertion with no third party named
        # anywhere — round 4's closed adverb/noun blacklist couldn't
        # enumerate all of them ("cautiously"/"fully"/"would" (a modal
        # auxiliary, not even an adverb)/"personally"/"research"/
        # "committee"), and "my"/"this report" weren't even recognized as
        # anchors at all.
        "We cautiously expect NVDA will rise to $200.",
        "We fully expect NVDA will rise to $200.",
        "We would expect NVDA will rise to $200.",
        "I personally believe NVDA will rise to $200.",
        "Our research believes NVDA will rise to $200.",
        "Our committee believes NVDA will rise to $200.",
        "My analysis says NVDA will rise to $200.",
        "This report says NVDA will rise to $200.",
    ):
        assert scan._scan_forbidden_output(phrase) != [], f"expected scan to flag: {phrase!r}"


def test_scan_en_price_forecast_allows_third_party_attribution() -> None:
    for phrase in (
        "UBS expects the stock will rise to $200 by year-end.",
        "Analysts believe the name will fall to $80 on weaker guidance.",
        # blacktomb42 PR #444 review: reporting-verb exclusion (see the
        # rating test above) applied to the forecast pattern too.
        "We report that UBS expects the stock will rise to $200.",
    ):
        assert scan._scan_forbidden_output(phrase) == [], (
            f"scan should not flag third-party attribution: {phrase!r}"
        )


def test_scan_en_recommend_flags_user_directed_advisory() -> None:
    # Bare EN recommend* used to match any inflection (#375 production hold
    # 2bff4c7e). The scan now only fires on first-person / product-to-user
    # framing, or a sentence-initial "recommend buying/selling/holding/reducing"
    # directed at the reader. Prompt-side PROMPT_VOCAB_STRING still lists
    # "recommend"; this is the output backstop only.
    for phrase in (
        "We recommend reducing exposure.",
        "I recommend you sell AAPL.",
        "Portfonia recommends a smaller allocation to the name.",
        "Recommend buying AAPL.",
        "It is recommended that you hold the position.",
        # Cross-family regression (blacktomb42 PR #444 review round 2): a
        # Markdown list item is still a sentence for scanning purposes.
        # recommend*'s sentence-initial branch shares `_SENTENCE_START` with
        # the action-directive and rating/forecast patterns below — this
        # fixture and the "- This is a strong buy..."/"- The stock will fall
        # to $80." fixtures on those tests exercise the SAME shared
        # primitive so a future regression there fails all three families
        # together, not silently in just one.
        "- Recommend buying AAPL before the print.",
    ):
        assert scan._scan_forbidden_output(phrase) != [], (
            f"expected scan to flag user-directed recommend: {phrase!r}"
        )


def test_scan_en_recommend_allows_third_party_attribution() -> None:
    # Layer-1/2 quotation of a named house view / bank / desk is not a
    # directive to the Portfonia user. Incident sentence from report
    # 2bff4c7e-99ec-4f6b-98bf-4a7c9876cc47 (issue #375) is the allow fixture.
    # Accepted residual: a user-directed recommend that mimics third-party
    # syntax in the same clause may slip — prefer that over holding bank-view
    # attribution.
    for phrase in (
        "The UBS house view material explicitly recommends building exposure "
        "in high-quality bonds with maturities of two to five years.",
        "The bank recommends a shorter duration sleeve in its published outlook.",
        "Analysts recommend watching the 2-year/10-year spread as the next observable.",
        "The filing restates the board's proxy recommendations for the AGM.",
    ):
        assert scan._scan_forbidden_output(phrase) == [], (
            f"scan should not flag third-party attribution: {phrase!r}"
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


def test_scan_zh_qingcang_flags_advisory_directive() -> None:
    # "liquidate the whole position" aimed at the reader is as unambiguous a
    # Layer-4 instruction as this vocabulary gets, so 清仓 stays on the scan
    # backstop (issue #205) — unlike 目标价/增持, which dropped to prompt-only.
    # Same modal/temporal shape as 止损 (issue #74): the gap walk is the v3
    # per-character form so a hedge verb after 清仓 cannot exclude a real
    # directive ("建议清仓。注意风险").
    for phrase in (
        "建议清仓该持仓，锁定利润",  # noqa: RUF001
        "应该立即清仓XX",
        "建议清仓",
        "应该清仓",
        "立即清仓",
        "马上清仓",
        "建议投资者清仓",
        "应该马上进行清仓",
        "建议清仓。注意风险",
        "建议清仓并注意流动性",
        "立刻清仓",
        "需要清仓",
        "请清仓",
        # Compensating nets (PR #206 review): 止损 kept 位点价 / level-break
        # after leaving scan_terms; 清仓 needs the same class of coverage
        # for non-modal directives. Not a bare 清仓 match — "清仓该持仓" and
        # "分批清仓" stay unscanned because they are the #205 production shape.
        "尽快清仓",
        "全部清仓，不要犹豫",  # noqa: RUF001
        "直接清仓",
        "跌破90就清仓",
        "立即把手里的仓位清仓",
    ):
        assert scan._scan_forbidden_output(phrase) != [], f"expected scan to flag: {phrase!r}"


def test_scan_zh_qingcang_allows_third_party_description() -> None:
    # Layer-1/2 factual description of a named fund's own disclosed position
    # change must not trip the scan. Reproduced in production on report
    # 2aee4d47-4932-40c1-8519-812228740a49 (issue #205): both sentences
    # below were held needs_review for the bare literal 清仓.
    for phrase in (
        "另一份备案文件报告指出，斯坦利·德鲁肯米勒旗下的 Duquesne Family Office "  # noqa: RUF001
        "在最近一个季度完全清仓了英特尔头寸，同时减持了博通和美光，并轮动至其他 "  # noqa: RUF001
        "AI 基础设施类股票。",
        "INTC — 下跌 15.68%，受 Duquesne Family Office 披露清仓该持仓以及"  # noqa: RUF001
        "英伟达财报前半导体板块轮动压力影响",
        "斯坦利·德鲁肯米勒旗下的 Duquesne Family Office 在最近一个季度完全清仓了英特尔头寸",
        "受 Duquesne Family Office 披露清仓该持仓影响",
        "该机构清仓了英特尔头寸",
        "基金披露清仓该持仓",
        # Hedge verb in the 0-6 modal gap (v3 walk): Layer-3 "worth watching",
        # not a directive. A 0-2 gap cut would also spare this, but would
        # drop "建议投资者清仓"; the unbounded-`.` lookahead would spare
        # "建议清仓。注意风险" (block list) by seeing 注意 past 清仓.
        "建议关注该机构清仓动作",
        "建议投资者关注清仓",
        # Temporal adverb + disclosure verb: third-party filing, not "立即清仓".
        "立即披露清仓该持仓",
        "立刻宣布清仓",
        # Forced / process liquidation (PR #206 review): temporal arm must
        # not recreate the #205 hold on Layer-1/2 market narration.
        "多头被迫立即清仓离场",
        "杠杆资金被迫马上清仓",
        "该ETF立刻启动清仓程序",
        "公司马上公告清仓计划",
        # Accepted residual vs a bare-literal backstop (same #205 shape):
        "分批清仓",
        "清仓该持仓",
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


def test_scan_third_party_possessive_residual_does_not_distinguish_instrument() -> None:
    """Documented accepted residual (blacktomb42 PR #444 round-5 review): a
    pure-text scanner cannot tell "UBS's rating" (a source) from "NVDA's
    rating" (the instrument itself, i.e. the model's own claim) apart
    without knowing which names are portfolio holdings — that requires
    integrating this scan with the report's holdings list, out of scope for
    this fix. Both are treated as third-party-shaped and allowed; see
    `forbidden_vocab.py`'s `_THIRD_PARTY_POSSESSIVE` docstring."""
    assert scan._scan_forbidden_output("NVDA's rating remains a strong buy.") == []


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
