"""One-off weekend snapshot backfill (issue #487 Requirement 6).

Writes real `portfolio_value_snapshots` rows for Saturday/Sunday dates in
a bounded window, using the live `stage_user_snapshot` + `apply_outbox_row`
path, gated by `snapshot_recovery.recompute_is_safe`. Does not call
`recover_portfolio_snapshots` (that wrapper still bounds recompute to
`CATCHUP_LOOKBACK_DAYS` from today, which is the wrong bound for this
explicit historical window).

    python -m app.scripts.backfill_weekend_gaps
    python -m app.scripts.backfill_weekend_gaps --start-date 2026-09-08 --end-date 2026-09-14

Never run this against production without a separate, explicit
authorization. Merge of the PR that adds the script is not that
authorization.
"""

from __future__ import annotations

import argparse
import logging
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.core.timezones import today_et
from app.models.portfolio_snapshot_batch import PortfolioSnapshotBatch
from app.services.portfolio_history import (
    apply_outbox_row,
    snapshot_fanout_user_ids,
    stage_user_snapshot,
)
from app.services.snapshot_outbox import get_outbox_row
from app.services.snapshot_recovery import (
    latest_frozen_evidence_date,
    recompute_is_safe,
)

logger = logging.getLogger(__name__)

_DEFAULT_START = date(2026, 9, 8)


@dataclass(frozen=True)
class WeekendGapSkip:
    user_id: uuid.UUID
    snapshot_date: date
    reason: str


@dataclass
class WeekendGapBackfillReport:
    backfilled: int = 0
    already_complete: int = 0
    skipped_unsafe: int = 0
    skipped_deps: int = 0
    dates: tuple[str, ...] = field(default_factory=tuple)
    skipped: tuple[WeekendGapSkip, ...] = field(default_factory=tuple)


def _weekend_dates(start_date: date, end_date: date) -> list[date]:
    if end_date < start_date:
        raise ValueError("end_date must not precede start_date")
    days: list[date] = []
    cursor = start_date
    while cursor <= end_date:
        if cursor.weekday() >= 5:
            days.append(cursor)
        cursor += timedelta(days=1)
    return days


def _unsafe_reason(session: Session, user_id: uuid.UUID, target: date) -> str:
    if latest_frozen_evidence_date(session, user_id, target) is None:
        return "no_frozen_evidence"
    return "composition_changed"


def backfill_weekend_gaps(
    session: Session, start_date: date, end_date: date
) -> WeekendGapBackfillReport:
    """Backfill Saturday/Sunday dates in `[start_date, end_date]` (inclusive).

    A row is written for `(user_id, date)` only when `recompute_is_safe`
    is True. Commits per date so one bad day cannot roll back another.
    """
    weekend_days = _weekend_dates(start_date, end_date)
    user_ids = snapshot_fanout_user_ids(session)
    backfilled = already_complete = skipped_unsafe = skipped_deps = 0
    skipped: list[WeekendGapSkip] = []
    touched: list[str] = []

    for target in weekend_days:
        complete_ids = set(
            session.execute(
                select(PortfolioSnapshotBatch.user_id).where(
                    PortfolioSnapshotBatch.snapshot_date == target,
                    PortfolioSnapshotBatch.status == "complete",
                )
            ).scalars()
        )
        for user_id in user_ids:
            if user_id in complete_ids:
                already_complete += 1
                continue
            if not recompute_is_safe(session, user_id, target):
                reason = _unsafe_reason(session, user_id, target)
                skipped_unsafe += 1
                skipped.append(WeekendGapSkip(user_id, target, reason))
                logger.warning(
                    "backfill_weekend_gaps: skip unsafe user=%s date=%s reason=%s",
                    user_id,
                    target,
                    reason,
                )
                continue
            _written, status = stage_user_snapshot(session, user_id, target)
            if status != "computed":
                skipped_deps += 1
                skipped.append(WeekendGapSkip(user_id, target, "skipped_deps"))
                logger.warning(
                    "backfill_weekend_gaps: skip deps user=%s date=%s",
                    user_id,
                    target,
                )
                continue
            frozen = get_outbox_row(session, user_id, target)
            if frozen is None:  # pragma: no cover - stage always freezes on computed
                skipped_deps += 1
                skipped.append(WeekendGapSkip(user_id, target, "missing_outbox"))
                continue
            apply_outbox_row(session, frozen)
            backfilled += 1
        touched.append(target.isoformat())
        session.commit()

    report = WeekendGapBackfillReport(
        backfilled=backfilled,
        already_complete=already_complete,
        skipped_unsafe=skipped_unsafe,
        skipped_deps=skipped_deps,
        dates=tuple(touched),
        skipped=tuple(skipped),
    )
    logger.info(
        "backfill_weekend_gaps: window=%s..%s weekends=%d backfilled=%d "
        "already_complete=%d skipped_unsafe=%d skipped_deps=%d",
        start_date,
        end_date,
        len(weekend_days),
        backfilled,
        already_complete,
        skipped_unsafe,
        skipped_deps,
    )
    return report


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-date", type=_parse_date, default=_DEFAULT_START)
    parser.add_argument(
        "--end-date",
        type=_parse_date,
        default=today_et() - timedelta(days=1),
        help="Inclusive end (default: yesterday, the day before this ships).",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    with SessionLocal() as session:
        report = backfill_weekend_gaps(session, args.start_date, args.end_date)
    print(
        f"backfilled={report.backfilled} already_complete={report.already_complete} skipped_unsafe={report.skipped_unsafe} skipped_deps={report.skipped_deps}"
    )
    for item in report.skipped:
        print(f"skipped user={item.user_id} date={item.snapshot_date} reason={item.reason}")


if __name__ == "__main__":
    main()
