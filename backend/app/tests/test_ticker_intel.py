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
    must NOT be cached (it would fan out to every user holding the
    identifier) — the batch call degrades to "no L1 intel for this
    identifier" rather than raising."""

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
    count = (
        db_session.execute(select(TickerIntel).where(TickerIntel.identifier == "NVDA"))
        .scalars()
        .all()
    )
    assert count == []


def test_llm_failure_degrades_without_raising(db_session: Session) -> None:
    def _raise(*args: object, **kwargs: object) -> str:
        raise RuntimeError("provider down")

    with (
        patch("app.services.ticker_intel._openrouter_client", return_value=MagicMock()),
        patch("app.services.ticker_intel._call_llm", side_effect=_raise),
    ):
        result = ti.get_l1_intel_batch(db_session, ["NVDA"], _DATE, {"NVDA": _facts()})

    assert result == {}


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
