"""Generic, report-independent durable operational-event writer (issue #446).

This module is the "reusable operational-log persistence example" the issue
asks for: a small context-local run/span API over `OperationalEvent`
(`app/models/operational_event.py`), backed by its own short-lived
PostgreSQL connection — never the caller's business `Session`. The report
pipeline (`report_generator.py`/`report_tasks.py`) is its first integration;
a future FX/capture integration can call `start_run`/`start_span` directly
with no `Report` involved at all (see `test_operational_events.py`'s
`test_capture_fx_example_has_no_report_dependency` for the worked example
Contract constraints requires).

**Concurrency model**: one run represents one task/direct-root invocation.
Within a run, attempts (one `generate_report` call each) and their spans
execute SEQUENTIALLY — this module tracks "the current attempt's
user_id/report_id" as plain mutable state on the run, not per-span state,
which is only safe because nothing in this codebase calls `generate_report`
concurrently within one task run (report_tasks.py's fan-out loop is a plain
`for`). A future concurrent fan-out would need per-attempt state threaded
explicitly rather than this module's ambient current-attempt tracking.

**Failure boundary (Design §2 / Contract constraints #2)**: every write goes
through `_write`, which never raises. A connection/statement/lock failure
logs one sanitized warning and disables further sink writes for the
CURRENT RUN ONLY (a later root run gets a fresh attempt at the sink) — it
never replaces or suppresses a caller's real exception, and it never
touches, commits, or rolls back a business `Session`.

**Durability boundary**: "durable" here means PostgreSQL acknowledged the
write, not that telemetry can never be lost — a database outage, a process
kill between events, or 90-day retention (see `cleanup_expired_events`)
are all real, accepted limits. See the module docstring precedent in
`ops_log.py` for the plain-logging predecessor this coexists with (that
module is unchanged by this issue — see Contract constraints #6, no mass
conversion of existing logging).
"""

from __future__ import annotations

import logging
import math
import os
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import Engine, create_engine, delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.operational_event import OperationalEvent

logger = logging.getLogger(__name__)

# Same pytest-DB guard shape as app.core.database.SessionLocal — this sink
# must never write to a developer's real dev/prod database just because a
# test forgot to exercise the real writer path (it deliberately does NOT
# go through the business Session/its db_session-under-test rebind, so it
# needs its own copy of the guard).
_TEST_DB_ENV = "PYTEST_CURRENT_TEST"


# ---------------------------------------------------------------------------
# Attribute allowlist (Design §4 "Minimal attributes")
# ---------------------------------------------------------------------------
# A closed, versioned set — widening it is a one-line code change (not a
# migration, since `attributes` is JSONB), but it stays a deliberate edit,
# never "whatever kwargs a call site happened to pass." Keys not listed here
# are dropped (with a debug log, never their value) rather than rejecting
# the whole event — one bad key must not cost the rest of a stage's evidence.
ALLOWED_ATTRIBUTE_KEYS = frozenset(
    {
        # trigger / path / body source (Design §3 worked examples)
        "trigger",
        "path",
        "body_source",
        "session_node",
        "report_type",
        "report_status",
        "final_status",
        "stage_state",
        "failed_stage",
        # counts / offsets
        "recipient_count",
        "recipient_index",
        "users_remaining",
        "batch_offset_ms",
        "report_ready_offset_ms",
        "cache_hit_count",
        "cache_miss_count",
        "candidate_count",
        # capture.fx (issue #509)
        "pairs_upserted",
        "pairs_failed",
        "budget_skipped_count",
        "query_count",
        "result_count",
        "external_request_count",
        "retry_count",
        "redelivered",
        # deployment / model / prompt
        "deployed_revision",
        "model",
        "prompt_version",
        "disclaimer_version",
        "analysis_framework_version",
        "locale",
        # LLM usage
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "finish_reason",
        # error / retry classification
        "error_class",
        "status_code",
        "backoff_group",
        "backoff_seconds",
        # SDK / worker config, when inspectable
        "sdk_timeout_s",
        "sdk_max_retries",
        "worker_pool",
        "worker_concurrency",
        # timing completeness
        "telemetry_overhead_ms",
        "cadence",
    }
)

_MAX_STRING_LEN = 300
_MAX_LIST_LEN = 20


def _sanitize_value(value: object) -> object | None:
    if value is None or isinstance(value, bool | int | float):
        if isinstance(value, float) and not math.isfinite(value):
            return None
        return value
    if isinstance(value, str):
        return value[:_MAX_STRING_LEN]
    if isinstance(value, list | tuple):
        return [_sanitize_value(v) for v in list(value)[:_MAX_LIST_LEN]]
    if isinstance(value, dict):
        return {
            str(k)[:_MAX_STRING_LEN]: _sanitize_value(v)
            for k, v in list(value.items())[:_MAX_LIST_LEN]
        }
    # Anything else (raw objects, exceptions, request/response bodies) is
    # exactly what Design §4's "no raw prompts/responses/unredacted
    # traceback" forbids — drop it rather than str()-ing an unknown object.
    return None


def _sanitize_attributes(attributes: dict[str, Any] | None) -> dict[str, Any]:
    if not attributes:
        return {}
    out: dict[str, Any] = {}
    for key, value in attributes.items():
        if key not in ALLOWED_ATTRIBUTE_KEYS:
            logger.debug("operational_events: dropped disallowed attribute key %r", key)
            continue
        sanitized = _sanitize_value(value)
        if sanitized is not None:
            out[key] = sanitized
    return out


def _safe_elapsed_ms(value: float | None) -> float | None:
    if value is None:
        return None
    if not math.isfinite(value) or value < 0:
        logger.warning("operational_events: rejected non-finite/negative elapsed_ms")
        return None
    return round(value, 3)


# ---------------------------------------------------------------------------
# Sink engine — independent of app.core.database's business engine
# ---------------------------------------------------------------------------

_sink_engine: Engine | None = None
_sink_engine_url: str | None = None


def _get_sink_engine() -> Engine:
    """Lazy, post-fork-safe: keyed by the CURRENT settings.database_url so a
    mid-process DB_NAME swap (e.g. the alembic_cfg test fixture) does not
    silently keep writing to a stale target — a cached engine is disposed
    and recreated whenever the URL changes, rather than requiring every
    caller to remember to call `reset_sink_engine()`.
    """
    global _sink_engine, _sink_engine_url
    settings = get_settings()
    url = settings.database_url
    if os.environ.get(_TEST_DB_ENV) and settings.DB_NAME not in url:
        # Defense in depth only — this branch is unreachable in practice
        # since `url` is built FROM settings.DB_NAME, kept for symmetry
        # with SessionLocal's guard and to fail loudly if that ever changes.
        raise RuntimeError("operational_events sink refused to bind outside the test DB")
    if _sink_engine is None or _sink_engine_url != url:
        if _sink_engine is not None:
            _sink_engine.dispose()
        _sink_engine = create_engine(
            url,
            pool_size=1,
            max_overflow=0,
            pool_timeout=0.1,
            pool_pre_ping=True,
            connect_args={
                "connect_timeout": 1,
                "options": "-c statement_timeout=250 -c lock_timeout=100",
            },
        )
        _sink_engine_url = url
    return _sink_engine


def reset_sink_engine() -> None:
    """Dispose the cached sink engine. Call alongside worker/test lifecycle
    (e.g. a post-fork hook) so a child process never inherits a live
    connection from its parent — the same reason app.core.database's
    `reset_engine` exists."""
    global _sink_engine, _sink_engine_url
    if _sink_engine is not None:
        _sink_engine.dispose()
    _sink_engine = None
    _sink_engine_url = None


# ---------------------------------------------------------------------------
# Run/span context
# ---------------------------------------------------------------------------


@dataclass
class _RunState:
    run_id: uuid.UUID
    task_id: str | None = None
    dispatch_id: uuid.UUID | None = None
    job_id: uuid.UUID | None = None
    sink_disabled: bool = False
    span_stack: list[uuid.UUID] = field(default_factory=list)
    span_starts: dict[uuid.UUID, float] = field(default_factory=dict)
    span_operations: dict[uuid.UUID, str] = field(default_factory=dict)
    span_parents: dict[uuid.UUID, uuid.UUID | None] = field(default_factory=dict)
    telemetry_overhead_ms: float = 0.0
    current_user_id: uuid.UUID | None = None
    current_report_id: uuid.UUID | None = None


_current_run: ContextVar[_RunState | None] = ContextVar("operational_events_run", default=None)


@dataclass
class SpanToken:
    """Opaque handle from `start_span`/`start_attempt`. `span_id is None`
    means "no active run" — every `end_*` call below treats that as a
    no-op, which is what makes `start_span`/`end_span` safe to call from a
    shared helper (e.g. report_llm.py) regardless of whether a report
    attempt is currently active (Contract constraints acceptance: "shared
    LLM helpers outside the integration emit no events")."""

    span_id: uuid.UUID | None
    is_root: bool = False


def current_run_id() -> uuid.UUID | None:
    state = _current_run.get()
    return state.run_id if state is not None else None


def _write(
    *,
    event_kind: str,
    operation: str,
    span_id: uuid.UUID,
    run_id: uuid.UUID,
    parent_span_id: uuid.UUID | None,
    elapsed_ms: float | None,
    outcome: str | None,
    reason_code: str | None,
    user_id: uuid.UUID | None,
    report_id: uuid.UUID | None,
    task_id: str | None,
    dispatch_id: uuid.UUID | None,
    job_id: uuid.UUID | None,
    attributes: dict[str, Any] | None,
    run_state: _RunState | None,
) -> None:
    if run_state is not None and run_state.sink_disabled:
        return
    event_id = uuid.uuid4()
    row = {
        "event_id": event_id,
        "occurred_at": datetime.now(UTC),
        "run_id": run_id,
        "span_id": span_id,
        "parent_span_id": parent_span_id,
        "event_kind": event_kind,
        "operation": operation,
        "elapsed_ms": _safe_elapsed_ms(elapsed_ms),
        "outcome": outcome,
        "reason_code": reason_code,
        "user_id": user_id,
        "report_id": report_id,
        "task_id": task_id,
        "dispatch_id": dispatch_id,
        "job_id": job_id,
        "attributes": _sanitize_attributes(attributes),
    }
    write_t0 = time.monotonic()
    try:
        engine = _get_sink_engine()
        stmt = (
            pg_insert(OperationalEvent)
            .values(**row)
            .on_conflict_do_nothing(index_elements=["event_id"])
        )
        with engine.begin() as conn:
            conn.execute(stmt)
    except Exception:
        # Sanitized: log the failure shape only, never attributes/exception
        # text (which could echo a value the allowlist was meant to keep
        # out — e.g. a driver error that embeds part of a query).
        logger.warning(
            "operational_events: sink write failed (operation=%s event_kind=%s) — "
            "disabling further writes for run_id=%s",
            operation,
            event_kind,
            run_id,
        )
        if run_state is not None:
            run_state.sink_disabled = True
        return
    if run_state is not None:
        run_state.telemetry_overhead_ms += (time.monotonic() - write_t0) * 1000


# ---------------------------------------------------------------------------
# Public run API
# ---------------------------------------------------------------------------


def start_run(
    operation: str,
    *,
    task_id: str | None = None,
    dispatch_id: uuid.UUID | None = None,
    job_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
    attributes: dict[str, Any] | None = None,
) -> None:
    """Start a new root run (one task/direct-root invocation) and make it
    the active context. Every retry/redelivery calls this again — a NEW
    run_id, never a reused one (Design §1: "A retry/redelivery keeps the
    observed task_id and uses a new run_id")."""
    run_id = uuid.uuid4()
    state = _RunState(
        run_id=run_id,
        task_id=task_id,
        dispatch_id=dispatch_id,
        job_id=job_id,
        current_user_id=user_id,
    )
    state.span_stack.append(run_id)
    state.span_starts[run_id] = time.monotonic()
    state.span_operations[run_id] = operation
    state.span_parents[run_id] = None
    _current_run.set(state)
    _write(
        event_kind="start",
        operation=operation,
        span_id=run_id,
        run_id=run_id,
        parent_span_id=None,
        elapsed_ms=None,
        outcome=None,
        reason_code=None,
        user_id=user_id,
        report_id=None,
        task_id=task_id,
        dispatch_id=dispatch_id,
        job_id=job_id,
        attributes=attributes,
        run_state=state,
    )


def end_run(
    outcome: str, *, reason_code: str | None = None, attributes: dict[str, Any] | None = None
) -> None:
    """End the active run's root span and clear the context. A killed
    process never reaches this call — its start row stays unmatched, which
    is the intended "unknown/incomplete" signal (Design §3)."""
    state = _current_run.get()
    if state is None:
        return
    t0 = state.span_starts.get(state.run_id)
    elapsed = (time.monotonic() - t0) * 1000 if t0 is not None else None
    merged = dict(attributes or {})
    # Excludes this terminal write's own overhead (Design §2 "excluding the
    # terminal write that carries it") — only prior child/start writes.
    merged.setdefault("telemetry_overhead_ms", round(state.telemetry_overhead_ms, 3))
    _write(
        event_kind="end",
        operation=state.span_operations.get(state.run_id, ""),
        span_id=state.run_id,
        run_id=state.run_id,
        parent_span_id=None,
        elapsed_ms=elapsed,
        outcome=outcome,
        reason_code=reason_code,
        user_id=state.current_user_id,
        report_id=state.current_report_id,
        task_id=state.task_id,
        dispatch_id=state.dispatch_id,
        job_id=state.job_id,
        attributes=merged,
        run_state=state,
    )
    _current_run.set(None)


# ---------------------------------------------------------------------------
# Public span API
# ---------------------------------------------------------------------------


def start_span(operation: str, *, attributes: dict[str, Any] | None = None) -> SpanToken:
    """Start a child span under the currently active run/attempt. A no-op
    (`span_id=None`) when there is no active run — safe to call from a
    shared helper that may or may not be running inside a report attempt."""
    state = _current_run.get()
    if state is None:
        return SpanToken(span_id=None)
    span_id = uuid.uuid4()
    parent = state.span_stack[-1] if state.span_stack else None
    state.span_stack.append(span_id)
    state.span_starts[span_id] = time.monotonic()
    state.span_operations[span_id] = operation
    state.span_parents[span_id] = parent
    _write(
        event_kind="start",
        operation=operation,
        span_id=span_id,
        run_id=state.run_id,
        parent_span_id=parent,
        elapsed_ms=None,
        outcome=None,
        reason_code=None,
        user_id=state.current_user_id,
        report_id=state.current_report_id,
        task_id=state.task_id,
        dispatch_id=state.dispatch_id,
        job_id=state.job_id,
        attributes=attributes,
        run_state=state,
    )
    return SpanToken(span_id=span_id)


def end_span(
    token: SpanToken,
    outcome: str,
    *,
    reason_code: str | None = None,
    attributes: dict[str, Any] | None = None,
) -> None:
    """End a span opened by `start_span`. A no-op for an inert token (no
    active run when it was created)."""
    if token.span_id is None:
        return
    state = _current_run.get()
    if state is None:
        return
    t0 = state.span_starts.get(token.span_id)
    elapsed = (time.monotonic() - t0) * 1000 if t0 is not None else None
    _write(
        event_kind="end",
        operation=state.span_operations.get(token.span_id, ""),
        span_id=token.span_id,
        run_id=state.run_id,
        parent_span_id=state.span_parents.get(token.span_id),
        elapsed_ms=elapsed,
        outcome=outcome,
        reason_code=reason_code,
        user_id=state.current_user_id,
        report_id=state.current_report_id,
        task_id=state.task_id,
        dispatch_id=state.dispatch_id,
        job_id=state.job_id,
        attributes=attributes,
        run_state=state,
    )
    # Pop only if this span is (still) the top of the stack — a span ended
    # out of strict LIFO order (shouldn't happen in the synchronous pipeline
    # this integrates with, but defensive) leaves the stack alone rather
    # than corrupting an ancestor's parent chain.
    if state.span_stack and state.span_stack[-1] == token.span_id:
        state.span_stack.pop()


def skip_span(
    operation: str, *, reason_code: str, attributes: dict[str, Any] | None = None
) -> None:
    """Record a one-shot non-entry: a stage that was never entered on this
    attempt (e.g. Pass 2 when assembly already produced the body). No
    start/end pair — `elapsed_ms` is always null (Design §3)."""
    state = _current_run.get()
    if state is None:
        return
    span_id = uuid.uuid4()
    parent = state.span_stack[-1] if state.span_stack else None
    _write(
        event_kind="skipped",
        operation=operation,
        span_id=span_id,
        run_id=state.run_id,
        parent_span_id=parent,
        elapsed_ms=None,
        outcome="skipped",
        reason_code=reason_code,
        user_id=state.current_user_id,
        report_id=state.current_report_id,
        task_id=state.task_id,
        dispatch_id=state.dispatch_id,
        job_id=state.job_id,
        attributes=attributes,
        run_state=state,
    )


def emit_retry(
    operation: str, *, reason_code: str | None = None, attributes: dict[str, Any] | None = None
) -> None:
    """Record an observed Celery-level retry/redelivery against the
    currently active run (e.g. a batch task's own `self.retry`). No-op with
    no active run."""
    state = _current_run.get()
    if state is None:
        return
    _write(
        event_kind="retry",
        operation=operation,
        span_id=uuid.uuid4(),
        run_id=state.run_id,
        parent_span_id=state.span_stack[-1] if state.span_stack else None,
        elapsed_ms=None,
        outcome=None,
        reason_code=reason_code,
        user_id=state.current_user_id,
        report_id=state.current_report_id,
        task_id=state.task_id,
        dispatch_id=state.dispatch_id,
        job_id=state.job_id,
        attributes=attributes,
        run_state=state,
    )


def emit_dispatch(
    operation: str,
    *,
    dispatch_id: uuid.UUID,
    task_id: str | None = None,
    attributes: dict[str, Any] | None = None,
) -> None:
    """Record a publication event (Celery `before_task_publish`). Written
    standalone — no run is active yet at publish time, so this creates and
    immediately closes its own tiny root record (`run_id == span_id ==
    dispatch_id`'s own fresh id, correlated to the eventual task-side run
    only via the `dispatch_id` column, per Design §1)."""
    _write(
        event_kind="dispatch",
        operation=operation,
        span_id=dispatch_id,
        run_id=dispatch_id,
        parent_span_id=None,
        elapsed_ms=None,
        outcome=None,
        reason_code=None,
        user_id=None,
        report_id=None,
        task_id=task_id,
        dispatch_id=dispatch_id,
        job_id=None,
        attributes=attributes,
        run_state=None,
    )


def set_report_id(report_id: uuid.UUID) -> None:
    """Attach `report_id` to the current attempt once it becomes available
    (a fresh `Report` row's id is only known after its own `session.flush()`
    — everything before that point in `generate_report` has no report_id to
    carry). No-op with no active run. Emits an explicit `context` event so a
    reader can see exactly when the association was made, in addition to
    every later event on this run/span carrying it directly."""
    state = _current_run.get()
    if state is None:
        return
    state.current_report_id = report_id
    _write(
        event_kind="context",
        operation="report.context",
        span_id=uuid.uuid4(),
        run_id=state.run_id,
        parent_span_id=state.span_stack[-1] if state.span_stack else None,
        elapsed_ms=None,
        outcome=None,
        reason_code=None,
        user_id=state.current_user_id,
        report_id=report_id,
        task_id=state.task_id,
        dispatch_id=state.dispatch_id,
        job_id=state.job_id,
        attributes=None,
        run_state=state,
    )


# ---------------------------------------------------------------------------
# Attempt API — the "inherit task context or create direct root" seam
# (Design §3, report_generator.py)
# ---------------------------------------------------------------------------


def start_attempt(
    operation: str,
    *,
    user_id: uuid.UUID,
    task_id: str | None = None,
    dispatch_id: uuid.UUID | None = None,
    job_id: uuid.UUID | None = None,
    attributes: dict[str, Any] | None = None,
) -> SpanToken:
    """Start one `generate_report` attempt. If a run is already active
    (this call happened inside a Celery task that already called
    `start_run`), the attempt is a child SPAN of that run — matching
    Design §1's "a task invocation owns run_id; each generate_report call
    has its own root child span". With no active run (a direct admin/
    self-service call with no task wrapper around it, or a test), this
    call itself becomes a fresh standalone ROOT RUN.

    Sets `current_user_id` on the active run/attempt immediately — unlike
    `report_id`, `user_id` is always known at a `generate_report` call's
    very entry."""
    state = _current_run.get()
    if state is not None:
        state.current_user_id = user_id
        state.current_report_id = None
        return start_span(operation, attributes=attributes)
    start_run(
        operation,
        task_id=task_id,
        dispatch_id=dispatch_id,
        job_id=job_id,
        user_id=user_id,
        attributes=attributes,
    )
    root_id = current_run_id()
    return SpanToken(span_id=root_id, is_root=True)


def end_attempt(
    token: SpanToken,
    outcome: str,
    *,
    reason_code: str | None = None,
    attributes: dict[str, Any] | None = None,
) -> None:
    if token.is_root:
        end_run(outcome, reason_code=reason_code, attributes=attributes)
    else:
        end_span(token, outcome, reason_code=reason_code, attributes=attributes)


@contextmanager
def operation_span(operation: str, **attributes: Any) -> Iterator[SpanToken]:
    """Convenience wrapper for a span whose extent is exactly one `with`
    block: outcome is `ok` unless the block raises, in which case it is
    `failed` (reason_code = the exception's class name) and the exception
    still propagates unchanged — this never converts a business exception
    into a swallowed telemetry failure."""
    token = start_span(operation, attributes=attributes)
    try:
        yield token
    except Exception as exc:
        end_span(token, "failed", reason_code=type(exc).__name__)
        raise
    else:
        end_span(token, "ok")


# ---------------------------------------------------------------------------
# Retention (Design §4 "Confirmed retention: 90 days")
# ---------------------------------------------------------------------------

RETENTION_DAYS = 90
_CLEANUP_BATCH_SIZE = 1000


def cleanup_expired_events(
    session: Session,
    *,
    retention_days: int = RETENTION_DAYS,
    batch_size: int = _CLEANUP_BATCH_SIZE,
    now: datetime | None = None,
) -> int:
    """Delete `operational_events` rows older than `retention_days`, in
    bounded batches, via the caller's own session (this is a business
    maintenance operation, not a telemetry write — it goes through the
    normal `SessionLocal`, unlike every write above). Returns the total
    rows deleted. Exact-cutoff rows are kept (`recorded_at < cutoff`, not
    `<=`). `now` defaults to the real clock; a caller (tests) may pin it
    to make the cutoff boundary deterministic."""
    cutoff = (now or datetime.now(UTC)) - timedelta(days=retention_days)
    total = 0
    while True:
        # Select concrete ids first (on the SAME session the delete below
        # runs on), then delete exactly those — simpler and safer than a
        # correlated subquery for a batched delete-oldest-N loop.
        ids = (
            session.execute(
                select(OperationalEvent.event_id)
                .where(OperationalEvent.recorded_at < cutoff)
                .limit(batch_size)
            )
            .scalars()
            .all()
        )
        if not ids:
            break
        result = cast(
            CursorResult[Any],
            session.execute(delete(OperationalEvent).where(OperationalEvent.event_id.in_(ids))),
        )
        session.commit()
        deleted = result.rowcount or 0
        total += deleted
        if len(ids) < batch_size:
            break
    return total
