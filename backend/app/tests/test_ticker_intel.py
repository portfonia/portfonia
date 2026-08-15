"""L1 shared ticker-intel cache (issue #128, Ring 1 A2 — design doc §4)."""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.ticker_intel import TickerIntel
from app.services import ticker_intel as ti

_DATE = date(2026, 8, 15)


def _facts(**overrides: object) -> ti.L1Facts:
    defaults: dict[str, object] = {
        "net_pct": 0.075,
        "max_day_pct": 0.05,
        "trigger": "single_day",
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
# build_l1_candidates: ordering + facts assembly from report context shapes
# ---------------------------------------------------------------------------


def test_build_l1_candidates_orders_by_anomaly_then_news_only() -> None:
    anomalies = [
        {
            "identifier": "NVDA",
            "window_net_pct": 0.075,
            "max_day_pct": 0.05,
            "trigger": "single_day",
            "market": "US",
            "current_price": 215.0,
            "prev_price": 200.0,
            "latest_date": "2026-08-14",
        },
        {
            "identifier": "AAPL",
            "window_net_pct": 0.03,
            "max_day_pct": 0.01,
            "trigger": "cumulative",
            "market": "US",
            "current_price": 110.0,
            "prev_price": 106.0,
            "latest_date": "2026-08-14",
        },
    ]
    holding_news = {
        "AAPL": [{"title": "Apple event"}],
        "TSLA": [{"title": "Tesla recall"}],  # news-only, no anomaly
    }
    order, facts = ti.build_l1_candidates(anomalies, holding_news, [])

    assert order == ["NVDA", "AAPL", "TSLA"]
    assert facts["NVDA"].net_pct == 0.075
    assert facts["AAPL"].news_headlines == ["Apple event"]
    assert facts["TSLA"].net_pct is None
    assert facts["TSLA"].news_headlines == ["Tesla recall"]


def test_build_l1_candidates_attaches_technical_facts() -> None:
    anomalies = [
        {
            "identifier": "NVDA",
            "window_net_pct": 0.075,
            "max_day_pct": 0.05,
            "trigger": "single_day",
            "market": "US",
            "current_price": 215.0,
            "prev_price": 200.0,
            "latest_date": "2026-08-14",
        }
    ]
    technical = [{"ticker": "NVDA", "pct_vs_sma50": 0.1, "pct_vs_sma200": 0.2}]
    _order, facts = ti.build_l1_candidates(anomalies, {}, technical)
    assert facts["NVDA"].pct_vs_sma50 == 0.1
    assert facts["NVDA"].pct_vs_sma200 == 0.2


def test_build_l1_candidates_theme_anomaly_expands_to_constituents_not_theme_slug() -> None:
    """Round 3 review bug: a theme-merged anomaly's OWN fields
    (`window_net_pct`, `current_price`, `prev_price`) are value-weighted by
    the CALLING USER's own holdings mix (window_data._merge_theme_anomalies
    picks/weights by `Holding.current_value`) — two holders of the same
    theme with different constituent weightings would get different
    numbers, and whichever user's report runs first in the fan-out writes
    that weighting into the shared cache for everyone else. The theme slug
    ("gold") must never become an L1 candidate itself; each constituent's
    own identifier becomes a candidate instead, using ONLY its own
    `pct_change` (global — set from compute_global_moves before any
    per-user merge/weighting happens), never the theme-level merged
    numbers."""
    anomalies = [
        {
            "identifier": "gold",
            "window_net_pct": 0.05,  # value-weighted by THIS user's mix — must not leak
            "max_day_pct": 0.03,
            "trigger": "single_day",
            "market": "US",
            "current_price": 190.0,  # dominant-by-THIS-user's-value — must not leak
            "prev_price": 180.0,
            "latest_date": "2026-08-14",
            "constituents": [
                {
                    "name": "SPDR Gold",
                    "identifier": "SGOL",
                    "pct_change": 0.08,
                    "current_value": 5000.0,
                },
                {"name": "GLD", "identifier": "GLD", "pct_change": 0.02, "current_value": 1000.0},
            ],
        }
    ]
    order, facts = ti.build_l1_candidates(anomalies, {}, [])

    assert "gold" not in facts
    assert "gold" not in order
    assert set(order) == {"SGOL", "GLD"}
    # Each constituent's own (global) pct_change, not the merged weighted figure.
    assert facts["SGOL"].net_pct == 0.08
    assert facts["GLD"].net_pct == 0.02
    # No global source for these at constituent granularity — must not
    # silently inherit the theme-level (per-user) merged values.
    assert facts["SGOL"].current_price is None
    assert facts["SGOL"].prev_price is None
    assert facts["SGOL"].max_day_pct is None


def test_build_l1_candidates_theme_constituent_gets_its_own_technical_facts() -> None:
    anomalies = [
        {
            "identifier": "gold",
            "window_net_pct": 0.05,
            "constituents": [
                {
                    "name": "SPDR Gold",
                    "identifier": "SGOL",
                    "pct_change": 0.08,
                    "current_value": 5000.0,
                },
                {"name": "GLD", "identifier": "GLD", "pct_change": 0.02, "current_value": 1000.0},
            ],
        }
    ]
    technical = [
        {"ticker": "SGOL", "pct_vs_sma50": 0.08},
        {"ticker": "GLD", "pct_vs_sma50": 0.01},
    ]
    _order, facts = ti.build_l1_candidates(anomalies, {}, technical)
    assert facts["SGOL"].pct_vs_sma50 == 0.08
    assert facts["GLD"].pct_vs_sma50 == 0.01


def test_build_l1_candidates_single_ticker_anomaly_is_unaffected() -> None:
    """A regular (non-theme-merged) single-ticker anomaly always serializes
    with `constituents: []` too (`_serialize_anomalies`: `a.constituents or
    []`) — the SAME shape a theme entry would have if it somehow had zero
    constituents (which `_merge_theme_anomalies` never actually produces:
    a theme bucket only exists if it has >=1 member). There is no reliable
    signal to distinguish the two from an empty list alone, so an empty
    `constituents` list is treated as "not a theme merge" — the single-
    ticker path, using the anomaly's own (genuinely global) top-level
    fields, which is the overwhelmingly common real case."""
    anomalies = [
        {
            "identifier": "NVDA",
            "window_net_pct": 0.075,
            "current_price": 215.0,
            "prev_price": 200.0,
            "constituents": [],
        }
    ]
    order, facts = ti.build_l1_candidates(anomalies, {}, [])
    assert order == ["NVDA"]
    assert facts["NVDA"].net_pct == 0.075
    assert facts["NVDA"].current_price == 215.0


def test_build_l1_prompt_never_contains_theme_weighted_net_pct() -> None:
    """Two users with different constituent weightings of the same theme
    must not converge on one leaked figure — locks the fix at the prompt
    level, not just the facts-assembly level."""
    anomalies_user_a = [
        {
            "identifier": "gold",
            "window_net_pct": 0.0512,  # user A's own weighted figure
            "constituents": [
                {
                    "name": "SPDR Gold",
                    "identifier": "SGOL",
                    "pct_change": 0.08,
                    "current_value": 9000.0,
                },
                {"name": "GLD", "identifier": "GLD", "pct_change": 0.02, "current_value": 100.0},
            ],
        }
    ]
    _order, facts = ti.build_l1_candidates(anomalies_user_a, {}, [])
    prompt = ti._build_l1_prompt("SGOL", facts["SGOL"])
    assert "5.12" not in prompt  # the leaked weighted figure must never appear
    assert "+8.00%" in prompt  # SGOL's own global pct_change


def test_build_l1_prompt_excludes_per_user_trigger_classification() -> None:
    """Round 2 review finding: `trigger` (single_day vs cumulative) is
    determined by the CALLING USER's own asset_class threshold
    (window_data.select_user_anomalies) — two holders of the same
    identifier can classify the identical move differently. The first
    writer's classification must not be baked into the shared prompt text
    (kept in the stored `facts` JSONB for audit only, per L1Facts.to_jsonb)."""
    prompt = ti._build_l1_prompt("NVDA", _facts(trigger="cumulative"))
    assert "cumulative" not in prompt.lower()
    assert "trigger" not in prompt.lower()
