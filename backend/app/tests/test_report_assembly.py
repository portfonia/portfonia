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
    assert "do not introduce" in ra._ASSEMBLY_SYSTEM.lower()


def test_assembly_system_prompt_keeps_every_shared_narrative_rule() -> None:
    """These rules are compliance-adjacent — a body pass that lost DIRECTION
    REQUIRES EVIDENCE would assert price direction with no window data."""
    system = ra._ASSEMBLY_SYSTEM
    for marker in (
        "MANDATORY COMPLIANCE",
        "FORWARD EVENTS:",
        "DIRECTION REQUIRES EVIDENCE:",
        "DIVERGENCE IS THE SIGNAL:",
        "§4.2 CROSS-REFERENCES:",
    ):
        assert marker in system, f"assembly system prompt lost: {marker}"


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
