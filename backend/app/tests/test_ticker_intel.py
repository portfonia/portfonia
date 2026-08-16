"""L1 shared ticker-intel cache (issue #128, Ring 1 A2 — design doc §4)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock, patch

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.ticker_intel import TickerIntel
from app.services import ticker_intel as ti
from app.services.window_data import HoldingMove

_DATE = date(2026, 8, 15)


def _move(identifier: str, **overrides: object) -> HoldingMove:
    """A global `HoldingMove` — the ONLY legal source of L1 numeric facts."""
    defaults: dict[str, object] = {
        "identifier": identifier,
        "market": "US",
        "current_price": Decimal("215"),
        "prev_price": Decimal("200"),
        "net_pct": Decimal("0.075"),
        "max_day_pct": Decimal("0.05"),
        "max_day_date": date(2026, 8, 14),
        "baseline_date": date(2026, 8, 11),
        "latest_date": date(2026, 8, 14),
        "prev_close": Decimal("210"),
        "day_open": Decimal("211"),
        "day_high": Decimal("216"),
        "day_low": Decimal("209"),
        "day_close": Decimal("215"),
        "after_hours": None,
    }
    defaults.update(overrides)
    return HoldingMove(**defaults)  # type: ignore[arg-type]


def _facts(**overrides: object) -> ti.L1Facts:
    defaults: dict[str, object] = {
        "day_pct": 0.075,
        "market": "US",
        "current_price": 215.0,
        "prev_price": 200.0,
        "latest_date": "2026-08-14",
        "news_headlines": ["NVIDIA beats earnings"],
        "pct_vs_sma50": 0.1,
        "pct_vs_sma200": 0.2,
        "pct_in_52w_range": 0.9,
        "vol_20d_annualized": 0.35,
    }
    defaults.update(overrides)
    return ti.L1Facts(**defaults)  # type: ignore[arg-type]


def _mock_llm_ok(*args: object, **kwargs: object) -> str:
    return "NVDA rallied on a confirmed earnings beat. [Established]"


# ---------------------------------------------------------------------------
# Cache-first behavior: shared across callers
# ---------------------------------------------------------------------------


def test_generate_call_keeps_deny_enforced_no_byok_no_holdings(db_session: Session) -> None:
    """design doc §4.3 hard requirement: L1 is holdings-derived (identifiers
    are drawn from anomalies/holding-news), so `data_collection=deny` must
    stay enforced — L1 must NOT reuse Pass 1/translation's BYOK exception
    (`enforce_data_collection=False` + `_BYOK_PROVIDER_ORDER`). Review round
    1 flagged this as an untested compliance-critical parameter set —
    locking it down the same way test_pass1_prompt_excludes_holdings_
    derived_anomalies locks Pass 1's isolation contract."""
    with (
        patch("app.services.ticker_intel._openrouter_client", return_value=MagicMock()),
        patch("app.services.ticker_intel._call_llm", side_effect=_mock_llm_ok) as mock_call,
    ):
        ti.get_l1_intel_batch(db_session, ["NVDA"], _DATE, {"NVDA": _facts()})

    kwargs = mock_call.call_args.kwargs
    assert kwargs.get("with_holdings") is False
    assert (
        "enforce_data_collection" not in kwargs
    )  # must stay at _call_llm's deny-enforcing default
    assert "provider_order" not in kwargs  # must not reuse the Pass 1/translation BYOK pin
    assert "allow_fallbacks" not in kwargs


def test_fresh_analysis_records_usage_in_the_shared_sink(db_session: Session) -> None:
    """Round 2 review finding: L1's per-identifier spend used to be invisible
    in report_inputs.llm_calls (Pass 1/Pass 2 both record there) — a day's
    L1 cost couldn't be audited from the report row that triggered it."""

    def _mock_llm_with_usage(*args: object, **kwargs: object) -> str:
        sink = kwargs.get("usage_sink")
        if isinstance(sink, list):
            sink.append({"model": "test-model", "cost": 0.001})
        return "NVDA rallied on a confirmed earnings beat. [Established]"

    usage: list[dict[str, object]] = []
    with (
        patch("app.services.ticker_intel._openrouter_client", return_value=MagicMock()),
        patch("app.services.ticker_intel._call_llm", side_effect=_mock_llm_with_usage),
    ):
        ti.get_l1_intel_batch(db_session, ["NVDA"], _DATE, {"NVDA": _facts()}, usage_sink=usage)

    assert usage == [{"model": "test-model", "cost": 0.001}]


def test_cache_hit_records_no_usage(db_session: Session) -> None:
    def _mock_llm_with_usage(*args: object, **kwargs: object) -> str:
        sink = kwargs.get("usage_sink")
        if isinstance(sink, list):
            sink.append({"model": "test-model", "cost": 0.001})
        return "NVDA rallied on a confirmed earnings beat. [Established]"

    with (
        patch("app.services.ticker_intel._openrouter_client", return_value=MagicMock()),
        patch("app.services.ticker_intel._call_llm", side_effect=_mock_llm_with_usage),
    ):
        ti.get_l1_intel_batch(db_session, ["NVDA"], _DATE, {"NVDA": _facts()})
        usage: list[dict[str, object]] = []
        ti.get_l1_intel_batch(db_session, ["NVDA"], _DATE, {"NVDA": _facts()}, usage_sink=usage)

    assert usage == []


def test_first_call_generates_and_caches(db_session: Session) -> None:
    with (
        patch("app.services.ticker_intel._openrouter_client", return_value=MagicMock()),
        patch("app.services.ticker_intel._call_llm", side_effect=_mock_llm_ok) as mock_call,
    ):
        result = ti.get_l1_intel_batch(db_session, ["NVDA"], _DATE, {"NVDA": _facts()})

    assert result == {"NVDA": "NVDA rallied on a confirmed earnings beat. [Established]"}
    mock_call.assert_called_once()
    row = db_session.execute(
        select(TickerIntel).where(TickerIntel.identifier == "NVDA", TickerIntel.trade_date == _DATE)
    ).scalar_one()
    assert row.analysis == "NVDA rallied on a confirmed earnings beat. [Established]"
    assert row.model


def test_second_call_same_day_hits_cache_no_llm_call(db_session: Session) -> None:
    """UAT-4: two users sharing NVDA in the same batch trigger exactly one L1
    analysis LLM call — the second call (second user) must hit the DB cache."""
    with (
        patch("app.services.ticker_intel._openrouter_client", return_value=MagicMock()),
        patch("app.services.ticker_intel._call_llm", side_effect=_mock_llm_ok) as mock_call,
    ):
        first = ti.get_l1_intel_batch(db_session, ["NVDA"], _DATE, {"NVDA": _facts()})
        second = ti.get_l1_intel_batch(db_session, ["NVDA"], _DATE, {"NVDA": _facts()})

    assert first == second
    mock_call.assert_called_once()


def test_different_trade_date_is_a_cache_miss(db_session: Session) -> None:
    with (
        patch("app.services.ticker_intel._openrouter_client", return_value=MagicMock()),
        patch("app.services.ticker_intel._call_llm", side_effect=_mock_llm_ok) as mock_call,
    ):
        ti.get_l1_intel_batch(db_session, ["NVDA"], _DATE, {"NVDA": _facts()})
        ti.get_l1_intel_batch(db_session, ["NVDA"], date(2026, 8, 16), {"NVDA": _facts()})

    assert mock_call.call_count == 2


# ---------------------------------------------------------------------------
# Compliance gate: blocked output never cached, report still generatable
# ---------------------------------------------------------------------------


def test_forbidden_output_not_cached_and_alerted(db_session: Session) -> None:
    """design doc §4.3: an L1 output tripping the forbidden-vocabulary scan
    must NOT be served (it would fan out to every user holding the
    identifier) — the batch call degrades to "no L1 intel for this
    identifier" rather than raising. Review round 1 bug: the FIRST draft
    wrote nothing at all on a block, so every later user in the fan-out
    re-attempted (and re-risked) the exact same call — this now writes a
    null-analysis marker row so the attempt is remembered and not retried
    (see test_forbidden_output_is_not_retried_by_a_later_call)."""

    def _bad_llm(*args: object, **kwargs: object) -> str:
        return "You should buy NVDA now."

    with (
        patch("app.services.ticker_intel._openrouter_client", return_value=MagicMock()),
        patch("app.services.ticker_intel._call_llm", side_effect=_bad_llm),
        patch("app.services.ticker_intel.send_ops_alert") as mock_alert,
    ):
        result = ti.get_l1_intel_batch(db_session, ["NVDA"], _DATE, {"NVDA": _facts()})

    assert result == {}
    mock_alert.assert_called_once()
    row = db_session.execute(
        select(TickerIntel).where(TickerIntel.identifier == "NVDA")
    ).scalar_one()
    assert row.analysis is None


def test_forbidden_output_is_not_retried_by_a_later_call(db_session: Session) -> None:
    """The exact bug from review round 1: without a persisted marker, a
    ticker that consistently trips the compliance scan bypasses the daily
    cap entirely (every one of N users in the fan-out re-calls the LLM)."""

    def _bad_llm(*args: object, **kwargs: object) -> str:
        return "You should buy NVDA now."

    with (
        patch("app.services.ticker_intel._openrouter_client", return_value=MagicMock()),
        patch("app.services.ticker_intel._call_llm", side_effect=_bad_llm) as mock_call,
        patch("app.services.ticker_intel.send_ops_alert"),
    ):
        ti.get_l1_intel_batch(db_session, ["NVDA"], _DATE, {"NVDA": _facts()})
        result = ti.get_l1_intel_batch(db_session, ["NVDA"], _DATE, {"NVDA": _facts()})

    assert result == {}
    mock_call.assert_called_once()


def test_llm_failure_degrades_without_raising(db_session: Session) -> None:
    def _raise(*args: object, **kwargs: object) -> str:
        raise RuntimeError("provider down")

    with (
        patch("app.services.ticker_intel._openrouter_client", return_value=MagicMock()),
        patch("app.services.ticker_intel._call_llm", side_effect=_raise),
    ):
        result = ti.get_l1_intel_batch(db_session, ["NVDA"], _DATE, {"NVDA": _facts()})

    assert result == {}
    row = db_session.execute(
        select(TickerIntel).where(TickerIntel.identifier == "NVDA")
    ).scalar_one()
    assert row.analysis is None


def test_llm_failure_is_not_retried_by_a_later_call(db_session: Session) -> None:
    def _raise(*args: object, **kwargs: object) -> str:
        raise RuntimeError("provider down")

    with (
        patch("app.services.ticker_intel._openrouter_client", return_value=MagicMock()),
        patch("app.services.ticker_intel._call_llm", side_effect=_raise) as mock_call,
    ):
        ti.get_l1_intel_batch(db_session, ["NVDA"], _DATE, {"NVDA": _facts()})
        ti.get_l1_intel_batch(db_session, ["NVDA"], _DATE, {"NVDA": _facts()})

    mock_call.assert_called_once()


# ---------------------------------------------------------------------------
# Daily cap: bounded fresh analyses, cache hits are free
# ---------------------------------------------------------------------------


def test_daily_cap_blocks_fresh_analyses_but_not_cache_hits(db_session: Session) -> None:
    with (
        patch("app.services.ticker_intel._MAX_L1_ANALYSES_PER_DAY", 1),
        patch("app.services.ticker_intel._openrouter_client", return_value=MagicMock()),
        patch("app.services.ticker_intel._call_llm", side_effect=_mock_llm_ok) as mock_call,
    ):
        first = ti.get_l1_intel_batch(db_session, ["NVDA"], _DATE, {"NVDA": _facts()})
        assert "NVDA" in first
        mock_call.assert_called_once()

        # Cap now exhausted for today: a brand-new identifier gets no
        # fresh analysis, but NVDA (already cached) is still free.
        second = ti.get_l1_intel_batch(
            db_session,
            ["NVDA", "AAPL"],
            _DATE,
            {"NVDA": _facts(), "AAPL": _facts()},
        )
        assert second == {"NVDA": first["NVDA"]}
        mock_call.assert_called_once()  # no new call for AAPL


def test_a_blocked_attempt_counts_against_the_daily_cap(db_session: Session) -> None:
    """Review round 1 bug: `fresh_budget` used to only decrement on a
    successful cache write, so a compliance-blocked (or failed) identifier
    was free against the cap — a systematically-blocked ticker could bypass
    the daily cap entirely (unbounded retries, one per user in the
    fan-out). One blocked attempt must consume the same budget slot a
    successful one would."""

    def _bad_llm(*args: object, **kwargs: object) -> str:
        return "You should buy NVDA now."

    with (
        patch("app.services.ticker_intel._MAX_L1_ANALYSES_PER_DAY", 1),
        patch("app.services.ticker_intel._openrouter_client", return_value=MagicMock()),
        patch("app.services.ticker_intel._call_llm", side_effect=_bad_llm) as mock_call,
        patch("app.services.ticker_intel.send_ops_alert"),
    ):
        first = ti.get_l1_intel_batch(db_session, ["NVDA"], _DATE, {"NVDA": _facts()})
        assert first == {}
        mock_call.assert_called_once()

        # Cap exhausted by the one (blocked) attempt above — a different,
        # never-attempted identifier gets no fresh analysis this call.
        second = ti.get_l1_intel_batch(db_session, ["AAPL"], _DATE, {"AAPL": _facts()})
        assert second == {}
        mock_call.assert_called_once()  # no new call for AAPL


# ---------------------------------------------------------------------------
# L1 prompt isolation: no per-user data can reach the prompt
# ---------------------------------------------------------------------------


def test_l1_facts_has_no_per_user_fields() -> None:
    """Structural guarantee (design doc §4.3 hard constraint): L1Facts is a
    fixed, explicit set of PUBLIC fields. Position size, portfolio weight,
    account value, and holder count are not among them — there is no field
    a caller could populate to leak them into the L1 prompt."""
    forbidden_field_names = {
        "weight",
        "portfolio_weight",
        "position",
        "shares",
        "account_value",
        "user_id",
        "holder_count",
        "portfolio_total",
    }
    actual_fields = {f.name for f in ti.L1Facts.__dataclass_fields__.values()}
    assert not (actual_fields & forbidden_field_names)


def test_build_l1_prompt_excludes_per_user_keywords() -> None:
    prompt = ti._build_l1_prompt("NVDA", _facts())
    lowered = prompt.lower()
    for term in ("portfolio", "position size", "account value", "holder", "user"):
        assert term not in lowered


# ---------------------------------------------------------------------------
# l1_identifiers_for_user: the per-user -> global type firewall
# ---------------------------------------------------------------------------


def test_l1_identifiers_for_user_returns_plain_strings_only() -> None:
    """THE structural firewall (design doc §4.8 addendum). The per-user
    anomaly list may contribute exactly one thing to the shared L1 cache:
    WHICH identifiers are worth analyzing. Its return type being
    `list[str]` is what makes the round 2/3/4 bug family a type error
    rather than a review finding — no per-user field (weighted pct,
    dominant-holding price, user's asset_class-derived trigger, holding
    name, constituent current_value) has any channel to travel past it."""
    anomalies = [
        {
            "identifier": "gold",
            "name": "Gold (user A's label)",
            "window_net_pct": 0.0512,
            "trigger": "single_day",
            "asset_type": "COMMODITY",
            "current_price": 190.0,
            "constituents": [
                {"name": "SPDR Gold", "identifier": "SGOL", "current_value": 9000.0},
                {"name": "GLD", "identifier": "GLD", "current_value": 100.0},
            ],
        }
    ]
    result = ti.l1_identifiers_for_user(anomalies)
    assert all(isinstance(i, str) for i in result)


def test_l1_identifiers_for_user_expands_theme_to_constituents() -> None:
    anomalies = [
        {
            "identifier": "gold",
            "window_net_pct": 0.05,
            "constituents": [
                {"name": "SPDR Gold", "identifier": "SGOL", "current_value": 5000.0},
                {"name": "GLD", "identifier": "GLD", "current_value": 1000.0},
            ],
        }
    ]
    result = ti.l1_identifiers_for_user(anomalies)
    assert "gold" not in result
    assert set(result) == {"SGOL", "GLD"}


def test_l1_identifiers_for_user_keeps_single_ticker_anomalies() -> None:
    """A genuine single-ticker anomaly also serializes with
    `constituents: []` (`_serialize_anomalies` renders `a.constituents or
    []`), so an empty list means "not a theme merge" — the common case."""
    anomalies = [
        {"identifier": "NVDA", "window_net_pct": 0.075, "constituents": []},
        {"identifier": "AAPL", "window_net_pct": 0.03},
    ]
    assert ti.l1_identifiers_for_user(anomalies) == ["NVDA", "AAPL"]


def test_l1_identifiers_for_user_deduplicates() -> None:
    anomalies: list[dict[str, Any]] = [
        {"identifier": "SGOL", "window_net_pct": 0.08},
        {
            "identifier": "gold",
            "constituents": [{"identifier": "SGOL"}, {"identifier": "GLD"}],
        },
    ]
    assert ti.l1_identifiers_for_user(anomalies) == ["SGOL", "GLD"]


# ---------------------------------------------------------------------------
# build_l1_facts: every number comes from the global HoldingMove
# ---------------------------------------------------------------------------


def test_build_l1_facts_reads_every_number_from_global_moves() -> None:
    """`day_moves` here stands in for what `report_generator.py` actually
    passes: `resolve_global_moves` called with `day_window_bounds(eff_date)`,
    never a user's `[period_start, period_end]` (design doc §4.8, second
    addendum). `HoldingMove.net_pct` under those bounds already IS the
    single trading day's move (baseline = the close immediately before
    `eff_date`, latest = `eff_date`'s own close) — `build_l1_facts` maps it
    straight onto `L1Facts.day_pct`, no separate `max_day_pct` needed since
    a one-day window has exactly one day to report on."""
    moves = {"NVDA": _move("NVDA")}
    facts = ti.build_l1_facts(["NVDA"], moves, {}, [])
    assert facts["NVDA"].day_pct == 0.075
    assert facts["NVDA"].current_price == 215.0
    assert facts["NVDA"].prev_price == 200.0
    assert facts["NVDA"].market == "US"
    assert facts["NVDA"].latest_date == "2026-08-14"


def test_build_l1_facts_tolerates_an_identifier_with_no_global_move() -> None:
    """Degrade, never fabricate: an identifier the global move set has no
    entry for (no usable baseline/series) gets a headline-only briefing
    rather than inheriting numbers from anywhere else."""
    facts = ti.build_l1_facts(["MISSING"], {}, {"MISSING": ["Some headline"]}, [])
    assert facts["MISSING"].day_pct is None
    assert facts["MISSING"].current_price is None
    assert facts["MISSING"].news_headlines == ["Some headline"]


def test_build_l1_facts_attaches_technical_facts() -> None:
    technical = [{"ticker": "NVDA", "pct_vs_sma50": 0.1, "pct_vs_sma200": 0.2}]
    facts = ti.build_l1_facts(["NVDA"], {"NVDA": _move("NVDA")}, {}, technical)
    assert facts["NVDA"].pct_vs_sma50 == 0.1
    assert facts["NVDA"].pct_vs_sma200 == 0.2


def test_build_l1_facts_normalizes_hk_ticker_before_matching_technical_positions() -> None:
    """`l1_identifiers_for_user`/`select_user_anomalies`/`compute_global_moves`
    all key HK tickers via `_normalize_hk_ticker(...).upper()` (4-digit form,
    e.g. "0700.HK"). `technical_positions[].ticker` comes straight from
    `HoldingValue.ticker` (`portfolio_calculator.py`), which is the RAW
    `Holding.ticker` value — un-normalized whenever a holding reached the DB
    without going through `holding_parser._postprocess` (e.g. a client
    posting directly to `POST /holdings/confirm`, which bypasses it; see
    CLAUDE.md's cash/wmf section). A raw "700.HK" would never match the
    normalized "0700.HK" key, silently dropping technical facts from the L1
    briefing for exactly the holdings most likely to need suffix
    normalization in the first place."""
    identifiers = ti.l1_identifiers_for_user([{"identifier": "0700.HK", "constituents": []}])
    technical = [{"ticker": "700.HK", "pct_vs_sma50": 0.06}]
    facts = ti.build_l1_facts(identifiers, {"0700.HK": _move("0700.HK")}, {}, technical)
    assert facts["0700.HK"].pct_vs_sma50 == 0.06


def test_theme_constituents_get_full_global_facts_not_a_stripped_subset() -> None:
    """The round-3 fix could only give a theme constituent its own
    `pct_change`, leaving price/max_day/technical facts as None (no global
    source was reachable from the merged anomaly dict). Sourcing from
    `compute_global_moves` instead means a constituent is a first-class
    identifier with the SAME fact coverage a standalone anomaly gets —
    the briefing quality regression the old shape forced is gone."""
    moves = {
        "SGOL": _move("SGOL", net_pct=Decimal("0.08"), current_price=Decimal("54")),
        "GLD": _move("GLD", net_pct=Decimal("0.02"), current_price=Decimal("310")),
    }
    technical = [{"ticker": "SGOL", "pct_vs_sma50": 0.08}]
    anomalies = [
        {
            "identifier": "gold",
            "window_net_pct": 0.05,
            "constituents": [
                {"identifier": "SGOL", "current_value": 5000.0},
                {"identifier": "GLD", "current_value": 1000.0},
            ],
        }
    ]
    identifiers = ti.l1_identifiers_for_user(anomalies)
    facts = ti.build_l1_facts(identifiers, moves, {}, technical)

    assert "gold" not in facts
    assert facts["SGOL"].day_pct == 0.08
    assert facts["SGOL"].current_price == 54.0
    assert facts["SGOL"].pct_vs_sma50 == 0.08


def test_same_theme_different_user_weightings_produce_identical_l1_facts() -> None:
    """The round-3 leak, asserted at the new architectural level: two users
    holding the same theme in different proportions produce DIFFERENT
    merged anomaly dicts (different weighted `window_net_pct`, different
    dominant-holding prices), yet must resolve to byte-identical L1 facts,
    because the facts never come from those dicts at all."""
    moves = {
        "SGOL": _move("SGOL", net_pct=Decimal("0.08")),
        "GLD": _move("GLD", net_pct=Decimal("0.02")),
    }
    user_a = [
        {
            "identifier": "gold",
            "window_net_pct": 0.0512,  # A's value-weighted figure
            "current_price": 54.0,  # A's dominant holding
            "trigger": "single_day",
            "constituents": [
                {"identifier": "SGOL", "current_value": 9000.0},
                {"identifier": "GLD", "current_value": 100.0},
            ],
        }
    ]
    user_b = [
        {
            "identifier": "gold",
            "window_net_pct": 0.0254,  # B's value-weighted figure — differs
            "current_price": 310.0,  # B's dominant holding — differs
            "trigger": "cumulative",  # B's asset_class classifies it differently
            "constituents": [
                {"identifier": "GLD", "current_value": 8000.0},
                {"identifier": "SGOL", "current_value": 200.0},
            ],
        }
    ]
    facts_a = ti.build_l1_facts(ti.l1_identifiers_for_user(user_a), moves, {}, [])
    facts_b = ti.build_l1_facts(ti.l1_identifiers_for_user(user_b), moves, {}, [])

    assert facts_a["SGOL"].to_jsonb() == facts_b["SGOL"].to_jsonb()
    assert facts_a["GLD"].to_jsonb() == facts_b["GLD"].to_jsonb()
    # And neither user's weighted figure reaches the prompt.
    prompt = ti._build_l1_prompt("SGOL", facts_a["SGOL"])
    assert "5.12" not in prompt
    assert "2.54" not in prompt
    assert "+8.00%" in prompt


def test_l1_facts_has_no_trigger_field_at_all() -> None:
    """`trigger` is derived from the CALLING USER's own asset_class
    threshold. Round 2 stopped it reaching the prompt but kept it in the
    dataclass and in the SHARED row's `facts` JSONB as an audit column —
    recording "which user's classification happened to run first" on a
    row served to everyone else is the last residue of the per-user input
    model, and precisely the kind of field a future consumer (A3/A4) would
    read back in good faith. Removed outright."""
    assert "trigger" not in ti.L1Facts.__dataclass_fields__
    assert (
        "trigger"
        not in ti.build_l1_facts(["NVDA"], {"NVDA": _move("NVDA")}, {}, [])["NVDA"].to_jsonb()
    )
