"""Snapshot recovery — replay a frozen payload, or catch up unconditionally
(issue #373; issue #497 removed the composition-fingerprint safety gate).

Two ways a tracking day can come back, in this order of preference:

1. **Replay**: the day has an outbox row (payload frozen, never published, or
   published into rows that are no longer there). Applying that payload
   reproduces the capture the book actually had that evening, which is the
   only honest way to restore a day after the holdings have moved.
2. **Recompute**: the day has no frozen payload (typically the task never
   ran, or FX arrived late and left it `skipped_deps`). Recomputing from
   *today's* holdings is allowed unconditionally, bounded only by
   `CATCHUP_LOOKBACK_DAYS` — issue #497 removed the composition-fingerprint
   check that used to refuse this when the live book didn't match the last
   frozen evidence (see issue #492: that check produced structural false
   positives for `pricing_mode="auto"` holdings, silently blocking
   legitimate recovery). The residual risk this accepts — a genuinely moved
   book gets recomputed as if it were the missing day's real book, with no
   disclosure flag distinguishing it from an ordinary same-day capture — is
   bounded by `CATCHUP_LOOKBACK_DAYS`, not eliminated.

Both paths go through `apply_outbox_row`, so a recovered day is published
exactly like a normal one (rows + `complete` batch + outbox `applied`, one
transaction).

Detection is #372's job (the 21:30 ET capture-health probe); this module only
recovers, and emits structured logs rather than alerts.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.timezones import today_et
from app.models.portfolio_snapshot_batch import PortfolioSnapshotBatch
from app.services.portfolio_history import (
    apply_outbox_row,
    snapshot_fanout_user_ids,
    stage_user_snapshot,
)
from app.services.snapshot_outbox import (
    OutboxPayloadError,
    get_outbox_row,
    mark_outbox_failed,
)

logger = logging.getLogger(__name__)

# How far back a day may be rebuilt from live holdings. Bounded on purpose:
# the further back, the likelier the book moved since — recomputing then
# reflects today's composition, not the missing day's real one.
CATCHUP_LOOKBACK_DAYS = 7

# First calendar day on which weekend snapshot capture is a live Beat
# target (issue #487 Requirement 1). Unattended recover_portfolio_snapshots
# must not invent weekend rows before this date — those gaps belong to the
# separately authorized backfill_weekend_gaps.py (Requirement 6).
WEEKEND_CAPTURE_ENABLED_FROM = date(2026, 9, 15)

# Ops-triggered recovery may look years back for frozen payloads (they are not
# age-limited); this only bounds one synchronous request.
MAX_RECOVERY_WINDOW_DAYS = 90


@dataclass(frozen=True)
class SnapshotRecoveryReport:
    replayed: int = 0
    recomputed: int = 0
    already_complete: int = 0
    skipped_old: int = 0
    skipped_deps: int = 0
    failed: int = 0
    dates: tuple[str, ...] = field(default_factory=tuple)


def _recover_user_day(
    session: Session, user_id: uuid.UUID, target: date, oldest_recomputable: date
) -> str:
    outbox_row = get_outbox_row(session, user_id, target)
    if outbox_row is not None:
        if outbox_row.status == "failed":
            logger.error(
                "snapshot_recovery: user=%s date=%s outbox payload is marked failed; "
                "needs a manual decision, skipping",
                user_id,
                target,
            )
            return "failed"
        # `computed` (never published) or `applied` into rows that are gone.
        try:
            apply_outbox_row(session, outbox_row)
        except OutboxPayloadError:
            mark_outbox_failed(outbox_row)
            session.flush()
            logger.error(
                "snapshot_recovery: user=%s date=%s outbox payload could not be decoded; marked failed",
                user_id,
                target,
            )
            return "failed"
        logger.warning(
            "snapshot_recovery: replayed frozen payload user=%s date=%s", user_id, target
        )
        return "replayed"

    if target < oldest_recomputable:
        logger.warning(
            "snapshot_recovery: user=%s date=%s has no frozen payload and is outside the "
            "%d-day recompute window; not inventing it",
            user_id,
            target,
            CATCHUP_LOOKBACK_DAYS,
        )
        return "skipped_old"

    _written, status = stage_user_snapshot(session, user_id, target)
    if status != "computed":
        logger.warning(
            "snapshot_recovery: user=%s date=%s still lacks its FX dependency; leaving skipped_deps",
            user_id,
            target,
        )
        return "skipped_deps"
    frozen = get_outbox_row(session, user_id, target)
    if frozen is None:  # pragma: no cover - stage always freezes on "computed"
        raise OutboxPayloadError("staged payload missing for a computed user-day")
    apply_outbox_row(session, frozen)
    logger.warning("snapshot_recovery: recomputed user=%s date=%s", user_id, target)
    return "recomputed"


def recover_portfolio_snapshots(
    session: Session,
    start_date: date,
    end_date: date,
    *,
    today: date | None = None,
) -> SnapshotRecoveryReport:
    """Recover missing snapshot days in `[start_date, end_date]` (inclusive).

    Every calendar day from `WEEKEND_CAPTURE_ENABLED_FROM` (issue #487):
    weekends on/after that date are legitimate portfolio capture targets,
    so a failed Saturday/Sunday is recoverable the same way as a weekday.
    Earlier weekend gaps stay untouched here — they are the separately
    authorized `backfill_weekend_gaps.py` window. Market-data pipelines
    still use `capture_health.expected_capture_date` (last Mon-Fri).

    Commits per date, so one bad day cannot roll back another's recovery.
    """
    if end_date < start_date:
        raise ValueError("end_date must not precede start_date")
    if (end_date - start_date).days > MAX_RECOVERY_WINDOW_DAYS:
        raise ValueError(f"recovery window may not exceed {MAX_RECOVERY_WINDOW_DAYS} days")

    reference_today = today or today_et()
    oldest_recomputable = reference_today - timedelta(days=CATCHUP_LOOKBACK_DAYS)
    user_ids = snapshot_fanout_user_ids(session)

    replayed = recomputed = already_complete = 0
    skipped_old = skipped_deps = failed = 0
    touched_dates: list[str] = []

    target = start_date
    while target <= end_date:
        if target.weekday() >= 5 and target < WEEKEND_CAPTURE_ENABLED_FROM:
            target += timedelta(days=1)
            continue
        complete_ids = set(
            session.execute(
                select(PortfolioSnapshotBatch.user_id).where(
                    PortfolioSnapshotBatch.snapshot_date == target,
                    PortfolioSnapshotBatch.status == "complete",
                )
            ).scalars()
        )
        needing = [user_id for user_id in user_ids if user_id not in complete_ids]
        already_complete += len(user_ids) - len(needing)
        if needing:
            touched_dates.append(target.isoformat())
        for user_id in needing:
            outcome = _recover_user_day(session, user_id, target, oldest_recomputable)
            if outcome == "replayed":
                replayed += 1
            elif outcome == "recomputed":
                recomputed += 1
            elif outcome == "failed":
                failed += 1
            elif outcome == "skipped_old":
                skipped_old += 1
            elif outcome == "skipped_deps":
                skipped_deps += 1
        session.commit()
        target += timedelta(days=1)

    report = SnapshotRecoveryReport(
        replayed=replayed,
        recomputed=recomputed,
        already_complete=already_complete,
        skipped_old=skipped_old,
        skipped_deps=skipped_deps,
        failed=failed,
        dates=tuple(touched_dates),
    )
    logger.info(
        "snapshot_recovery: window=%s..%s replayed=%d recomputed=%d already_complete=%d "
        "skipped_old=%d skipped_deps=%d failed=%d",
        start_date,
        end_date,
        replayed,
        recomputed,
        already_complete,
        skipped_old,
        skipped_deps,
        failed,
    )
    return report
