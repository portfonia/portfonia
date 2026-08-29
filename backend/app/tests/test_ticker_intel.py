"""L1 shared ticker-intel cache (issue #128, Ring 1 A2 — design doc §4)."""

from __future__ import annotations

import dataclasses
import inspect
from datetime import date
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import openai
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


def _request() -> httpx.Request:
    return httpx.Request("POST", "https://openrouter.test/v1/chat/completions")


def _connection_error() -> openai.APIConnectionError:
    """A retryable transport fault — what `_call_llm` re-raises AFTER its own
    connection backoff sequence is exhausted."""
    return openai.APIConnectionError(request=_request())


def _status_error(status: int) -> openai.APIStatusError:
    return openai.APIStatusError(
        "upstream said no", response=httpx.Response(status, request=_request()), body=None
    )


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


def test_stray_disclaimer_line_is_stripped_before_the_compliance_scan(
    db_session: Session,
) -> None:
    """Round 7 review finding: unlike Pass 2 (`cleaned = _strip_markers(raw_body)`
    in report_generator.py, then scanned), L1's `_generate` used to scan the
    LLM's raw output directly. A model-emitted disclaimer line legitimately
    contains advisory-sounding wording (here: the literal substring '投资建议',
    which is both a `compliance_vocab.yml` scan_term AND a
    `body_disclaimer_regex_terms_zh` entry) — pre-fix, that line alone would
    false-trip the scan and permanently blacklist the identifier for the day
    (a null-analysis marker is cached and never retried). `_strip_markers`
    drops the whole disclaimer line before the real analysis reaches the
    scan, same as Pass 2, so the compliant remainder is served and cached."""

    def _llm_with_stray_disclaimer(*args: object, **kwargs: object) -> str:
        return (
            "NVDA rallied on a confirmed earnings beat. [Established]\n"
            "本简报不构成投资建议，仅供参考。"  # noqa: RUF001
        )

    with (
        patch("app.services.ticker_intel._openrouter_client", return_value=MagicMock()),
        patch("app.services.ticker_intel._call_llm", side_effect=_llm_with_stray_disclaimer),
        patch("app.services.ticker_intel.send_ops_alert") as mock_alert,
    ):
        result = ti.get_l1_intel_batch(db_session, ["NVDA"], _DATE, {"NVDA": _facts()})

    mock_alert.assert_not_called()
    assert "NVDA" in result
    assert "不构成投资建议" not in result["NVDA"]
    assert "earnings beat" in result["NVDA"]
    row = db_session.execute(
        select(TickerIntel).where(TickerIntel.identifier == "NVDA")
    ).scalar_one()
    assert row.analysis is not None
    assert "不构成投资建议" not in row.analysis


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


def test_headline_only_facts_are_not_cached_and_dont_consume_the_daily_cap(
    db_session: Session,
) -> None:
    """Round 6 review finding: a pre-close manual run has no captured close
    for `trade_date` yet, so `day_pct` is None for every candidate — but a
    candidate with a matched headline still survives `build_l1_facts`'s own
    "headline-only briefing" degrade path (see its docstring: a move-less
    candidate with a headline is kept, not dropped). Caching such an
    analysis under the day's unique key would make it FINAL for the day:
    the real after_close batch, running later with a genuine `day_pct`
    available, would hit the cache and never re-analyze — every user that
    day permanently gets the numberless, pre-close briefing. The fix: an
    identifier with no `day_pct` is skipped entirely — no LLM call, no
    cache row of any kind (not even a null marker, which would ALSO
    permanently block a later retry) — so a later call the same day, once
    `day_pct` exists, can attempt it for real."""
    with (
        patch("app.services.ticker_intel._openrouter_client", return_value=MagicMock()),
        patch("app.services.ticker_intel._call_llm", side_effect=_mock_llm_ok) as mock_call,
    ):
        result = ti.get_l1_intel_batch(db_session, ["NVDA"], _DATE, {"NVDA": _facts(day_pct=None)})

    assert result == {}
    mock_call.assert_not_called()
    row = db_session.execute(
        select(TickerIntel).where(TickerIntel.identifier == "NVDA", TickerIntel.trade_date == _DATE)
    ).scalar_one_or_none()
    assert row is None


def test_headline_only_skip_does_not_consume_the_daily_cap(db_session: Session) -> None:
    with (
        patch("app.services.ticker_intel._MAX_L1_ANALYSES_PER_DAY", 1),
        patch("app.services.ticker_intel._openrouter_client", return_value=MagicMock()),
        patch("app.services.ticker_intel._call_llm", side_effect=_mock_llm_ok) as mock_call,
    ):
        # NVDA has no day_pct yet (pre-close) -> skipped, no budget spent.
        result = ti.get_l1_intel_batch(db_session, ["NVDA"], _DATE, {"NVDA": _facts(day_pct=None)})
        assert result == {}
        mock_call.assert_not_called()

        # AAPL DOES have a day_pct -> still gets its fresh analysis, cap intact.
        result2 = ti.get_l1_intel_batch(db_session, ["AAPL"], _DATE, {"AAPL": _facts(day_pct=0.05)})
        assert "AAPL" in result2
        mock_call.assert_called_once()


def test_headline_only_identifier_can_be_analyzed_later_same_day_once_day_pct_exists(
    db_session: Session,
) -> None:
    with (
        patch("app.services.ticker_intel._openrouter_client", return_value=MagicMock()),
        patch("app.services.ticker_intel._call_llm", side_effect=_mock_llm_ok) as mock_call,
    ):
        # Morning manual run: no close captured yet.
        first = ti.get_l1_intel_batch(db_session, ["NVDA"], _DATE, {"NVDA": _facts(day_pct=None)})
        assert first == {}
        mock_call.assert_not_called()

        # After-close batch, same trade_date: real day_pct now available.
        second = ti.get_l1_intel_batch(db_session, ["NVDA"], _DATE, {"NVDA": _facts(day_pct=0.03)})
        assert "NVDA" in second
        mock_call.assert_called_once()


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
# Transient vs. permanent failure (issue #160)
#
# A marker row locks a key for the rest of the trade_date. That is right for
# a failure an identical call would reproduce (bad key, malformed request) and
# for a compliance block, but wrong for a transient one: `_call_llm` has
# already absorbed its own backoff by the time it re-raises, and the next user
# in the fan-out may well be calling after the blip cleared. `attempt_count`
# bounds how many times the system as a whole may try, regardless of how many
# users ask.
# ---------------------------------------------------------------------------


def test_attempt_cap_is_three() -> None:
    """N is a product decision (issue #160): initial + 2 retries, chosen
    because anything reaching this module already survived `_call_llm`'s
    120s connection backoff. Locked so a future edit is deliberate."""
    assert ti._MAX_ATTEMPTS_PER_KEY == 3


def test_transient_failure_is_retried_by_the_next_caller(db_session: Session) -> None:
    """The issue #160 bug: one connection blip during the first user's report
    used to write a marker that silently starved every later user in the
    same fan-out (and every manual re-run) of that identifier's intel for the
    whole trading day."""
    with (
        patch("app.services.ticker_intel._openrouter_client", return_value=MagicMock()),
        patch("app.services.ticker_intel._call_llm", side_effect=_connection_error()) as mock_call,
    ):
        assert ti.get_l1_intel_batch(db_session, ["NVDA"], _DATE, {"NVDA": _facts()}) == {}
        assert ti.get_l1_intel_batch(db_session, ["NVDA"], _DATE, {"NVDA": _facts()}) == {}

    assert mock_call.call_count == 2


def test_transient_failure_stops_after_the_per_key_attempt_cap(db_session: Session) -> None:
    """The other half of the trade-off: a genuinely persistent outage must
    not turn into one LLM call per user. Every caller shares ONE session here
    on purpose — that is the fan-out's real shape
    (`generate_incremental_report` hands the same Session to every user), so
    a stale identity-mapped row would show up as attempts past the cap."""
    with (
        patch("app.services.ticker_intel._openrouter_client", return_value=MagicMock()),
        patch("app.services.ticker_intel._call_llm", side_effect=_connection_error()) as mock_call,
    ):
        for _ in range(6):
            assert ti.get_l1_intel_batch(db_session, ["NVDA"], _DATE, {"NVDA": _facts()}) == {}

    assert mock_call.call_count == ti._MAX_ATTEMPTS_PER_KEY


def test_transient_failure_then_success_serves_the_real_analysis(db_session: Session) -> None:
    """A retry that succeeds must upgrade the marker row in place, not leave
    a NULL row shadowing the real analysis for the rest of the day."""
    outcomes: list[object] = [
        _connection_error(),
        "NVDA rallied on an earnings beat. [Established]",
    ]

    def _flaky(*args: object, **kwargs: object) -> str:
        outcome = outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return str(outcome)

    with (
        patch("app.services.ticker_intel._openrouter_client", return_value=MagicMock()),
        patch("app.services.ticker_intel._call_llm", side_effect=_flaky),
    ):
        assert ti.get_l1_intel_batch(db_session, ["NVDA"], _DATE, {"NVDA": _facts()}) == {}
        second = ti.get_l1_intel_batch(db_session, ["NVDA"], _DATE, {"NVDA": _facts()})

    assert "NVDA" in second
    row = db_session.execute(
        select(TickerIntel).where(TickerIntel.identifier == "NVDA")
    ).scalar_one()
    assert row.analysis is not None
    assert row.attempt_count == 2


def test_non_retryable_failure_locks_the_key_immediately(db_session: Session) -> None:
    """A 401 reproduces exactly on an identical call — spending the retry
    budget on it only delays the real diagnosis (llm_errors' own rationale)."""
    with (
        patch("app.services.ticker_intel._openrouter_client", return_value=MagicMock()),
        patch("app.services.ticker_intel._call_llm", side_effect=_status_error(401)) as mock_call,
    ):
        for _ in range(3):
            assert ti.get_l1_intel_batch(db_session, ["NVDA"], _DATE, {"NVDA": _facts()}) == {}

    mock_call.assert_called_once()


def test_compliance_block_locks_the_key_immediately(db_session: Session) -> None:
    """A compliance block is not a transient fault: retrying re-risks the
    same violation, re-alerts ops, and costs a real call each time."""

    def _bad_llm(*args: object, **kwargs: object) -> str:
        return "You should buy NVDA now."

    with (
        patch("app.services.ticker_intel._openrouter_client", return_value=MagicMock()),
        patch("app.services.ticker_intel._call_llm", side_effect=_bad_llm) as mock_call,
        patch("app.services.ticker_intel.send_ops_alert"),
    ):
        for _ in range(3):
            assert ti.get_l1_intel_batch(db_session, ["NVDA"], _DATE, {"NVDA": _facts()}) == {}

    mock_call.assert_called_once()
    row = db_session.execute(
        select(TickerIntel).where(TickerIntel.identifier == "NVDA")
    ).scalar_one()
    assert row.attempt_count == ti._MAX_ATTEMPTS_PER_KEY


def test_a_lock_charges_the_daily_budget_what_it_writes(db_session: Session) -> None:
    """Review round 1 (blacktomb42, PR #162): a permanent lock writes
    `attempt_count = _MAX_ATTEMPTS_PER_KEY` while the in-batch loop used to
    decrement `fresh_budget` by 1, so the two halves of the same cap
    disagreed — the first caller in a batch could spend a full 1-per-call
    budget, while every later caller (reading `SUM(attempt_count)`) saw three
    slots gone per lock. The invariant is that the in-batch decrement equals
    the change this write makes to the SUM."""

    def _bad_llm(*args: object, **kwargs: object) -> str:
        return "You should buy NVDA now."

    identifiers = ["NVDA", "AAPL", "MSFT", "GOOG", "AMZN"]
    with (
        patch("app.services.ticker_intel._MAX_L1_ANALYSES_PER_DAY", 4),
        patch("app.services.ticker_intel._openrouter_client", return_value=MagicMock()),
        patch("app.services.ticker_intel._call_llm", side_effect=_bad_llm) as mock_call,
        patch("app.services.ticker_intel.send_ops_alert"),
    ):
        assert (
            ti.get_l1_intel_batch(
                db_session, identifiers, _DATE, {i: _facts() for i in identifiers}
            )
            == {}
        )

    # Each lock charges 3, so a budget of 4 buys two attempts, not four.
    # (Overshooting the cap by at most one key's charge is inherent: the
    # charge isn't known until the attempt resolves.)
    assert mock_call.call_count == 2
    assert ti._attempts_today(db_session, _DATE) == 2 * ti._MAX_ATTEMPTS_PER_KEY


def test_retry_attempts_count_against_the_daily_budget(db_session: Session) -> None:
    """The daily cap counts ATTEMPTS, not rows: a retried key must not get
    its extra attempts for free, or the cap silently loosens by a factor of
    `_MAX_ATTEMPTS_PER_KEY` on a bad day."""
    with (
        patch("app.services.ticker_intel._MAX_L1_ANALYSES_PER_DAY", 2),
        patch("app.services.ticker_intel._openrouter_client", return_value=MagicMock()),
        patch("app.services.ticker_intel._call_llm", side_effect=_connection_error()) as mock_call,
    ):
        ti.get_l1_intel_batch(db_session, ["NVDA"], _DATE, {"NVDA": _facts()})
        ti.get_l1_intel_batch(db_session, ["NVDA"], _DATE, {"NVDA": _facts()})
        assert mock_call.call_count == 2

        # Two attempts spent on NVDA exhaust the day's budget: a
        # never-attempted identifier gets nothing.
        assert ti.get_l1_intel_batch(db_session, ["AAPL"], _DATE, {"AAPL": _facts()}) == {}
        assert mock_call.call_count == 2


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


def test_l1_prompt_version_is_v4_after_macro_briefs_removal() -> None:
    assert ti._PROMPT_VERSION == "l1-v4"


def test_l1_uses_luna_with_effort_none_not_flash(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}

    def _fake_call(_client: Any, model: str, system: str, user: str, **kwargs: Any) -> str:
        captured["model"] = model
        captured["kwargs"] = kwargs
        captured["system"] = system
        return "TSM rose with no company-specific headline. AI-capex brief applies. [Probable]"

    monkeypatch.setattr(ti, "_openrouter_client", lambda: object())
    monkeypatch.setattr(ti, "_call_llm", _fake_call)
    monkeypatch.setattr(ti, "_scan_forbidden_output", lambda _text: [])
    monkeypatch.setattr(ti, "_strip_markers", lambda text: text)
    monkeypatch.setattr(ti, "_write_cache", lambda *a, **k: None)
    facts = ti.L1Facts(day_pct=0.012, latest_date="2026-08-17")
    text, _charged = ti._generate(
        session=object(),  # type: ignore[arg-type]
        identifier="TSM",
        trade_date=date(2026, 8, 17),
        facts=facts,
    )
    assert captured["model"] == "openai/gpt-5.6-luna"
    assert captured["kwargs"].get("reasoning_effort") == "none"
    assert captured["kwargs"].get("disable_reasoning") is not True
    assert "2-4 sentences" not in captured["system"]
    assert text is not None


def test_build_l1_prompt_dates_lookback_and_headlines() -> None:
    facts = ti.L1Facts(
        day_pct=0.012,
        latest_date="2026-08-17",
        news_headlines=["2026-08-16: Anthropic revenue run-rate hits $65B"],
        dated_moves=["2026-08-14: +0.40%", "2026-08-17: +1.22%"],
    )
    prompt = ti._build_l1_prompt("TSM", facts)
    assert "2026-08-16: Anthropic" in prompt
    assert "2026-08-14: +0.40%" in prompt
    assert "date" in prompt.lower()


# ---------------------------------------------------------------------------
# l1_identifiers_for_user: the per-user -> global type firewall
# ---------------------------------------------------------------------------


def test_holding_identifier_normalizes_known_collision_ticker() -> None:
    """issue #204 PR #253 review: _holding_identifier fed
    large_weight_identifiers/_weighted_identifiers, which must key a PSH
    holding under 'PSH.L' — the same identifier compute_global_moves and
    select_user_anomalies use — or top-weight selection silently loses it."""
    assert ti._holding_identifier({"ticker": "PSH"}) == "PSH.L"


def test_build_l1_facts_normalizes_known_collision_ticker_before_matching_technical_positions() -> (
    None
):
    """Same shape as the HK case above, for issue #204's PSH/PSH.L collision:
    technical_positions[].ticker is the raw Holding.ticker ('PSH'), while
    identifiers/day_moves keys are the normalized form ('PSH.L')."""
    identifiers = ti.l1_identifiers_for_user([{"identifier": "PSH.L", "constituents": []}])
    technical = [{"ticker": "PSH", "pct_vs_sma50": 0.04}]
    facts = ti.build_l1_facts(identifiers, {"PSH.L": _move("PSH.L")}, {}, technical)
    assert facts["PSH.L"].pct_vs_sma50 == 0.04


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


def test_l1_identifiers_adds_top_weight_holdings_after_anomalies() -> None:
    """Issue #128 quality gate: a 22% name with no price move still needs
    L1, or assembly can only write [Speculative] for the book's anchor."""
    anomalies = [{"identifier": "AAOI", "window_net_pct": 0.19, "constituents": []}]
    holdings = [
        {
            "ticker": "TSM",
            "market_value_base": 225_000.0,
            "asset_class": "STOCK",
        },
        {
            "ticker": "VOO",
            "market_value_base": 260_000.0,
            "asset_class": "EQUITY_US_BROAD",
        },
        {
            "ticker": "AAOI",
            "market_value_base": 50_000.0,
            "asset_class": "STOCK",
        },
        {
            "ticker": "TINY",
            "market_value_base": 10_000.0,
            "asset_class": "STOCK",
        },
    ]
    result = ti.l1_identifiers_for_user(
        anomalies,
        holdings=holdings,
        portfolio_total=545_000.0,
    )
    assert result[0] == "AAOI"
    assert "TSM" in result
    assert "VOO" in result
    assert "TINY" not in result
    assert all(isinstance(i, str) for i in result)


def _split_lot_holdings() -> list[dict[str, Any]]:
    """VOO split across two lots (140k + 130k = 270k combined, this book's
    largest single exposure) alongside five other distinct single-lot
    holdings. Sized so the bug is unambiguous either way `top_k=5` picks
    without per-identifier aggregation: each VOO lot (140k/130k) individually
    outranks BBB (120k)/CCC (110k), so an UNaggregated top-5 by raw weight
    is [AAA, VOO, VOO, BBB, CCC] — VOO occupies two slots (a duplicate
    identifier) and DDD (100k), the fifth genuinely distinct holding, is
    wrongly pushed out. Aggregated, VOO's combined 270k is the single largest
    identifier and the correct top-5 is [VOO, AAA, BBB, CCC, DDD]."""
    return [
        {"ticker": "VOO", "market_value_base": 140_000.0, "asset_class": "EQUITY_US_BROAD"},
        {"ticker": "AAA", "market_value_base": 150_000.0, "asset_class": "STOCK"},
        {"ticker": "VOO", "market_value_base": 130_000.0, "asset_class": "EQUITY_US_BROAD"},
        {"ticker": "BBB", "market_value_base": 120_000.0, "asset_class": "STOCK"},
        {"ticker": "CCC", "market_value_base": 110_000.0, "asset_class": "STOCK"},
        {"ticker": "DDD", "market_value_base": 100_000.0, "asset_class": "STOCK"},
        {"ticker": "EEE", "market_value_base": 90_000.0, "asset_class": "STOCK"},
    ]


def test_large_weight_identifiers_aggregates_split_lots_of_same_identifier() -> None:
    """PR #168 review round 1 suggestion: a holding split across two lots
    (this product preserves upload order, so the same ticker legitimately
    appears as more than one `Holding` row — VOO is the worked example in
    CLAUDE.md) must be combined by identifier BEFORE ranking, not ranked as
    two separate half-sized entries. Un-aggregated, the two VOO rows can each
    independently qualify for `top_k` and take two of its slots, silently
    evicting a genuinely distinct 5th holding (DDD) that should have made the
    cut on its own combined weight ranking."""
    holdings = _split_lot_holdings()
    result = ti.large_weight_identifiers(holdings, portfolio_total=840_000.0)
    assert result == ["VOO", "AAA", "BBB", "CCC", "DDD"]
    assert result.count("VOO") == 1, f"VOO must occupy exactly one slot, got {result}"
    assert "DDD" in result, "DDD must not be evicted by VOO's un-aggregated duplicate slot"
    assert len(result) == len(set(result)), f"no identifier should repeat: {result}"


def test_l1_identifiers_for_user_weight_channel_also_aggregates_split_lots() -> None:
    """Same bug, the other call site the review flagged: `l1_identifiers_for_user`'s
    weight channel re-implements its own selection inline (`_weighted_identifiers`
    called directly, not through `large_weight_identifiers`) rather than sharing
    the fixed selection — so fixing `large_weight_identifiers` alone would not
    have closed this path. Locks that the two call sites can no longer drift:
    the per-user L1 candidate list must show the same aggregated ranking."""
    holdings = _split_lot_holdings()
    result = ti.l1_identifiers_for_user([], holdings=holdings, portfolio_total=840_000.0)
    assert result.count("VOO") == 1, f"VOO must occupy exactly one slot, got {result}"
    assert "DDD" in result, "DDD must not be evicted by VOO's un-aggregated duplicate slot"


def test_l1_identifiers_adds_holdings_in_l2_exposed_classes() -> None:
    """PR #167 review round 1, suggestion: the original version of this test
    put the exposed holding (QQQ) at ~97% of the book — already selected by
    the WEIGHT channel (`top_k=5`, `min_weight=5%`) regardless of whether the
    class-intersection channel does anything at all, so it could not fail if
    that channel were deleted outright (verified: it did not, when the
    channel was temporarily gutted to check).

    `l1_identifiers_for_user`'s own docstring says the class channel has NO
    weight floor specifically so a tracking position still gets L1 coverage —
    that claim is what needed a holding placed BELOW `_L1_MIN_WEIGHT` to
    actually test. TSM here is a control at the same tiny weight but in a
    class NOT in `exposed_asset_classes`, so a bug that added every small
    holding regardless of class (not just the exposed one) would also be
    caught.
    """
    anomalies: list[dict[str, Any]] = []
    holdings = [
        {
            "ticker": "QQQ",
            "market_value_base": 400.0,
            "asset_class": "EQUITY_US_TECH",
        },
        {
            "ticker": "TSM",
            "market_value_base": 400.0,
            "asset_class": "STOCK",
        },
        {
            "ticker": "VOO",
            "market_value_base": 99_200.0,
            "asset_class": "EQUITY_US_BROAD",
        },
    ]
    result = ti.l1_identifiers_for_user(
        anomalies,
        holdings=holdings,
        portfolio_total=100_000.0,
        exposed_asset_classes=["EQUITY_US_TECH"],
    )
    assert "QQQ" in result, "the exposed small holding must be added despite being under min_weight"
    assert "TSM" not in result, "an unexposed small holding must not ride along"


def test_l1_identifiers_extra_channels_still_return_strings_only() -> None:
    holdings = [
        {
            "ticker": "TSM",
            "name": "do-not-leak",
            "market_value_base": 90_000.0,
            "asset_class": "STOCK",
            "weight": 0.9,
        }
    ]
    result = ti.l1_identifiers_for_user(
        [],
        holdings=holdings,
        portfolio_total=100_000.0,
        exposed_asset_classes=["STOCK"],
    )
    assert result == ["TSM"]


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


def test_build_l1_facts_formats_dated_lookback_moves() -> None:
    lookback = {
        date(2026, 8, 14): {"TSM": _move("TSM", net_pct=Decimal("0.004"))},
        date(2026, 8, 17): {"TSM": _move("TSM", net_pct=Decimal("0.0122"))},
    }
    facts = ti.build_l1_facts(
        ["TSM"],
        lookback[date(2026, 8, 17)],
        {},
        [],
        lookback_moves=lookback,
    )
    assert facts["TSM"].dated_moves == ["2026-08-14: +0.40%", "2026-08-17: +1.22%"]


def test_build_l1_facts_has_no_macro_briefs_channel() -> None:
    """Regression lock (PR #167 review round 1, bug 1): `ctx.macro_event_intel`
    is a per-user L2 SELECTION (`l2_event_keys_for_user` over this user's own
    `macro_signals`/watermark/`news_surfaced`). A prior draft passed it into
    `build_l1_facts` as `macro_briefs`, baking that per-user selection into a
    value written to the shared `ticker_intel` cache — whichever user's report
    reached an identifier first would freeze THEIR macro-brief set into a row
    every later holder reads. `build_l1_facts` must have no parameter through
    which that dict (or any dict shaped like it) could arrive; `L1Facts` must
    have no field to carry it. L3 (`cross_name_intel.get_day_synthesis`)
    performs the L1+L2 join instead — globally, once per trading day."""
    params = set(inspect.signature(ti.build_l1_facts).parameters)
    assert "macro_briefs" not in params
    fields = {f.name for f in dataclasses.fields(ti.L1Facts)}
    assert "macro_briefs" not in fields


def test_l1_system_prompt_does_not_reference_macro_briefs() -> None:
    """PR #167 review round 2, suggestion: removing the DATA channel
    (previous test) is not the same as removing the INSTRUCTION to use it.
    `_L1_SYSTEM` still told the model to cover "any supplied macro brief"
    and, absent a company catalyst, to "connect a supplied macro brief when
    its date and mechanism fit" — a field `_build_l1_prompt` never emits
    (the user turn says "grounded ONLY in the facts above"). An instruction
    to use a field that no longer exists is worse than a no-op: it invites
    the model to either misread a dated headline as a "macro brief" or
    invent a mechanism to satisfy the system prompt, which is exactly the
    fabrication l1-v4 exists to prevent."""
    assert "macro brief" not in ti._L1_SYSTEM.lower()


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


# ---------------------------------------------------------------------------
# Multi-user fan-out fairness (issue #128 A4, design doc §5.7 hand-off item 1)
# ---------------------------------------------------------------------------


def test_first_user_in_a_fanout_cannot_spend_the_whole_daily_cap(
    db_session: Session,
) -> None:
    """The third recurrence of the fan-out fairness bug (A1 Tavily budget ->
    A2 L1 cap -> A3 L2 cap), and the one A3 explicitly could not fix its own
    way: L1 candidates are per-user (different users hold different
    identifiers), so there is no key prefix to split the budget on.

    Without a share, the first user in the fixed `active_user_ids` order
    analyzes candidates until the day's cap is gone, and every later user —
    the SAME users every day, since that order never rotates — gets nothing
    fresh.
    """
    idents = [f"T{i}" for i in range(9)]
    facts = {i: _facts() for i in idents}
    with (
        patch("app.services.ticker_intel._MAX_L1_ANALYSES_PER_DAY", 9),
        patch("app.services.ticker_intel._openrouter_client", return_value=MagicMock()),
        patch("app.services.ticker_intel._call_llm", side_effect=_mock_llm_ok) as mock_call,
    ):
        # User 1 of 3 offers every candidate; its share is ceil(9/3) = 3.
        first = ti.get_l1_intel_batch(db_session, idents, _DATE, facts, users_remaining=3)
        assert len(first) == 3, "first user must be capped at its own share"
        assert mock_call.call_count == 3

        # Budget genuinely survives for the users that come after.
        second = ti.get_l1_intel_batch(db_session, idents, _DATE, facts, users_remaining=2)
        assert len(second) == 6, "3 cache hits + 3 fresh from this user's share"
        assert mock_call.call_count == 6

        third = ti.get_l1_intel_batch(db_session, idents, _DATE, facts, users_remaining=1)
        assert len(third) == 9, "last user may spend everything still left"
        assert mock_call.call_count == 9


def test_unused_share_flows_forward_instead_of_being_stranded(
    db_session: Session,
) -> None:
    """A user with fewer candidates than its share must not strand the
    remainder: the next user's share is recomputed from what is actually
    left, not from a fixed per-user quota."""
    with (
        patch("app.services.ticker_intel._MAX_L1_ANALYSES_PER_DAY", 9),
        patch("app.services.ticker_intel._openrouter_client", return_value=MagicMock()),
        patch("app.services.ticker_intel._call_llm", side_effect=_mock_llm_ok) as mock_call,
    ):
        ti.get_l1_intel_batch(db_session, ["AAA"], _DATE, {"AAA": _facts()}, users_remaining=3)
        assert mock_call.call_count == 1

        # 8 slots left, 2 users to go -> this user may take 4, not 3.
        idents = [f"U{i}" for i in range(8)]
        second = ti.get_l1_intel_batch(
            db_session, idents, _DATE, {i: _facts() for i in idents}, users_remaining=2
        )
        assert len(second) == 4
        assert mock_call.call_count == 5


def test_default_call_site_is_unrestricted_by_the_fanout_share(
    db_session: Session,
) -> None:
    """Every pre-A4 caller omits `users_remaining` and must keep the whole
    budget — this mechanism may not change single-user behavior."""
    idents = [f"V{i}" for i in range(4)]
    with (
        patch("app.services.ticker_intel._MAX_L1_ANALYSES_PER_DAY", 4),
        patch("app.services.ticker_intel._openrouter_client", return_value=MagicMock()),
        patch("app.services.ticker_intel._call_llm", side_effect=_mock_llm_ok) as mock_call,
    ):
        got = ti.get_l1_intel_batch(db_session, idents, _DATE, {i: _facts() for i in idents})
        assert len(got) == 4
        assert mock_call.call_count == 4
