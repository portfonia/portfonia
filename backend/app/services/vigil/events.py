"""Durable Vigil operational events (issue #527, #516 finding 6).

Replaces `vigil_audit_events`: the transitions that used to be inserted into
that bespoke table now go to the generic `operational_events` sink (#446).
One call site per former audit write (`arm.py`, `cycles.py`), each with the
same operation name as the audit row's `action`, plus the actor that caused
the transition (`owner` / `token` / `system` / `ops` — the old
`actor_type`).

Two properties are deliberate, and the first one was measured against a
real Postgres rather than assumed:

- **No `user_id` is attached.** `operational_events.user_id` is a real FK
  (ON DELETE CASCADE), so the sink's INSERT takes a `FOR KEY SHARE` lock on
  that `users` row — while every Vigil mutation already holds the same row
  `FOR UPDATE` (the User -> vault lock order, #450 Design section 3). The
  sink's own `lock_timeout` (100ms) then refuses the write and disables the
  sink for that run, i.e. the event is dropped: emitting with `user_id` gave
  0 rows, without it 2 (start+end). Consequences to accept openly: a user
  purge does not cascade these rows (they age out within the table's 90-day
  retention), and a reader correlates them to an owner through `vault_id`.
  Nothing personal is written — vault/cycle/round ids, phase names, actor.
- **Emission stays inside the caller's transaction**, at the same point the
  audit insert sat, so a sink failure never breaks the Vigil action (the
  sink's own failure boundary). The flip side of the sink's independent
  connection: unlike the old same-transaction audit row, an event is not
  rolled back with a business rollback. The business rows remain the
  authoritative record; these events are the operational trail.

A run pair (start + end) is written per event because that is the sink's
shape for a direct-root invocation (`start_run`) — an HTTP request or a scan
step is exactly that — and adding a one-shot `event_kind` would have meant a
migration on the shared table. When a run is already active (a task that
wraps the call), the event becomes a child span of it instead, so an outer
run is never clobbered.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from app.core import operational_events as oe

__all__ = ["emit_vigil_event"]


def emit_vigil_event(
    operation: str,
    *,
    actor: str,
    vault_id: UUID,
    from_phase: str | None = None,
    to_phase: str | None = None,
    cycle_id: UUID | None = None,
    round_id: UUID | None = None,
    level: int | None = None,
) -> None:
    """Write one durable Vigil transition event. Never raises."""
    attributes: dict[str, Any] = {"actor": actor, "vault_id": str(vault_id)}
    if from_phase is not None:
        attributes["from_phase"] = from_phase
    if to_phase is not None:
        attributes["to_phase"] = to_phase
    if cycle_id is not None:
        attributes["cycle_id"] = str(cycle_id)
    if round_id is not None:
        attributes["round_id"] = str(round_id)
    if level is not None:
        attributes["level"] = level

    if oe.current_run_id() is None:
        oe.start_run(operation, attributes=attributes)
        oe.end_run("ok")
        return
    token = oe.start_span(operation, attributes=attributes)
    oe.end_span(token, "ok")
