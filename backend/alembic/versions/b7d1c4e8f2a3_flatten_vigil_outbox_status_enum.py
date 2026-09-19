"""flatten vigil outbox status enum

Issue #525 (Vigil R0, #516 finding 4): `vigil_outbox.status` collapses from
six values (`pending/leased/accepted/failed/unknown/cancelled`) to the
three-state machine the dispatcher actually implements now
(`pending/accepted/failed`). Values below are a FROZEN SNAPSHOT of
`VALID_VIGIL_OUTBOX_STATUSES` (app/models/vigil.py) as of this migration's
authoring date — deliberately NOT imported live, matching the precedent set
by `6cd7544f63cf` / `e1f2a3b4c5d6`: a migration is an immutable historical
record. Any later widening is a NEW migration, not an edit to this file.

The three dropped values are mapped onto the new vocabulary before the
constraint is swapped, because P3.1 (#456) is deployed (2026-09-18) and a
row written by it may still carry one:

- `leased`  -> `pending`: identical meaning — a claimed-but-unfinished
  attempt. The row keeps its `lease_until` (or loses it 60s later, after
  which the next sweep re-claims it with the same idempotency key).
- `unknown` -> `failed`: the old machine kept retrying these inside a 23h
  schedule window; the new machine has no separate retryable-unknown state.
  Terminal is the conservative landing: provider facts (`provider_id`,
  `accepted_at`) are preserved, and the frozen payload is cleared exactly as
  the expiry sweep would have cleared it at 24h. A row that was actually
  accepted provider-side but whose finalize never committed is `leased`, not
  `unknown`, so the replay path (`_lease_one` -> same idempotency key) is
  untouched.
- `cancelled` -> `failed`: identical meaning (caller gave up; payload
  already cleared).

Payload clearing for the mapped `unknown` rows is the only data change, and
it removes encrypted transient mail intents, never business state.

Revision ID: b7d1c4e8f2a3
Revises: a9c3e7f1b204
Create Date: 2026-09-19

"""

from collections.abc import Sequence

from alembic import op

revision: str = "b7d1c4e8f2a3"
down_revision: str | None = "a9c3e7f1b204"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

# Frozen snapshot — see module docstring. Do not import live constants here.
_VALID_STATUSES = ("pending", "accepted", "failed")
# Pre-#525 vocabulary, mapped in upgrade() before the CHECK is replaced.
_LEGACY_STATUSES = ("pending", "leased", "accepted", "failed", "unknown", "cancelled")


def _in_list_sql(column: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"


def upgrade() -> None:
    op.execute(
        "UPDATE vigil_outbox SET status = 'failed', payload_cipher = NULL, "
        "payload_sha256 = NULL, next_attempt_at = NULL, lease_until = NULL "
        "WHERE status = 'unknown'"
    )
    op.execute("UPDATE vigil_outbox SET status = 'pending' WHERE status = 'leased'")
    op.execute("UPDATE vigil_outbox SET status = 'failed' WHERE status = 'cancelled'")
    # Bare constraint name (not "ck_vigil_outbox_status") — target_metadata's
    # naming_convention (app/models/base.py) applies the ck_%(table_name)s_
    # %(constraint_name)s prefix; a pre-rendered name doubles it (same gotcha
    # documented in 6cd7544f63cf / e1f2a3b4c5d6).
    op.drop_constraint("status", "vigil_outbox", type_="check")
    op.create_check_constraint("status", "vigil_outbox", _in_list_sql("status", _VALID_STATUSES))


def downgrade() -> None:
    # Restores the pre-#525 vocabulary so the constraint matches the old
    # code. The mapping above is not reversed: rows that were `unknown` or
    # `cancelled` stay `failed`.
    op.drop_constraint("status", "vigil_outbox", type_="check")
    op.create_check_constraint("status", "vigil_outbox", _in_list_sql("status", _LEGACY_STATUSES))
