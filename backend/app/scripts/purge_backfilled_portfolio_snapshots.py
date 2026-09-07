"""One-off ops cleanup: delete known-bad `portfolio_value_snapshots` rows
written by the now-deleted composition-replay backfill (issue #366).

Phase 1's `backfill_portfolio_value_history.py` (deleted by this same
change) derived a position's chart start date from "earliest ticker price
`price_snapshots` happens to have," not from when the user was actually
being tracked — production verification found ~35,605 such rows across 4
accounts with fictional start dates in 2024-11/12 for a product that only
existed since mid-2026 (full incident: vault `Portfolio_Pfmc.md` §6).

This is an **ops data-cleanup script, not a user-facing correction
product** (issue #366 contract, section A): it deletes rows already marked
`is_backfilled=True` and removes any `portfolio_snapshot_batches` row that
is left an orphan — a `(user_id, snapshot_date)` batch marked `complete`
purely because of now-deleted backfilled rows, with zero real snapshot rows
remaining for that day. Leaving such a batch row behind would make
`GET /portfolio/performance` read that day as a legitimate $0
zero-holdings day (the same code path a real "sold everything" day uses)
instead of correctly having no data for it at all.

Never re-run the deleted backfill script after this — `tracking_start` will
then correctly land on the user's first REAL snapshot day.

    python -m app.scripts.purge_backfilled_portfolio_snapshots          # dry run
    python -m app.scripts.purge_backfilled_portfolio_snapshots --apply  # commit changes

Add `--user-id <uuid>` (repeatable) to scope to specific users; omitted
means every user with any `is_backfilled=True` row.
"""

from __future__ import annotations

import argparse
import uuid
from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.models.portfolio_snapshot_batch import PortfolioSnapshotBatch
from app.models.portfolio_value_snapshot import PortfolioValueSnapshot


def purge_backfilled_portfolio_snapshots(
    session: Session, *, apply_changes: bool, user_ids: list[uuid.UUID] | None = None
) -> dict[str, int]:
    """Delete `is_backfilled=True` snapshot rows and their now-orphaned
    batch rows. Returns counts for the caller to print/log. Mutates
    `session` in place when `apply_changes` (caller commits); a dry run
    only counts/prints what would change."""
    query = select(PortfolioValueSnapshot).where(PortfolioValueSnapshot.is_backfilled.is_(True))
    if user_ids:
        query = query.where(PortfolioValueSnapshot.user_id.in_(user_ids))
    bad_rows = list(session.execute(query).scalars())

    affected_days: set[tuple[uuid.UUID, object]] = {(r.user_id, r.snapshot_date) for r in bad_rows}
    per_user_count: dict[uuid.UUID, int] = defaultdict(int)
    for r in bad_rows:
        per_user_count[r.user_id] += 1

    tag = "APPLY" if apply_changes else "DRY-RUN"
    for user_id, count in sorted(per_user_count.items(), key=lambda kv: str(kv[0])):
        print(f"[{tag}] user {user_id}: {count} backfilled snapshot row(s)")

    if apply_changes:
        for r in bad_rows:
            session.delete(r)
        session.flush()

    # A batch row is an orphan only if, after removing the backfilled rows
    # above, it has ZERO real snapshot rows left for that (user, date) —
    # never delete a batch that still has real data alongside what was
    # purged.
    orphan_batches: list[PortfolioSnapshotBatch] = []
    for user_id, snapshot_date in affected_days:
        remaining = session.execute(
            select(PortfolioValueSnapshot.id).where(
                PortfolioValueSnapshot.user_id == user_id,
                PortfolioValueSnapshot.snapshot_date == snapshot_date,
                PortfolioValueSnapshot.is_backfilled.is_(False),
            )
        ).first()
        if remaining is not None:
            continue
        batch = session.execute(
            select(PortfolioSnapshotBatch).where(
                PortfolioSnapshotBatch.user_id == user_id,
                PortfolioSnapshotBatch.snapshot_date == snapshot_date,
            )
        ).scalar_one_or_none()
        if batch is not None:
            orphan_batches.append(batch)

    for batch in orphan_batches:
        print(
            f"[{tag}] orphan batch: user {batch.user_id} date {batch.snapshot_date} "
            f"(status={batch.status!r}) — no real snapshot rows remain"
        )
        if apply_changes:
            session.delete(batch)

    print(
        f"[OK] {len(bad_rows)} backfilled snapshot row(s), "
        f"{len(orphan_batches)} orphan batch row(s), "
        f"{len(per_user_count)} user(s) affected "
        f"({'applied' if apply_changes else 'dry run — no changes committed'})"
    )
    return {
        "snapshot_rows": len(bad_rows),
        "orphan_batches": len(orphan_batches),
        "users": len(per_user_count),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Commit changes (default: dry run)")
    parser.add_argument(
        "--user-id",
        action="append",
        dest="user_ids",
        help="Scope to this user id (repeatable); omit for every affected user",
    )
    args = parser.parse_args()
    user_ids = [uuid.UUID(u) for u in args.user_ids] if args.user_ids else None

    with SessionLocal() as session:
        result = purge_backfilled_portfolio_snapshots(
            session, apply_changes=args.apply, user_ids=user_ids
        )
        if args.apply and result["snapshot_rows"]:
            session.commit()


if __name__ == "__main__":
    main()
