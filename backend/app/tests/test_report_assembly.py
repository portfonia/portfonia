"""Second-layer personalized assembly (issue #128 A4, design doc §6)."""

from __future__ import annotations

import inspect
from typing import Any

from app.services import report_assembly as ra

_PORTFOLIO: dict[str, Any] = {
    "base_currency": "USD",
    "total_base": 500000.0,
    "fx_date": "2026-08-17",
    "holdings": [
        {
            "name": "NVIDIA",
            "ticker": "NVDA",
            "currency": "USD",
            "market_value": 300000.0,
            "market_value_base": 300000.0,
            "asset_class": "STOCK",
        },
        {
            "name": "Vanguard S&P 500",
            "ticker": "VOO",
            "currency": "USD",
            "market_value": 200000.0,
            "market_value_base": 200000.0,
            "asset_class": "EQUITY_US_BROAD",
        },
    ],
    "by_asset_class": {"STOCK": 300000.0, "EQUITY_US_BROAD": 200000.0},
    "by_currency": {"USD": 500000.0},
    "by_market": {"US": 500000.0},
    "concentration": {
        "single_holding_watch": True,
        "single_holding_high": False,
        "top_holding_name": "NVIDIA",
        "top_holding_asset_class": "STOCK",
        "top_holding_ratio": 0.6,
    },
}

_ANOMALIES: list[dict[str, Any]] = [
    {
        "identifier": "NVDA",
        "name": "NVIDIA",
        "market": "US",
        "asset_type": "stock",
        "window_net_pct": 0.0812,
        "latest_date": "2026-08-17",
    }
]

_TICKER_INTEL = {"NVDA": "NVDA rose 8.1% on a confirmed earnings beat. [Established]"}
_MACRO_INTEL = {
    "theme:monetary_policy": {
        "analysis": "The 30-year yield reached 5.31%, pressuring equity valuations.",
        "affected_asset_classes": ["EQUITY_US_BROAD", "BOND_FUND"],
        "affected_sectors": ["Technology"],
    }
}
_EXPOSURE = {"theme:monetary_policy": ["EQUITY_US_BROAD"]}


def _prompt(**overrides: Any) -> str:
    kwargs: dict[str, Any] = {
        "portfolio": _PORTFOLIO,
        "price_anomalies": _ANOMALIES,
        "ticker_intel": _TICKER_INTEL,
        "macro_event_intel": _MACRO_INTEL,
        "macro_event_exposure": _EXPOSURE,
        "period_start": "2026-08-14T21:00:00+00:00",
        "period_end": "2026-08-17T21:00:00+00:00",
        "trading_days": 2,
    }
    kwargs.update(overrides)
    return ra.build_assembly_prompt(**kwargs)


# ---------------------------------------------------------------------------
# The type boundary (design doc §4.8 / §5.7 hand-off item 2)
# ---------------------------------------------------------------------------


def test_build_assembly_prompt_cannot_reach_the_database_at_all() -> None:
    """A4's leak risk is the inverse of A2/A3's: it does not WRITE a shared
    cache, it READS two of them and mixes in per-user holdings. The failure
    mode is therefore assembling another user's shared-cache rows into this
    user's report — exactly design doc §1.3's cross-user leak in a new place.

    The boundary that makes that impossible is the absence of a `Session`
    parameter: with no DB handle, this function can only ever see the values
    its per-user caller hands it (`ctx.ticker_intel` / `ctx.macro_event_intel`,
    both already scoped by `l1_identifiers_for_user` /
    `l2_event_keys_for_user`). It cannot query `ticker_intel` for "everything
    cached today" even by mistake. Same trick as `build_l2_facts`, inverted.
    """
    params = inspect.signature(ra.build_assembly_prompt).parameters
    assert "session" not in params
    annotations = [str(p.annotation) for p in params.values()]
    assert not any("Session" in a for a in annotations)


def test_assembly_module_never_imports_the_shared_cache_models() -> None:
    """Belt-and-braces on the same boundary: no ORM model import means no
    path to a query, however the module is later edited."""
    source = inspect.getsource(ra)
    assert "from app.models" not in source
    assert "TickerIntel" not in source
    assert "MacroEventIntel" not in source


def test_prompt_carries_only_the_identifiers_it_was_given() -> None:
    """Another user's holding must not appear merely because it shares the
    day's cache."""
    prompt = _prompt()
    assert "NVDA" in prompt
    assert "TSLA" not in prompt


# ---------------------------------------------------------------------------
# Assembly inputs (design doc §6.3)
# ---------------------------------------------------------------------------


def test_prompt_carries_the_precomputed_shared_ticker_intel() -> None:
    assert "confirmed earnings beat" in _prompt()


def test_prompt_carries_shared_macro_intel_with_this_users_exposure() -> None:
    prompt = _prompt()
    assert "30-year yield reached 5.31%" in prompt
    # The per-user half of L2: which of the affected classes this user holds.
    assert "EQUITY_US_BROAD" in prompt


def test_prompt_carries_per_user_portfolio_weights() -> None:
    """The personalization half — weights and concentration are what make an
    assembled report this user's rather than a generic digest."""
    prompt = _prompt()
    assert "NVIDIA" in prompt
    assert "60.0%" in prompt or "60%" in prompt


def test_prompt_orders_holdings_by_weight_descending() -> None:
    prompt = _prompt()
    assert prompt.index("NVIDIA") < prompt.index("Vanguard S&P 500")


def test_prompt_includes_code_built_transmission_for_exposed_classes() -> None:
    """L2 analysis alone is not enough for assembly to trace a rate move
    onto TSMC. The mechanism labels are code-built from a closed map, not
    free-text from the model."""
    prompt = _prompt()
    assert "TRANSMISSION" in prompt
    assert "discount_rate" in prompt


def test_prompt_includes_technical_positions_when_supplied() -> None:
    prompt = _prompt(
        technical_positions=[
            {
                "ticker": "NVDA",
                "pct_vs_sma50": 0.12,
                "pct_vs_sma200": 0.4,
                "pct_in_52w_range": 0.81,
            }
        ]
    )
    assert "TECHNICAL POSITION" in prompt
    assert "NVDA" in prompt
    assert "50" in prompt


def test_prompt_tells_the_model_to_connect_supplied_facts_not_invent() -> None:
    prompt = _prompt()
    assert "connect" in prompt.lower()
    assert "do not introduce" in prompt.lower() or "must not invent" in prompt.lower()


def test_prompt_omits_raw_news_and_search_results() -> None:
    """Where the cost reduction actually comes from (design doc §6.3): the
    raw corpus was already digested into L1/L2, so the assembly pass never
    re-sends it. A signature carrying them back would silently restore the
    Pass 2 token profile."""
    params = inspect.signature(ra.build_assembly_prompt).parameters
    assert "news_items" not in params
    assert "search_results" not in params


def test_prompt_forbids_introducing_facts_beyond_the_supplied_intel() -> None:
    """Design doc §6.4 risk: the assembly pass re-deriving its own analysis
    would defeat the whole architecture (and spend the tokens twice)."""
    assert "do not introduce" in ra._build_assembly_system().lower()


def test_assembly_system_prompt_keeps_every_shared_narrative_rule() -> None:
    """These rules are compliance-adjacent — a body pass that lost DIRECTION
    REQUIRES EVIDENCE would assert price direction with no window data."""
    system = ra._build_assembly_system()
    for marker in (
        "MANDATORY COMPLIANCE",
        "FORWARD EVENTS:",
        "DIRECTION REQUIRES EVIDENCE:",
        "DIVERGENCE IS THE SIGNAL:",
        "§4.2 CROSS-REFERENCES:",
    ):
        assert marker in system, f"assembly system prompt lost: {marker}"


def test_assembly_system_prompt_never_mentions_large_holdings_window_price() -> None:
    """PR #168 round 2 review, suggestion: `build_assembly_prompt` never
    renders a LARGE HOLDINGS WINDOW PRICE data block (no `large_holding_moves`
    parameter at all — see this module's docstring), but the shared narrative
    rules (DIRECTION REQUIRES EVIDENCE, NAMING IS NOT ANALYSIS) used to
    reference it verbatim as a grounding source / "check below" pointer
    regardless of which pass composed them. A dangling reference to data that
    is never actually present would tell the assembly model to check a
    section that does not exist."""
    assert "LARGE HOLDINGS WINDOW PRICE" not in ra._build_assembly_system()


def test_assembly_prompt_section2_instructs_selection_not_mechanical_coverage() -> None:
    """2026-08-21 §2 rewrite, assembly's own inline instruction block (issue
    #128 Ring 1 stage B / B1 PR follow-up) — same rewrite as Pass 2's
    report_prompts._SECTION2_INSTRUCTIONS: select a few genuinely-changed
    events into flowing paragraphs, not mechanical bulleted coverage of
    every supplied one."""
    prompt = _prompt()
    assert "2 to 4" in prompt
    assert "Impact on this portfolio" not in prompt
    for label in ("short-term (this period", "medium-term (weeks to a quarter)"):
        assert label not in prompt


def test_assembly_prompt_section2_no_mapping_means_no_standalone_paragraph() -> None:
    """Same 2026-08-22 tightening as report_prompts._SECTION2_INSTRUCTIONS —
    assembly's own inline §2 block gets the matching no-direct-mapping
    rule."""
    prompt = " ".join(_prompt().split())
    assert "no direct, concrete mapping to a holding" in prompt
    assert "does not earn its own paragraph by default" in prompt


def test_assembly_prompt_requests_the_same_section_markers_pass2_emits() -> None:
    """The assembled body is a drop-in replacement for Pass 2's: the same
    `_render_full_md` injects §2.5/§4.2/§4.4 into it and the same
    completeness guard checks it."""
    prompt = _prompt()
    assert "§2" in prompt
    assert "§3" in prompt
    assert "§4" in prompt


# ---------------------------------------------------------------------------
# Enablement + degradation (design doc §6.3 "冷启动与降级", §6.5)
# ---------------------------------------------------------------------------


def test_disabled_switch_never_assembles() -> None:
    assert not ra.should_use_assembly(
        enabled=False,
        model="some/model",
        ticker_intel=_TICKER_INTEL,
        macro_event_intel=_MACRO_INTEL,
    )


def test_enabled_with_intel_assembles() -> None:
    assert ra.should_use_assembly(
        enabled=True,
        model="some/model",
        ticker_intel=_TICKER_INTEL,
        macro_event_intel=_MACRO_INTEL,
    )


def test_cold_cache_falls_back_rather_than_assembling_from_nothing() -> None:
    """Design doc §6.3: with both shared caches empty (cold start, day's cap
    exhausted, every candidate compliance-blocked) there is nothing to
    assemble — the run must degrade to Pass 2, not emit a hollow report."""
    assert not ra.should_use_assembly(
        enabled=True, model="some/model", ticker_intel={}, macro_event_intel={}
    )


def test_partial_intel_still_assembles() -> None:
    """Only one of the two layers having content is a normal day (a quiet
    macro day, or holdings that all sat still), not a degraded one."""
    assert ra.should_use_assembly(
        enabled=True, model="m", ticker_intel=_TICKER_INTEL, macro_event_intel={}
    )
    assert ra.should_use_assembly(
        enabled=True, model="m", ticker_intel={}, macro_event_intel=_MACRO_INTEL
    )


def test_unset_model_falls_back_instead_of_guessing_one() -> None:
    """ASSEMBLY_LLM_MODEL is an output of the shadow comparison. Enabling the
    switch before that decision is made must not silently pick a model."""
    assert not ra.should_use_assembly(
        enabled=True,
        model="   ",
        ticker_intel=_TICKER_INTEL,
        macro_event_intel=_MACRO_INTEL,
    )


# ---------------------------------------------------------------------------
# Shadow comparison harness (design doc §6.3.1)
# ---------------------------------------------------------------------------


def test_shadow_models_parse_from_a_comma_separated_setting() -> None:
    assert ra.parse_shadow_models("a/one, b/two") == ["a/one", "b/two"]


def test_shadow_models_empty_setting_disables_the_harness() -> None:
    assert ra.parse_shadow_models("") == []
    assert ra.parse_shadow_models("  ,  ") == []


def test_shadow_models_deduplicate_while_keeping_order() -> None:
    """A duplicated entry would otherwise bill the same comparison twice."""
    assert ra.parse_shadow_models("a/one, b/two, a/one") == ["a/one", "b/two"]


# ---------------------------------------------------------------------------
# Fund-only holdings (no `ticker`, only `fund_code`) — review round-1 finding,
# PR #163: a fund row previously carried no identifier in the Holdings
# listing and fell out of the L1 weight lookup entirely (matched on `ticker`
# only), silently sorting to the bottom of the L1 block regardless of size.
# ---------------------------------------------------------------------------

_FUND_PORTFOLIO: dict[str, Any] = {
    "base_currency": "USD",
    "total_base": 400000.0,
    "fx_date": "2026-08-17",
    "holdings": [
        {
            "name": "Offshore Fund",
            "fund_code": "110011",
            "currency": "CNY",
            "market_value": 300000.0,
            "market_value_base": 300000.0,
            "asset_class": "EQUITY_CN",
        },
        {
            "name": "NVIDIA",
            "ticker": "NVDA",
            "currency": "USD",
            "market_value": 100000.0,
            "market_value_base": 100000.0,
            "asset_class": "STOCK",
        },
    ],
    "by_asset_class": {"EQUITY_CN": 300000.0, "STOCK": 100000.0},
    "by_currency": {"CNY": 300000.0, "USD": 100000.0},
    "by_market": {},
    "concentration": {},
}


def test_holdings_listing_shows_the_fund_code_for_a_ticker_less_row() -> None:
    prompt = ra.build_assembly_prompt(
        portfolio=_FUND_PORTFOLIO,
        price_anomalies=[],
        ticker_intel={},
        macro_event_intel={},
        macro_event_exposure={},
    )
    assert "(110011)" in prompt


def test_fund_row_is_ordered_by_its_real_weight_in_the_l1_block() -> None:
    """The fund is 75% of the portfolio and has an L1 entry — it must sort
    ahead of NVDA (25%), not fall to the bottom for lack of a `ticker` key."""
    prompt = ra.build_assembly_prompt(
        portfolio=_FUND_PORTFOLIO,
        price_anomalies=[],
        ticker_intel={
            "110011": "The fund's NAV rose on broad CN equity strength. [Established]",
            "NVDA": "NVDA rose on an earnings beat. [Established]",
        },
        macro_event_intel={},
        macro_event_exposure={},
    )
    l1_section = prompt.split("=== SHARED TICKER INTEL")[1]
    assert l1_section.index("110011") < l1_section.index("NVDA")


# ---------------------------------------------------------------------------
# Stale-ticker callout — review round-1 finding, PR #163: Pass 2's prompt
# tells the model which holdings have no price data and are excluded from
# valuations (`_build_pass2_prompt`'s "Stale/no-price identifiers" block);
# the assembly prompt omitted it, so the model could not know a holding
# named in the portfolio was silently missing from every total.
# ---------------------------------------------------------------------------


def test_prompt_carries_stale_ticker_callouts_when_present() -> None:
    portfolio = {**_PORTFOLIO, "stale_tickers": ["TSLA"]}
    prompt = _prompt(portfolio=portfolio)
    assert "Stale/no-price identifiers" in prompt
    assert "TSLA" in prompt


def test_prompt_omits_the_stale_ticker_section_when_empty() -> None:
    """No stale tickers -> no empty/misleading section header."""
    prompt = _prompt()
    assert "Stale/no-price identifiers" not in prompt


# ---------------------------------------------------------------------------
# Report window rendering — review round-1 finding, PR #163: Pass 2's prompt
# converts the stored UTC window to ET and labels it explicitly
# (`_build_pass2_prompt`: `.astimezone(ET).strftime(...)` + "ET" suffix), so
# the model's date/time references match the report's own stated timezone.
# The assembly prompt printed the raw UTC ISO strings verbatim instead.
# ---------------------------------------------------------------------------


def test_report_window_is_rendered_in_et_like_pass2() -> None:
    prompt = _prompt(
        period_start="2026-08-14T21:00:00+00:00",  # 17:00 ET
        period_end="2026-08-17T21:00:00+00:00",  # 17:00 ET
    )
    window_line = prompt.splitlines()[1]
    assert "2026-08-14 17:00" in window_line
    assert "2026-08-17 17:00" in window_line
    assert "ET" in window_line
    assert "+00:00" not in window_line


# ---------------------------------------------------------------------------
# L2 event ordering — review round-2 finding, PR #163: events were sorted by
# how MANY asset classes they overlap (`len(macro_event_exposure[k])`), not
# by how much of the portfolio those classes actually weigh — inverting the
# "lead by weight and exposure" personalization the module's own docstring
# and system prompt both promise.
# ---------------------------------------------------------------------------

_WEIGHTED_PORTFOLIO: dict[str, Any] = {
    "base_currency": "USD",
    "total_base": 500000.0,
    "fx_date": "2026-08-17",
    "holdings": [],
    "by_asset_class": {"BIG": 400000.0, "SMALL_A": 50000.0, "SMALL_B": 50000.0},
    "by_currency": {},
}


def test_l2_events_are_ordered_by_portfolio_weight_not_class_count() -> None:
    """One event overlapping two tiny classes (20% combined) must not
    outrank an event overlapping a single class that is 80% of the book —
    a count-based sort would put the two-class event first; a weight-based
    sort must put the single-class event first."""
    macro_event_intel = {
        "theme:two_small": {"analysis": "TWO_SMALL touches SMALL_A and SMALL_B."},
        "theme:one_big": {"analysis": "ONE_BIG touches BIG."},
    }
    macro_event_exposure = {
        "theme:two_small": ["SMALL_A", "SMALL_B"],
        "theme:one_big": ["BIG"],
    }
    prompt = ra.build_assembly_prompt(
        portfolio=_WEIGHTED_PORTFOLIO,
        price_anomalies=[],
        ticker_intel={},
        macro_event_intel=macro_event_intel,
        macro_event_exposure=macro_event_exposure,
    )
    assert prompt.index("theme:one_big") < prompt.index("theme:two_small")


# ---------------------------------------------------------------------------
# fx_date — review round-2 finding, PR #163: the assembly prompt asks the
# model to write §4.3 FX exposure but never supplies `fx_date`, the only FX
# fact the portfolio snapshot carries. Pass 2's header
# (`_build_pass2_prompt`) includes it.
# ---------------------------------------------------------------------------


def test_prompt_includes_fx_date_for_section_4_3() -> None:
    prompt = _prompt()
    portfolio_header = prompt.splitlines()[3]
    assert "=== PORTFOLIO" in portfolio_header
    assert "2026-08-17" in portfolio_header


# ---------------------------------------------------------------------------
# Holdings listing must print the SAME key the L1 block is keyed under —
# review round-2 nit, PR #163: the listing printed the raw `ticker`/
# `fund_code` value, while the L1 weight lookup and L1 block both key via
# `_identifier()` (`_normalize_hk_ticker(...).upper()`'d). A raw HK ticker
# stored as "700.HK" would print one spelling while L1 keys everything
# "0700.HK" — the model has no way to connect the two, the same join
# failure the fund_code fix (round 1) was for.
# ---------------------------------------------------------------------------

_HK_PORTFOLIO: dict[str, Any] = {
    "base_currency": "HKD",
    "total_base": 100000.0,
    "fx_date": "2026-08-17",
    "holdings": [
        {
            "name": "Tencent",
            "ticker": "700.HK",  # raw, un-normalized form
            "currency": "HKD",
            "market_value": 100000.0,
            "market_value_base": 100000.0,
            "asset_class": "EQUITY_CN",
        }
    ],
    "by_asset_class": {"EQUITY_CN": 100000.0},
    "by_currency": {"HKD": 100000.0},
}


def test_holdings_listing_prints_the_normalized_identifier_l1_is_keyed_under() -> None:
    prompt = ra.build_assembly_prompt(
        portfolio=_HK_PORTFOLIO,
        price_anomalies=[],
        ticker_intel={"0700.HK": "Tencent moved on regulatory news. [Established]"},
        macro_event_intel={},
        macro_event_exposure={},
    )
    holdings_section = prompt.split("Holdings, largest first")[1].split("===")[0]
    assert "(0700.HK)" in holdings_section
    assert "(700.HK)" not in holdings_section


_PSH_PORTFOLIO: dict[str, Any] = {
    "base_currency": "GBP",
    "total_base": 59000.0,
    "fx_date": "2026-08-28",
    "holdings": [
        {
            "name": "Pershing Square Holdings",
            "ticker": "PSH",  # raw form, as stored on Holding
            "currency": "GBP",
            "market_value": 59000.0,
            "market_value_base": 59000.0,
            "asset_class": "STOCK",
        }
    ],
    "by_asset_class": {"STOCK": 59000.0},
    "by_currency": {"GBP": 59000.0},
}


def test_holdings_listing_normalizes_known_collision_ticker_to_l1_key() -> None:
    """issue #204 PR #253 review: PSH must print under 'PSH.L' — the same
    identifier compute_global_moves/select_user_anomalies and the L1 block
    key it under — not the raw 'PSH' stored on the holding."""
    prompt = ra.build_assembly_prompt(
        portfolio=_PSH_PORTFOLIO,
        price_anomalies=[],
        ticker_intel={"PSH.L": "Pershing Square moved on NAV discount news. [Established]"},
        macro_event_intel={},
        macro_event_exposure={},
    )
    holdings_section = prompt.split("Holdings, largest first")[1].split("===")[0]
    assert "(PSH.L)" in holdings_section
    assert "(PSH)" not in holdings_section


# ---------------------------------------------------------------------------
# L3 cross-name clusters in the prompt (issue #128 quality gate, §6.7 item 1)
# ---------------------------------------------------------------------------


_CLUSTERS: list[dict[str, Any]] = [
    {
        "identifiers": ["NVDA", "VOO"],
        "mechanism": "ai_capex_stack",
        "summary": "Accelerator demand set the tape while the long end cut the other way.",
        "confidence": "Probable",
    }
]


def test_cross_name_clusters_reach_the_prompt_with_mechanism_and_confidence() -> None:
    """The whole point of L3: assembly may state a cross-name conclusion only
    because a prior pass established it. Mechanism, member names and the
    confidence label all have to survive into the prompt, or the body can only
    restate per-name notes again."""
    prompt = _prompt(cross_name_intel=_CLUSTERS)
    assert "CROSS-NAME" in prompt
    assert "ai_capex_stack" in prompt
    assert "NVDA, VOO" in prompt
    assert "[Probable]" in prompt
    assert "Accelerator demand" in prompt


def test_prompt_without_clusters_says_so_rather_than_omitting_the_block() -> None:
    """An absent block reads to the model as "not part of this task"; an
    explicit "none" reads as "this was checked and there was nothing" — the
    difference between silence and a licence to invent one."""
    prompt = _prompt(cross_name_intel=[])
    assert "CROSS-NAME" in prompt
    assert "no cross-holding mechanism" in prompt


def test_cluster_block_is_absent_from_the_instruction_when_no_clusters() -> None:
    """Instructing the model to "state the cross-name conclusion" when none was
    supplied is precisely how an invented one gets written."""
    assert "state the shared mechanism" not in _prompt(cross_name_intel=[])
    assert "state the shared mechanism" in _prompt(cross_name_intel=_CLUSTERS)


# ---------------------------------------------------------------------------
# Tracking-position display clamp (design doc §6.7, ruled 2026-08-18)
# ---------------------------------------------------------------------------


_TRACKING_PORTFOLIO: dict[str, Any] = {
    "base_currency": "USD",
    "total_base": 840000.0,
    "fx_date": "2026-08-17",
    "holdings": [
        {
            "name": "Taiwan Semiconductor",
            "ticker": "TSM",
            "market_value_base": 189000.0,
            "asset_class": "STOCK",
        },
        {
            "name": "China A50 ETF",
            "ticker": "513650.SS",
            "market_value_base": 53.0,
            "asset_class": "EQUITY_CN",
        },
    ],
    "by_asset_class": {"STOCK": 189000.0, "EQUITY_CN": 53.0},
    "by_currency": {"USD": 840000.0},
}


def test_sub_one_percent_holdings_are_labelled_as_tracking_positions() -> None:
    """A ruled product decision, not a cleanup: a near-zero position is a
    deliberate way to watch a name that is not owned yet, so it must stay
    VISIBLE. What it must not do is occupy a section the size of a 22.5%
    holding's — that is a display problem, and it is fixed in display."""
    prompt = ra.build_assembly_prompt(
        portfolio=_TRACKING_PORTFOLIO,
        price_anomalies=[],
        ticker_intel={
            "TSM": "TSM rose 1.2%. [Probable]",
            "513650.SS": "The A50 tracker drifted. [Speculative]",
        },
        macro_event_intel={},
        macro_event_exposure={},
    )
    assert "513650.SS" in prompt, "a tracking position must remain visible"
    assert "tracking position" in prompt.lower()


def test_tracking_positions_are_denied_a_section_of_their_own() -> None:
    prompt = ra.build_assembly_prompt(
        portfolio=_TRACKING_PORTFOLIO,
        price_anomalies=[],
        ticker_intel={"513650.SS": "The A50 tracker drifted. [Speculative]"},
        macro_event_intel={},
        macro_event_exposure={},
    )
    # Matched on the tracking-specific wording, not on a bare "one line" —
    # §4.2's own "ONE line per holding" instruction already contains that
    # phrase, so a looser assertion would pass without this feature existing.
    assert "never a heading of its own" in prompt


def test_a_large_holding_is_never_labelled_a_tracking_position() -> None:
    prompt = ra.build_assembly_prompt(
        portfolio=_TRACKING_PORTFOLIO,
        price_anomalies=[],
        ticker_intel={"TSM": "TSM rose 1.2%. [Probable]"},
        macro_event_intel={},
        macro_event_exposure={},
    )
    tsm_line = next(ln for ln in prompt.splitlines() if "Taiwan Semiconductor" in ln)
    assert "tracking" not in tsm_line.lower()


def test_assembly_prompt_version_bumped_for_the_quality_gate_contract() -> None:
    """PR #167 review round 3, nit: the user-turn contract grew a CROSS-NAME
    block, closed-set TRANSMISSION labels, TRACKING POSITION display rules,
    and a TECHNICAL POSITION block, but `ASSEMBLY_PROMPT_VERSION` stayed at
    the pre-PR value — the constant documented as existing precisely so a
    stored report's `report_inputs["assembly_prompt_version"]` says which
    contract produced it. A stale value means a pre- and post-quality-gate
    assembled report are indistinguishable by that field alone."""
    assert ra.ASSEMBLY_PROMPT_VERSION != "a4-v1"


def test_assembly_prompt_omits_not_processed_holdings() -> None:
    """Issue #311 / PR #312 B2: assembly is the Ring 1 production body.
    A capture_supported=False holding must not appear as (unvalued)."""
    portfolio = {
        "base_currency": "USD",
        "total_base": 100.0,
        "fx_date": "2026-09-01",
        "holdings": [
            {
                "name": "Apple",
                "ticker": "AAPL",
                "currency": "USD",
                "market_value": 100.0,
                "market_value_base": 100.0,
                "asset_class": "STOCK",
                "capture_supported": True,
            },
            {
                "name": "BHP Group",
                "ticker": "BHP.AX",
                "currency": "AUD",
                "market_value": None,
                "market_value_base": None,
                "asset_class": "STOCK",
                "capture_supported": False,
            },
        ],
        "by_asset_class": {"STOCK": 100.0},
        "by_currency": {"USD": 100.0},
        "by_market": {"US": 100.0},
        "concentration": {},
        "stale_tickers": [],
    }
    prompt = ra.build_assembly_prompt(
        portfolio=portfolio,
        price_anomalies=[],
        ticker_intel={},
        macro_event_intel={},
        macro_event_exposure={},
    )
    assert "Apple" in prompt
    assert "BHP.AX" not in prompt
    assert "BHP Group" not in prompt
