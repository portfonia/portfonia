"""Snapshot recovery — replay a frozen payload, or catch up safely (issue #373).

Two ways a tracking day can come back, in this order of preference:

1. **Replay**: the day has an outbox row (payload frozen, never published, or
   published into rows that are no longer there). Applying that payload
   reproduces the capture the book actually had that evening, which is the
   only honest way to restore a day after the holdings have moved.
2. **Guarded recompute**: the day has no frozen payload (typically the task
   never ran, or FX arrived late and left it `skipped_deps`). Recomputing from
   *today's* holdings is allowed only inside `CATCHUP_LOOKBACK_DAYS` and only
   when the live book can be shown to be unchanged since the latest frozen
   evidence — the same composition fingerprint as of the last `complete`
   snapshot day. Anything else is skipped and logged; it is never invented.

Both paths go through `apply_outbox_row`, so a recovered day is published
exactly like a normal one (rows + `complete` batch + outbox `applied`, one
transaction).

A change-and-revert inside the fingerprint window is undetectable without a
holdings CDC (explicitly out of scope for #373) — that residual is the price
of not building one, and it is why the recompute window is deliberately short.

Detection is #372's job (the 21:30 ET capture-health probe); this module only
recovers, and emits structured logs rather than alerts.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.holding import Holding
from app.models.portfolio_snapshot_batch import PortfolioSnapshotBatch
from app.models.portfolio_value_snapshot import PortfolioValueSnapshot
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
# the further back, the likelier the book moved and moved back, which no
# fingerprint can see without a holdings CDC.
CATCHUP_LOOKBACK_DAYS = 7

# Ops-triggered recovery may look years back for frozen payloads (they are not
# age-limited); this only bounds one synchronous request.
MAX_RECOVERY_WINDOW_DAYS = 90

# Composition, not valuation: these are the fields that make the frozen rows
# "the same book". Everything price/FX-derived (market_value,
# market_value_base, data_quality, fx/price as-of, base_currency) is expected
# to differ day to day and is deliberately excluded.
_COMPOSITION_FIELDS = (
    "holding_id",
    "ticker",
    "fund_code",
    "pricing_mode",
    "capture_supported",
    "currency",
    "shares",
    "current_value",
    "market",
    "broker",
    "account",
    "portfolio",
    "asset_class",
)

# The live side's primary key is `id`; the frozen row's soft reference is
# `holding_id`. Everything else shares a name.
_LIVE_ATTRS = {"holding_id": "id"}


@dataclass(frozen=True)
class SnapshotRecoveryReport:
    replayed: int = 0
    recomputed: int = 0
    already_complete: int = 0
    skipped_unsafe: int = 0
    skipped_old: int = 0
    skipped_deps: int = 0
    failed: int = 0
    dates: tuple[str, ...] = field(default_factory=tuple)


def _canonical(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, Decimal):
        # Value equality, not string equality: 10.50 and 10.5 are one book.
        return format(value.normalize(), "f")
    if isinstance(value, uuid.UUID):
        return str(value)
    return str(value)


def _fingerprint(items: list[list[str]]) -> str:
    serialized = json.dumps(sorted(items), separators=(",", ":"))
    return hashlib.sha256(serialized.encode()).hexdigest()


def live_book_fingerprint(session: Session, user_id: uuid.UUID) -> str:
    holdings = session.execute(select(Holding).where(Holding.user_id == user_id)).scalars()
    return _fingerprint(
        [
            [
                _canonical(getattr(h, _LIVE_ATTRS.get(field, field), None))
                for field in _COMPOSITION_FIELDS
            ]
            for h in holdings
        ]
    )


def frozen_book_fingerprint(session: Session, user_id: uuid.UUID, snapshot_date: date) -> str:
    rows = session.execute(
        select(PortfolioValueSnapshot).where(
            PortfolioValueSnapshot.user_id == user_id,
            PortfolioValueSnapshot.snapshot_date == snapshot_date,
            PortfolioValueSnapshot.is_backfilled.is_(False),
        )
    ).scalars()
    return _fingerprint(
        [[_canonical(getattr(r, f, None)) for f in _COMPOSITION_FIELDS] for r in rows]
    )


def latest_frozen_evidence_date(session: Session, user_id: uuid.UUID, before: date) -> date | None:
    """Latest day whose book we can still read back (a `complete` batch)."""
    value = session.execute(
        select(func.max(PortfolioSnapshotBatch.snapshot_date)).where(
            PortfolioSnapshotBatch.user_id == user_id,
            PortfolioSnapshotBatch.status == "complete",
            PortfolioSnapshotBatch.snapshot_date < before,
        )
    ).scalar_one()
    return value if isinstance(value, date) else None


def recompute_is_safe(session: Session, user_id: uuid.UUID, target: date) -> bool:
    """True only when today's book demonstrably equals the book on `target`.

    The book on `target` itself is unknown (that is the missing day), so this
    compares live holdings against the newest frozen evidence *before* it: if
    they match, composition has not moved since that day, hence not between
    it and `target` either.
    """
    reference = latest_frozen_evidence_date(session, user_id, target)
    if reference is None:
        return False
    return frozen_book_fingerprint(session, user_id, reference) == live_book_fingerprint(
        session, user_id
    )


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

    if not recompute_is_safe(session, user_id, target):
        logger.warning(
            "snapshot_recovery: user=%s date=%s has no frozen payload and the book has moved "
            "since the last frozen evidence; skipping instead of recomputing",
            user_id,
            target,
        )
        return "skipped_unsafe"

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
    logger.warning(
        "snapshot_recovery: recomputed user=%s date=%s from an unchanged book", user_id, target
    )
    return "recomputed"


def recover_portfolio_snapshots(
    session: Session,
    start_date: date,
    end_date: date,
    *,
    today: date | None = None,
) -> SnapshotRecoveryReport:
    """Recover missing snapshot days in `[start_date, end_date]` (inclusive).

    Weekdays only — the daily capture runs Mon-Fri, so that is the set of days
    that can be missing (no market-holiday calendar exists here; see
    `capture_health.expected_capture_date` for the same convention).

    Commits per date, so one bad day cannot roll back another's recovery.
    """
    if end_date < start_date:
        raise ValueError("end_date must not precede start_date")
    if (end_date - start_date).days > MAX_RECOVERY_WINDOW_DAYS:
        raise ValueError(f"recovery window may not exceed {MAX_RECOVERY_WINDOW_DAYS} days")

    reference_today = today or date.today()
    oldest_recomputable = reference_today - timedelta(days=CATCHUP_LOOKBACK_DAYS)
    user_ids = snapshot_fanout_user_ids(session)

    replayed = recomputed = already_complete = 0
    skipped_unsafe = skipped_old = skipped_deps = failed = 0
    touched_dates: list[str] = []

    target = start_date
    while target <= end_date:
        if target.weekday() < 5:
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
                else:
                    skipped_unsafe += 1
            session.commit()
        target += timedelta(days=1)

    report = SnapshotRecoveryReport(
        replayed=replayed,
        recomputed=recomputed,
        already_complete=already_complete,
        skipped_unsafe=skipped_unsafe,
        skipped_old=skipped_old,
        skipped_deps=skipped_deps,
        failed=failed,
        dates=tuple(touched_dates),
    )
    logger.info(
        "snapshot_recovery: window=%s..%s replayed=%d recomputed=%d already_complete=%d "
        "skipped_unsafe=%d skipped_old=%d skipped_deps=%d failed=%d",
        start_date,
        end_date,
        replayed,
        recomputed,
        already_complete,
        skipped_unsafe,
        skipped_old,
        skipped_deps,
        failed,
    )
    return report
