"""Durable operational-event writer (issue #446).

Exercises the acceptance scenarios from the issue's Contract constraints
table that are testable against this module in isolation: independent
commit/rollback boundary, event_id dedup, sink fail-open + disable-on-error,
attribute allowlist, retention cleanup, user-purge cascade, context
isolation between consecutive runs, and the report-independent (FX) example.
Report-pipeline integration scenarios (resume/email-only/quiet paths, Tavily
aggregation, stage-state maps) live in `test_report_generator_telemetry.py`.
"""

from __future__ import annotations

import uuid
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core import operational_events as oe
from app.core.database import get_engine
from app.models.operational_event import OperationalEvent
from app.models.user import User


@pytest.fixture(autouse=True)
def _clear_context() -> Generator[None, None, None]:
    """Every test starts with no active run, and leaves none behind — a
    test that forgets to call end_run/end_attempt must not leak context
    into the next test (mirrors the "two consecutive task contexts never
    inherit each other's identity" acceptance scenario)."""
    oe._current_run.set(None)
    yield
    oe._current_run.set(None)


def _seed_real_user(user_id: uuid.UUID, email: str) -> None:
    """Insert a `users` row through a genuinely separate, immediately
    committed connection — NOT `db_session`, whose `.commit()` only
    releases its own SAVEPOINT inside the test's still-open OUTER
    transaction (see `conftest.db_session`'s docstring) and is therefore
    invisible to the independent connection `operational_events`'s sink
    uses. A `user_id` FK reference only resolves against rows a different
    connection can actually see, so any test needing the FK to hold real
    must seed the user this way."""
    with get_engine().connect() as conn:
        conn.execute(
            pg_insert(User).values(
                id=user_id,
                auth_provider="supabase",
                auth_subject=f"sub-{user_id}",
                email=email,
                status="active",
                locale="en",
                base_currency="USD",
                report_cadence="mwf",
            )
        )
        conn.commit()


def _read_events(db_session: Session, run_id: uuid.UUID) -> list[OperationalEvent]:
    # A fresh connection, not db_session's own SAVEPOINT-joined one —
    # exercises the "independent reader" acceptance scenario: this sink's
    # rows are visible to a second connection, not just to whatever wrote
    # them. Read Committed isolation means this also works via db_session
    # itself, but a separate connection is the more faithful test of
    # independence from the caller's own transaction state.
    with get_engine().connect() as conn:
        rows = conn.execute(select(OperationalEvent).where(OperationalEvent.run_id == run_id)).all()
    return [OperationalEvent(**row._mapping) for row in rows]


def test_start_end_run_writes_matched_rows(db_session: Session) -> None:
    oe.start_run("test.batch", attributes={"session_node": "manual"})
    run_id = oe.current_run_id()
    assert run_id is not None
    oe.end_run("ok")

    rows = _read_events(db_session, run_id)
    kinds = sorted(r.event_kind for r in rows)
    assert kinds == ["end", "start"]
    start_row = next(r for r in rows if r.event_kind == "start")
    end_row = next(r for r in rows if r.event_kind == "end")
    assert end_row.outcome == "ok"
    assert end_row.elapsed_ms is not None and end_row.elapsed_ms >= 0
    assert start_row.attributes.get("session_node") == "manual"


def test_nested_spans_record_parent_and_aggregate_by_operation(db_session: Session) -> None:
    oe.start_run("test.attempt")
    run_id = oe.current_run_id()
    assert run_id is not None
    span1 = oe.start_span("tavily_search")
    oe.end_span(span1, "ok", attributes={"external_request_count": 1})
    span2 = oe.start_span("tavily_search")
    oe.end_span(span2, "ok", attributes={"external_request_count": 1})
    oe.end_run("ok")

    rows = _read_events(db_session, run_id)
    tavily_starts = [r for r in rows if r.operation == "tavily_search" and r.event_kind == "start"]
    assert len(tavily_starts) == 2
    # Both occurrences are direct children of the root run span — the
    # "repeated Tavily calls aggregate" contract is a read-side SUM over
    # these rows (Design §4), not an in-process merge.
    assert {r.parent_span_id for r in tavily_starts} == {run_id}


def test_skip_span_has_no_elapsed_and_no_start_pair(db_session: Session) -> None:
    oe.start_run("test.attempt")
    run_id = oe.current_run_id()
    assert run_id is not None
    oe.skip_span("pass2_analysis", reason_code="assembly_selected")
    oe.end_run("ok")

    rows = _read_events(db_session, run_id)
    skipped = [r for r in rows if r.event_kind == "skipped"]
    assert len(skipped) == 1
    assert skipped[0].elapsed_ms is None
    assert skipped[0].reason_code == "assembly_selected"
    # No matching start/end pair for the skipped operation.
    assert not [r for r in rows if r.operation == "pass2_analysis" and r.event_kind == "start"]


def test_operation_span_records_failed_outcome_and_reraises(db_session: Session) -> None:
    oe.start_run("test.attempt")
    run_id = oe.current_run_id()
    assert run_id is not None
    with pytest.raises(RuntimeError), oe.operation_span("pass2_analysis"):
        raise RuntimeError("boom")
    oe.end_run("failed")

    rows = _read_events(db_session, run_id)
    end_row = next(r for r in rows if r.operation == "pass2_analysis" and r.event_kind == "end")
    assert end_row.outcome == "failed"
    assert end_row.reason_code == "RuntimeError"


def test_attempt_inherits_task_run_as_child_span(db_session: Session) -> None:
    """report_tasks.py's shape: one task run, two per-user attempts inside
    it — each attempt is a CHILD SPAN, not its own run (Design §1)."""
    oe.start_run("report.batch", attributes={"recipient_count": 2})
    run_id = oe.current_run_id()
    assert run_id is not None

    user_a = uuid.uuid4()
    _seed_real_user(user_a, "attempt-a@example.com")
    attempt_a = oe.start_attempt("report.generate", user_id=user_a)
    assert attempt_a.is_root is False
    oe.set_report_id(uuid.uuid4())
    oe.end_attempt(attempt_a, "ok")

    user_b = uuid.uuid4()
    _seed_real_user(user_b, "attempt-b@example.com")
    attempt_b = oe.start_attempt("report.generate", user_id=user_b)
    oe.end_attempt(attempt_b, "ok")

    oe.end_run("ok")

    rows = _read_events(db_session, run_id)
    attempts = [r for r in rows if r.operation == "report.generate" and r.event_kind == "start"]
    assert len(attempts) == 2
    assert {r.user_id for r in attempts} == {user_a, user_b}
    # The second attempt did not inherit the first attempt's report_id.
    second_start = max(attempts, key=lambda r: r.recorded_at)
    assert second_start.report_id is None


def test_direct_call_with_no_active_run_becomes_its_own_root(db_session: Session) -> None:
    assert oe.current_run_id() is None
    user_id = uuid.uuid4()
    _seed_real_user(user_id, "direct-root@example.com")
    token = oe.start_attempt("report.generate", user_id=user_id)
    assert token.is_root is True
    root_run_id = token.span_id
    assert root_run_id is not None
    oe.end_attempt(token, "ok")
    assert oe.current_run_id() is None

    rows = _read_events(db_session, root_run_id)
    assert {r.event_kind for r in rows} == {"start", "end"}
    assert all(r.user_id == user_id for r in rows)


def test_shared_helper_outside_active_run_emits_nothing(db_session: Session) -> None:
    """A helper module (e.g. report_llm.py) calling start_span/end_span with
    no attempt currently active — the shared-LLM-helpers acceptance
    scenario — must be a true no-op, not an orphan row."""
    assert oe.current_run_id() is None
    token = oe.start_span("llm_call")
    assert token.span_id is None
    oe.end_span(token, "ok")  # must not raise


def test_duplicate_event_id_yields_one_row(db_session: Session) -> None:
    event_id = uuid.uuid4()
    run_id = uuid.uuid4()
    row = {
        "event_id": event_id,
        "occurred_at": datetime.now(UTC),
        "run_id": run_id,
        "span_id": run_id,
        "parent_span_id": None,
        "event_kind": "start",
        "operation": "test.dup",
        "elapsed_ms": None,
        "outcome": None,
        "reason_code": None,
        "user_id": None,
        "report_id": None,
        "task_id": None,
        "dispatch_id": None,
        "job_id": None,
        "attributes": {},
    }
    stmt = (
        pg_insert(OperationalEvent)
        .values(**row)
        .on_conflict_do_nothing(index_elements=["event_id"])
    )
    with get_engine().connect() as conn:
        conn.execute(stmt)
        conn.execute(stmt)  # redelivered write of the exact same event_id
        conn.commit()

    rows = _read_events(db_session, run_id)
    assert len(rows) == 1


def test_business_rollback_does_not_remove_committed_events(db_session: Session) -> None:
    """The report row a business transaction was building rolls back; the
    operational_events rows already written during that attempt survive —
    Contract constraints "Real PostgreSQL: business rollback ... including
    new uncommitted report row"."""
    oe.start_run("report.generate")
    run_id = oe.current_run_id()
    assert run_id is not None
    span = oe.start_span("preparation")
    oe.end_span(span, "ok")

    # Simulate the business failure path: an uncommitted row on the test's
    # OWN session/transaction, then rollback — never touches oe's sink.
    db_session.add(
        User(
            auth_provider="supabase",
            email="rollback@example.com",
            status="active",
            locale="en",
            base_currency="USD",
            report_cadence="mwf",
        )
    )
    db_session.flush()
    db_session.rollback()

    oe.end_run("failed")

    rows = _read_events(db_session, run_id)
    assert {r.event_kind for r in rows} == {"start", "end"}
    assert len([r for r in rows if r.operation == "preparation"]) == 2


def test_sink_failure_disables_further_writes_for_this_run_only(db_session: Session) -> None:
    oe.start_run("test.attempt")  # written before the sink is patched — succeeds
    run_id = oe.current_run_id()
    assert run_id is not None

    with patch.object(oe, "_get_sink_engine", side_effect=RuntimeError("connection refused")):
        span = oe.start_span("pass1_query_gen")  # swallowed — one warning, no raise
        oe.end_span(span, "ok")  # also swallowed

    # The failed write never landed, and the run's sink is now disabled —
    # even a write attempted AFTER the patch is removed (end_run below,
    # with the real sink reachable again) is still swallowed, proving the
    # disable is sticky for this run rather than scoped to the patch.
    rows = _read_events(db_session, run_id)
    assert [r for r in rows if r.operation == "pass1_query_gen"] == []
    assert len(rows) == 1  # only the original start_run row

    oe.end_run("failed")  # still must not raise even though the sink stays disabled
    rows = _read_events(db_session, run_id)
    assert len(rows) == 1  # end_run's own write was swallowed too
    assert rows[0].event_kind == "start"

    # A later, separate root run is unaffected — the sink re-attempts.
    oe.start_run("test.attempt")
    other_run_id = oe.current_run_id()
    assert other_run_id is not None
    oe.end_run("ok")
    assert len(_read_events(db_session, other_run_id)) == 2


def test_attribute_allowlist_drops_unknown_keys_without_raising(db_session: Session) -> None:
    oe.start_run(
        "test.attempt",
        attributes={
            "session_node": "manual",  # allowed
            "raw_prompt": "some holdings-derived text",  # not allowed
        },
    )
    run_id = oe.current_run_id()
    assert run_id is not None
    oe.end_run("ok")

    rows = _read_events(db_session, run_id)
    start_row = next(r for r in rows if r.event_kind == "start")
    assert start_row.attributes == {"session_node": "manual"}


def test_cleanup_expired_events_respects_exact_cutoff(db_session: Session) -> None:
    now = datetime.now(UTC)
    old_id, recent_id, exact_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    def _row(event_id: uuid.UUID, recorded_at: datetime) -> dict[str, object]:
        return {
            "event_id": event_id,
            "occurred_at": now,
            "recorded_at": recorded_at,
            "run_id": event_id,
            "span_id": event_id,
            "parent_span_id": None,
            "event_kind": "start",
            "operation": "test.retention",
            "elapsed_ms": None,
            "outcome": None,
            "reason_code": None,
            "user_id": None,
            "report_id": None,
            "task_id": None,
            "dispatch_id": None,
            "job_id": None,
            "attributes": {},
        }

    with get_engine().connect() as conn:
        conn.execute(pg_insert(OperationalEvent).values(**_row(old_id, now - timedelta(days=91))))
        conn.execute(
            pg_insert(OperationalEvent).values(**_row(recent_id, now - timedelta(days=10)))
        )
        conn.execute(pg_insert(OperationalEvent).values(**_row(exact_id, now - timedelta(days=90))))
        conn.commit()

    deleted = oe.cleanup_expired_events(db_session, retention_days=90, batch_size=1000, now=now)
    assert deleted >= 1

    # Verified through db_session itself, not a separate connection: like
    # every other write `cleanup_expired_events` makes, its delete commits
    # only within db_session's own SAVEPOINT-nested transaction (see
    # conftest.db_session's docstring) — a fresh connection would not see
    # it either way, so a raw-connection check here would test the wrong
    # thing. A real (non-test) caller's plain `SessionLocal()` has no such
    # wrapping and commits for real.
    remaining_ids = set(
        db_session.execute(
            select(OperationalEvent.event_id).where(
                OperationalEvent.event_id.in_([old_id, recent_id, exact_id])
            )
        ).scalars()
    )
    assert old_id not in remaining_ids
    assert recent_id in remaining_ids
    assert exact_id in remaining_ids


def test_user_purge_cascades_operational_events(db_session: Session) -> None:
    user_id = uuid.uuid4()
    _seed_real_user(user_id, "purge-telemetry@example.com")

    oe.start_run("report.generate", user_id=user_id)
    run_id = oe.current_run_id()
    assert run_id is not None
    oe.end_run("ok")
    assert len(_read_events(db_session, run_id)) == 2

    # Deleted through the same independently-committed connection the user
    # was seeded on — db_session's own commit only releases a SAVEPOINT
    # inside the test's still-open outer transaction and would not make
    # the CASCADE's effect visible to the sink's independent connection.
    with get_engine().connect() as conn:
        conn.execute(delete(User).where(User.id == user_id))
        conn.commit()

    assert _read_events(db_session, run_id) == []


def test_capture_fx_example_has_no_report_dependency(db_session: Session) -> None:
    """Contract constraints "Reusable example / isolation": a future
    capture.fx integration uses the SAME writer, with no Report row at all
    — user_id/report_id stay null throughout."""
    oe.start_run("capture.fx")
    run_id = oe.current_run_id()
    assert run_id is not None
    span = oe.start_span(
        "capture.fx",
        attributes={"external_request_count": 1},
    )
    oe.end_span(span, "ok")
    oe.end_run("ok")

    rows = _read_events(db_session, run_id)
    assert rows
    assert all(r.user_id is None and r.report_id is None for r in rows)
