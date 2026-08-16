"""L2 shared macro-event intel cache (issue #128, Ring 1 A3 — design doc §5).

The two hard principles A2 cost four review rounds to establish (design doc
§4.8) are load-bearing here and each has its own regression test below:

1. A cross-user shared cache may consume only globally-typed artifacts.
   SELECTION may be per-user; VALUES must not be.
   -> test_facts_come_from_global_day_news_not_the_calling_users_window
2. The window a shared entry describes must be a pure function of the date.
   -> test_build_l2_facts_signature_cannot_accept_per_user_state (structural)
      + the day-news test above (behavioral)
"""

from __future__ import annotations

import inspect
from datetime import UTC, date, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock, patch

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.timezones import ET
from app.models.forward_event import ForwardEvent
from app.models.macro_event_intel import MacroEventIntel
from app.models.news import News
from app.services import macro_event_intel as l2

_DATE = date(2026, 8, 14)


def _news(url: str, title: str, when: datetime) -> News:
    return News(url_hash=url, title=title, source="S", url=url, summary="", published_at=when)


def _on_day(hour: int = 12) -> datetime:
    """A timestamp inside `_DATE`'s ET calendar day."""
    return datetime(_DATE.year, _DATE.month, _DATE.day, hour, tzinfo=ET)


def _seed_day_news(session: Session, *, title: str = "Fed holds rates steady") -> News:
    row = _news("https://x.test/day", title, _on_day())
    session.add(row)
    session.flush()
    return row


def _seed_forward_event(session: Session, **overrides: Any) -> ForwardEvent:
    defaults: dict[str, Any] = {
        "event_type": "macro",
        "name": "Consumer Price Index (CPI)",
        "ticker": "",
        "scheduled_date": _DATE + timedelta(days=3),
        "source": "fred",
        "captured_at": datetime.now(tz=UTC),
    }
    defaults.update(overrides)
    row = ForwardEvent(**defaults)
    session.add(row)
    session.flush()
    return row


def _macro_signals(*themes: str) -> dict[str, Any]:
    """The per-user `ctx.macro_signals` shape (`_serialize_macro`)."""
    return {
        "has_any_hit": bool(themes),
        "total_matched_articles": len(themes),
        "hits": [
            {
                "theme": t,
                "keywords_found": ["Fed"],
                "article_count": 1,
                # Deliberately a DIFFERENT headline from the global day news
                # the facts builder is required to use — see the contamination
                # test below.
                "top_articles": [{"title": f"per-user article for {t}", "source": "S"}],
            }
            for t in themes
        ],
    }


def _llm_json(
    analysis: str = "The Fed held rates steady, a policy-path datapoint. [Established]",
    classes: list[str] | None = None,
    sectors: list[str] | None = None,
) -> str:
    import json

    return json.dumps(
        {
            "analysis": analysis,
            "affected_asset_classes": ["EQUITY_US_BROAD"] if classes is None else classes,
            "affected_sectors": ["Financials"] if sectors is None else sectors,
        }
    )


def _mock_llm_ok(*args: object, **kwargs: object) -> str:
    return _llm_json()


def _patched_llm(side_effect: object) -> Any:
    return (
        patch("app.services.macro_event_intel._openrouter_client", return_value=MagicMock()),
        patch("app.services.macro_event_intel._call_llm", side_effect=side_effect),
    )


# ---------------------------------------------------------------------------
# Principle 1: the per-user -> global firewall
# ---------------------------------------------------------------------------


def test_event_keys_for_user_returns_plain_strings_only(db_session: Session) -> None:
    """`ctx.macro_signals` is per-user (its themes AND their backing articles
    come from `load_news_window(..., user_id)`). Exactly one thing may cross
    into the shared cache: which event keys are worth analyzing."""
    _seed_forward_event(db_session)
    keys = l2.l2_event_keys_for_user(db_session, _DATE, _macro_signals("货币政策"))

    assert all(isinstance(k, str) for k in keys)
    assert "theme:货币政策" in keys
    # No per-user article title can ride along inside a key.
    assert not any("per-user article" in k for k in keys)


def test_event_keys_include_global_forward_events_for_the_day(db_session: Session) -> None:
    row = _seed_forward_event(db_session)
    keys = l2.l2_event_keys_for_user(db_session, _DATE, _macro_signals())
    assert f"fwd:{row.id}" in keys


def test_event_keys_exclude_forward_events_outside_the_horizon(db_session: Session) -> None:
    far = _seed_forward_event(db_session, scheduled_date=_DATE + timedelta(days=60))
    past = _seed_forward_event(db_session, name="Old", scheduled_date=_DATE - timedelta(days=1))
    keys = l2.l2_event_keys_for_user(db_session, _DATE, _macro_signals())
    assert f"fwd:{far.id}" not in keys
    assert f"fwd:{past.id}" not in keys


def test_event_key_order_is_independent_of_the_calling_user(db_session: Session) -> None:
    """The daily analysis cap is consumed in list order, so an order derived
    from whichever user happens to run first in the fan-out would
    systematically starve the users behind them (the shape of the Tavily
    budget problem A1 handed to A2). Ordering here is deterministic and
    global: sorted themes, then forward events by scheduled date."""
    _seed_forward_event(db_session)
    first = l2.l2_event_keys_for_user(db_session, _DATE, _macro_signals("科技监管", "货币政策"))
    second = l2.l2_event_keys_for_user(db_session, _DATE, _macro_signals("货币政策", "科技监管"))
    assert first == second


def test_build_l2_facts_signature_cannot_accept_per_user_state() -> None:
    """Structural guard (A2's type-boundary lesson, design doc §4.8): the
    values side takes a Session, plain strings and a date — there is no
    parameter a caller could use to pass a user's anomalies, watermark,
    portfolio or news window into a row that ships to every user."""
    params = list(inspect.signature(l2.build_l2_facts).parameters)
    assert params == ["session", "event_keys", "trade_date"]


def test_facts_come_from_global_day_news_not_the_calling_users_window(
    db_session: Session,
) -> None:
    """The A2 round-3 bug class, transplanted: `ctx.macro_signals.hits[].
    top_articles` is drawn from the caller's OWN news window (watermark +
    `news_surfaced` ledger), so whichever user's report reached L2 first
    would stamp their own article set into the shared row."""
    _seed_day_news(db_session, title="Fed holds rates steady")
    facts = l2.build_l2_facts(db_session, ["theme:货币政策"], _DATE)

    assert facts["theme:货币政策"].news_headlines == ["Fed holds rates steady"]
    assert "per-user article" not in str(facts["theme:货币政策"].to_jsonb())


def test_facts_ignore_news_published_outside_the_trade_date(db_session: Session) -> None:
    """Principle 2: an L2 row describes exactly one trading day, so a
    headline from an adjacent day must not reach it (a user whose report
    window spans a week must not widen what the shared row describes)."""
    db_session.add(
        _news("https://x.test/old", "Fed hiked rates last month", _on_day() - timedelta(days=5))
    )
    _seed_day_news(db_session, title="Fed holds rates steady")
    facts = l2.build_l2_facts(db_session, ["theme:货币政策"], _DATE)

    assert facts["theme:货币政策"].news_headlines == ["Fed holds rates steady"]


def test_forward_event_facts_read_the_global_row(db_session: Session) -> None:
    row = _seed_forward_event(db_session, event_type="earnings", name="NVDA", ticker="NVDA")
    facts = l2.build_l2_facts(db_session, [f"fwd:{row.id}"], _DATE)

    entry = facts[f"fwd:{row.id}"]
    assert entry.label == "NVDA"
    assert entry.ticker == "NVDA"
    assert entry.scheduled_date == (_DATE + timedelta(days=3)).isoformat()


def test_theme_with_no_global_day_coverage_is_dropped(db_session: Session) -> None:
    """Principle 4 (A2's round-6 lesson): a candidate with no global facts
    gets no entry — the caller must not cache a factless briefing that would
    hold the day's only cache slot for that key."""
    facts = l2.build_l2_facts(db_session, ["theme:货币政策"], _DATE)
    assert facts == {}


# ---------------------------------------------------------------------------
# Read-through cache: shared across users
# ---------------------------------------------------------------------------


def test_second_call_same_day_hits_cache_without_a_second_llm_call(
    db_session: Session,
) -> None:
    """UAT-7 at unit level: the same event analyzed by two users on the same
    day costs exactly one inference."""
    _seed_day_news(db_session)
    keys = ["theme:货币政策"]
    facts = l2.build_l2_facts(db_session, keys, _DATE)

    client_patch, call_patch = _patched_llm(_mock_llm_ok)
    with client_patch, call_patch as mock_call:
        first = l2.get_l2_intel_batch(db_session, keys, _DATE, facts)
        second = l2.get_l2_intel_batch(db_session, keys, _DATE, facts)

    assert mock_call.call_count == 1
    assert first == second
    rows = db_session.execute(select(MacroEventIntel)).scalars().all()
    assert len(rows) == 1


def test_cached_row_carries_the_validated_structured_output(db_session: Session) -> None:
    _seed_day_news(db_session)
    keys = ["theme:货币政策"]
    facts = l2.build_l2_facts(db_session, keys, _DATE)

    client_patch, call_patch = _patched_llm(_mock_llm_ok)
    with client_patch, call_patch:
        result = l2.get_l2_intel_batch(db_session, keys, _DATE, facts)

    assert result["theme:货币政策"]["affected_asset_classes"] == ["EQUITY_US_BROAD"]
    assert result["theme:货币政策"]["affected_sectors"] == ["Financials"]
    row = db_session.execute(select(MacroEventIntel)).scalars().one()
    assert row.affected_asset_classes == ["EQUITY_US_BROAD"]
    assert row.analysis is not None


def test_generate_call_keeps_deny_enforced_no_byok(db_session: Session) -> None:
    """Same compliance contract as L1 (design doc §4.3): forward-event keys
    are derived from the holdings universe, so the BYOK exception carved out
    for Pass 1/translation must not be reused here."""
    _seed_day_news(db_session)
    keys = ["theme:货币政策"]
    facts = l2.build_l2_facts(db_session, keys, _DATE)

    client_patch, call_patch = _patched_llm(_mock_llm_ok)
    with client_patch, call_patch as mock_call:
        l2.get_l2_intel_batch(db_session, keys, _DATE, facts)

    kwargs = mock_call.call_args.kwargs
    assert kwargs.get("enforce_data_collection", True) is True
    assert kwargs.get("provider_order") is None
    assert kwargs.get("with_holdings") is False


def test_daily_cap_stops_fresh_inferences(db_session: Session) -> None:
    _seed_day_news(db_session)
    keys = [f"theme:t{i}" for i in range(l2._MAX_L2_ANALYSES_PER_DAY + 3)]
    facts = {k: l2.L2Facts(event_kind="macro_theme", label=k, news_headlines=["h"]) for k in keys}

    client_patch, call_patch = _patched_llm(_mock_llm_ok)
    with client_patch, call_patch as mock_call:
        l2.get_l2_intel_batch(db_session, keys, _DATE, facts)

    assert mock_call.call_count == l2._MAX_L2_ANALYSES_PER_DAY


def test_candidate_without_facts_is_skipped_without_writing_a_row(
    db_session: Session,
) -> None:
    """Principle 4: no LLM call, no cache row at all — not even an
    "attempted" marker, which would block a later, better-informed retry
    the same day."""
    client_patch, call_patch = _patched_llm(_mock_llm_ok)
    with client_patch, call_patch as mock_call:
        result = l2.get_l2_intel_batch(db_session, ["theme:货币政策"], _DATE, {})

    assert result == {}
    assert mock_call.call_count == 0
    assert db_session.execute(select(MacroEventIntel)).scalars().all() == []


# ---------------------------------------------------------------------------
# Closed-enum validation (design doc §5.3)
# ---------------------------------------------------------------------------


def test_out_of_taxonomy_asset_class_is_dropped_and_never_reaches_the_mapping(
    db_session: Session,
) -> None:
    _seed_day_news(db_session)
    keys = ["theme:货币政策"]
    facts = l2.build_l2_facts(db_session, keys, _DATE)

    def _bogus(*args: object, **kwargs: object) -> str:
        return _llm_json(classes=["EQUITY_US_BROAD", "CRYPTO", "equity_us_broad_v2"])

    client_patch, call_patch = _patched_llm(_bogus)
    with client_patch, call_patch:
        result = l2.get_l2_intel_batch(db_session, keys, _DATE, facts)

    assert result["theme:货币政策"]["affected_asset_classes"] == ["EQUITY_US_BROAD"]
    row = db_session.execute(select(MacroEventIntel)).scalars().one()
    assert row.affected_asset_classes == ["EQUITY_US_BROAD"]

    exposure = l2.user_event_exposure(result, {"EQUITY_US_BROAD": 100.0, "CRYPTO": 50.0})
    assert exposure == {"theme:货币政策": ["EQUITY_US_BROAD"]}


def test_out_of_taxonomy_sector_is_dropped(db_session: Session) -> None:
    _seed_day_news(db_session)
    keys = ["theme:货币政策"]
    facts = l2.build_l2_facts(db_session, keys, _DATE)

    def _bogus(*args: object, **kwargs: object) -> str:
        return _llm_json(sectors=["Financials", "Semiconductors", "Other"])

    client_patch, call_patch = _patched_llm(_bogus)
    with client_patch, call_patch:
        result = l2.get_l2_intel_batch(db_session, keys, _DATE, facts)

    # "Other" is the unknown-bucket a HOLDING falls into, never a meaningful
    # affected sector for an event — accepting it would map every
    # unclassified holding into the event's exposure.
    assert result["theme:货币政策"]["affected_sectors"] == ["Financials"]


def test_unparseable_llm_output_writes_a_marker_row_and_serves_nothing(
    db_session: Session,
) -> None:
    _seed_day_news(db_session)
    keys = ["theme:货币政策"]
    facts = l2.build_l2_facts(db_session, keys, _DATE)

    def _garbage(*args: object, **kwargs: object) -> str:
        return "not json at all"

    client_patch, call_patch = _patched_llm(_garbage)
    with client_patch, call_patch:
        result = l2.get_l2_intel_batch(db_session, keys, _DATE, facts)

    assert result == {}
    row = db_session.execute(select(MacroEventIntel)).scalars().one()
    assert row.analysis is None


def test_llm_failure_writes_a_marker_row_and_is_not_retried_today(
    db_session: Session,
) -> None:
    _seed_day_news(db_session)
    keys = ["theme:货币政策"]
    facts = l2.build_l2_facts(db_session, keys, _DATE)

    def _boom(*args: object, **kwargs: object) -> str:
        raise RuntimeError("provider down")

    client_patch, call_patch = _patched_llm(_boom)
    with client_patch, call_patch as mock_call:
        assert l2.get_l2_intel_batch(db_session, keys, _DATE, facts) == {}
        assert l2.get_l2_intel_batch(db_session, keys, _DATE, facts) == {}

    assert mock_call.call_count == 1


# ---------------------------------------------------------------------------
# Compliance gate (design doc §5.5 step 4 / §4.3 blast-radius argument)
# ---------------------------------------------------------------------------


def test_forbidden_output_is_not_cached_and_alerts_ops(db_session: Session) -> None:
    _seed_day_news(db_session)
    keys = ["theme:货币政策"]
    facts = l2.build_l2_facts(db_session, keys, _DATE)

    def _advisory(*args: object, **kwargs: object) -> str:
        return _llm_json(analysis="You should buy US equities now.")

    client_patch, call_patch = _patched_llm(_advisory)
    with (
        client_patch,
        call_patch,
        patch("app.services.macro_event_intel.send_ops_alert") as mock_alert,
    ):
        result = l2.get_l2_intel_batch(db_session, keys, _DATE, facts)

    assert result == {}
    assert mock_alert.called
    row = db_session.execute(select(MacroEventIntel)).scalars().one()
    assert row.analysis is None
    assert "buy" not in (row.analysis or "")


def test_model_emitted_disclaimer_does_not_false_trip_the_scan(db_session: Session) -> None:
    """A2's round-7 finding, pre-empted here: the scan runs on the STRIPPED
    text, exactly as Pass 2 does. A disclaimer line the model added against
    instructions must not blacklist this event's only cache slot for the
    day."""
    _seed_day_news(db_session)
    keys = ["theme:货币政策"]
    facts = l2.build_l2_facts(db_session, keys, _DATE)

    def _with_disclaimer(*args: object, **kwargs: object) -> str:
        return _llm_json(
            analysis=(
                "The Fed held rates steady. [Established]\n本简报不构成投资建议，仅供参考。"  # noqa: RUF001
            )
        )

    client_patch, call_patch = _patched_llm(_with_disclaimer)
    with client_patch, call_patch:
        result = l2.get_l2_intel_batch(db_session, keys, _DATE, facts)

    assert "theme:货币政策" in result
    assert "投资建议" not in result["theme:货币政策"]["analysis"]


# ---------------------------------------------------------------------------
# Per-user mapping (design doc §5.3: pure set ops, zero LLM)
# ---------------------------------------------------------------------------


def test_user_event_exposure_is_a_pure_intersection_with_no_llm_call() -> None:
    intel = {
        "theme:货币政策": {
            "analysis": "x",
            "affected_asset_classes": ["EQUITY_US_BROAD", "BOND_FUND"],
            "affected_sectors": ["Financials"],
        },
        "fwd:1": {
            "analysis": "y",
            "affected_asset_classes": ["PRECIOUS_METALS"],
            "affected_sectors": [],
        },
    }
    with patch("app.services.macro_event_intel._call_llm") as mock_call:
        exposure = l2.user_event_exposure(intel, {"EQUITY_US_BROAD": 10.0, "CASH_EQUIV": 1.0})

    assert mock_call.call_count == 0
    # Only the classes this user actually holds survive; an event with no
    # overlap is omitted entirely rather than carried as an empty entry.
    assert exposure == {"theme:货币政策": ["EQUITY_US_BROAD"]}


def test_user_event_exposure_never_reads_sector() -> None:
    """CLAUDE.md keeps `sector` scoped to the forward-event holding-relevance
    mapping; A3 stores it for that consumer but must not widen its use into
    a second exposure dimension."""
    intel = {
        "theme:x": {
            "analysis": "x",
            "affected_asset_classes": [],
            "affected_sectors": ["Financials"],
        }
    }
    assert l2.user_event_exposure(intel, {"EQUITY_US_BROAD": 10.0}) == {}
