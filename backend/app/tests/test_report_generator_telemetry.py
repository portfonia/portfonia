"""End-to-end operational-event integration for the report pipeline
(issue #446).

`test_report_generator.py` proves report CONTENT correctness with the same
mocking strategy; this file proves the operational-event SIDE CHANNEL fires
correctly alongside it — real stage spans, real elapsed/overhead numbers,
real Postgres rows, for the acceptance scenarios `test_operational_events.py`
cannot exercise in isolation (it never calls `generate_report` itself).

The seeded user is committed through a genuinely separate connection (see
`_seed_real_user`, mirroring `test_operational_events.py`'s helper) — unlike
`test_report_generator.py`'s `_seed_test_user` fixture, which only flushes
within `db_session`'s own SAVEPOINT-nested transaction and is therefore
invisible to `operational_events`'s independent sink connection (its
`user_id` FK would silently fail-open with zero rows written, defeating the
point of this file).
"""

from __future__ import annotations

import contextlib
import copy
import uuid
from datetime import date
from typing import Any, cast
from unittest.mock import MagicMock, patch

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.database import get_engine
from app.models.operational_event import OperationalEvent
from app.models.user import User
from app.services import report_generator as rg
from app.tests.test_report_generator import (
    _FAKE_TAVILY_RESULTS,
    _anomaly,
    _macro_hit,
    _mock_llm,
    _news_item,
    _portfolio_snap,
)

_USER = uuid.uuid4()
_TODAY = date(2026, 6, 4)


def _seed_real_user(user_id: uuid.UUID) -> None:
    with get_engine().connect() as conn:
        conn.execute(
            pg_insert(User).values(
                id=user_id,
                auth_provider="supabase",
                auth_subject=f"sub-{user_id}",
                email=f"{user_id}@example.com",
                status="active",
                locale="en",
                base_currency="USD",
                report_cadence="mwf",
            )
        )
        conn.commit()


def _events_for_report(db_session: Session, report_id: uuid.UUID) -> list[OperationalEvent]:
    """Every event for the run this report's attempt executed under.

    A direct `generate_report` call with no active run context becomes its
    own root run — its OWN `report.generate` start event necessarily
    precedes `report_id` being known (Design §1: report_id becomes
    available only after the fresh row's own `session.flush()`), so that
    start row's `report_id` column is null by design. Readers correlate
    via `run_id`/span ancestry, not a `report_id` filter on every row — the
    one row guaranteed to carry `report_id` early is the explicit `context`
    event `set_report_id` emits, which is what this looks up first.
    """
    context_event = db_session.execute(
        select(OperationalEvent).where(
            OperationalEvent.report_id == report_id, OperationalEvent.event_kind == "context"
        )
    ).scalar_one()
    return list(
        db_session.execute(
            select(OperationalEvent)
            .where(OperationalEvent.run_id == context_event.run_id)
            .order_by(OperationalEvent.recorded_at)
        ).scalars()
    )


def test_generate_report_full_path_emits_matched_stage_spans(db_session: Session) -> None:
    _seed_real_user(_USER)
    with (
        patch("app.services.report_generator.send_report_email", return_value=True),
        patch("app.services.report_generator.compute_portfolio", return_value=_portfolio_snap()),
        patch(
            "app.services.report_generator.load_news_window",
            return_value=[_news_item("Fed raises rates")],
        ),
        patch("app.services.report_generator.detect_macro_signals", return_value=_macro_hit()),
        patch(
            "app.services.report_generator.detect_window_anomalies", return_value=([_anomaly()], 2)
        ),
        patch("app.services.report_generator._openrouter_client", return_value=MagicMock()),
        patch("app.services.report_generator._call_llm", side_effect=_mock_llm),
        patch(
            "app.services.report_generator._run_tavily_search",
            side_effect=lambda *a, **kw: copy.deepcopy(_FAKE_TAVILY_RESULTS),
        ),
    ):
        report = rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)

    assert report.status == "success"
    events = _events_for_report(db_session, report.id)
    assert events, "expected operational_events rows correlated to this report"

    by_operation: dict[str, list[OperationalEvent]] = {}
    for e in events:
        by_operation.setdefault(e.operation, []).append(e)

    # Root attempt: exactly one start + one end, outcome ok, full path.
    root = [e for e in events if e.operation == "report.generate"]
    assert {e.event_kind for e in root} == {"start", "end"}
    root_end = next(e for e in root if e.event_kind == "end")
    assert root_end.outcome == "ok"
    assert root_end.attributes.get("path") == "full"
    assert root_end.attributes.get("report_status") == "success"
    stage_state = cast(dict[str, Any], root_end.attributes.get("stage_state") or {})
    for stage in (
        "preparation",
        "pass1_query_gen",
        "tavily_search",
        "l2_intel",
        "l1_intel",
        "l3_synthesis",
        "shadow_assembly",
        "render_and_compliance",
        "persist_report",
        "email_send",
    ):
        assert stage_state.get(stage) == "ok", f"{stage} should be ok, got {stage_state.get(stage)}"

    # Every major named stage entered has a matched start/end pair.
    for stage in (
        "preparation",
        "pass1_query_gen",
        "l2_intel",
        "l1_intel",
        "l3_synthesis",
        "render_and_compliance",
        "persist_report",
        "email_send",
    ):
        kinds = {e.event_kind for e in by_operation.get(stage, [])}
        assert kinds == {"start", "end"}, f"{stage}: {kinds}"

    # Tavily search occurrences aggregate by SUM at read time, not
    # in-process — at least one occurrence should be present (two search
    # sites run given a seeded anomaly + macro hit: the macro-themed pass
    # and the anomaly-targeted pass).
    tavily_starts = [e for e in by_operation.get("tavily_search", []) if e.event_kind == "start"]
    assert len(tavily_starts) >= 1
    assert all(e.parent_span_id == root[0].span_id for e in tavily_starts)

    # llm_call child spans exist for Pass 1 at minimum (Pass 2 too, since
    # assembly is disabled by default in this test's Settings).
    llm_calls = [e for e in by_operation.get("llm_call", []) if e.event_kind == "start"]
    assert len(llm_calls) >= 1

    # Every event on this run/attempt carries the seeded user_id.
    assert all(e.user_id == _USER for e in events)

    # elapsed_ms on every completed end event is a real, non-negative number.
    for e in events:
        if e.event_kind == "end":
            assert e.elapsed_ms is not None and e.elapsed_ms >= 0

    # Telemetry overhead is reported, not claimed zero (Contract constraints
    # "measure telemetry overhead and event volume").
    assert isinstance(root_end.attributes.get("telemetry_overhead_ms"), int | float)
    print(
        f"[issue #446 evidence] full-path attempt: {len(events)} operational_events rows, "
        f"telemetry_overhead_ms={root_end.attributes.get('telemetry_overhead_ms')}, "
        f"root elapsed_ms={root_end.elapsed_ms}"
    )


def test_generate_report_pass2_truncation_leaves_pass2_span_failed_and_later_stages_not_reached(
    db_session: Session,
) -> None:
    """Design §5 worked example 1: Pass 2 raises → its end is failed; render/
    persist/email are not_reached in the root's stage_state."""
    user_id = uuid.uuid4()
    _seed_real_user(user_id)

    def _truncated_llm(
        client: object,
        model: str,
        system: str,
        user: str,
        *,
        with_holdings: bool = False,
        **kw: object,
    ) -> str:
        return "too short" if with_holdings else '{"queries": ["q1"]}'

    with (
        patch("app.services.report_generator.compute_portfolio", return_value=_portfolio_snap()),
        patch(
            "app.services.report_generator.load_news_window",
            return_value=[_news_item("Fed raises rates")],
        ),
        patch("app.services.report_generator.detect_macro_signals", return_value=_macro_hit()),
        patch(
            "app.services.report_generator.detect_window_anomalies", return_value=([_anomaly()], 2)
        ),
        patch("app.services.report_generator._openrouter_client", return_value=MagicMock()),
        patch("app.services.report_generator._call_llm", side_effect=_truncated_llm),
        patch(
            "app.services.report_generator._run_tavily_search",
            side_effect=lambda *a, **kw: copy.deepcopy(_FAKE_TAVILY_RESULTS),
        ),
        contextlib.suppress(RuntimeError),
    ):
        rg.generate_report(db_session, user_id=user_id, report_date=_TODAY)

    from app.models.report import Report

    report = db_session.execute(
        select(Report).where(Report.user_id == user_id, Report.report_date == _TODAY)
    ).scalar_one()
    events = _events_for_report(db_session, report.id)
    root_end = next(e for e in events if e.operation == "report.generate" and e.event_kind == "end")
    assert root_end.outcome == "failed"
    stage_state = cast(dict[str, Any], root_end.attributes.get("stage_state") or {})
    assert stage_state.get("preparation") == "ok"
    assert stage_state.get("pass1_query_gen") == "ok"
    assert stage_state.get("pass2_analysis") == "failed"
    assert stage_state.get("render_and_compliance") == "not_reached"
    assert stage_state.get("persist_report") == "not_reached"
    assert stage_state.get("email_send") == "not_reached"

    pass2_ends = [e for e in events if e.operation == "pass2_analysis" and e.event_kind == "end"]
    assert len(pass2_ends) == 1
    assert pass2_ends[0].outcome == "failed"
    assert pass2_ends[0].elapsed_ms is not None and pass2_ends[0].elapsed_ms >= 0
