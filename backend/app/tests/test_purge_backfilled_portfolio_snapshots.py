"""Tests for the issue #366 ops cleanup script: deleting known-bad
`is_backfilled=True` portfolio snapshot rows and their now-orphaned batch
rows, without touching real data."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.portfolio_snapshot_batch import PortfolioSnapshotBatch
from app.models.portfolio_value_snapshot import PortfolioValueSnapshot
from app.scripts.purge_backfilled_portfolio_snapshots import (
    purge_backfilled_portfolio_snapshots,
)
from app.tests.conftest import seed_user

D1 = date(2024, 11, 1)
D2 = date(2026, 8, 1)


def test_dry_run_deletes_nothing(db_session: Session) -> None:
    user_id = uuid.uuid4()
    seed_user(db_session, user_id)
    db_session.add(PortfolioSnapshotBatch(user_id=user_id, snapshot_date=D1, status="complete"))
    db_session.add(
        PortfolioValueSnapshot(
            user_id=user_id,
            snapshot_date=D1,
            holding_id=uuid.uuid4(),
            currency="USD",
            market_value_base=Decimal("5000"),
            is_backfilled=True,
        )
    )
    db_session.flush()

    result = purge_backfilled_portfolio_snapshots(db_session, apply_changes=False)
    assert result == {"snapshot_rows": 1, "orphan_batches": 1, "users": 1}

    assert db_session.execute(select(PortfolioValueSnapshot)).scalars().first() is not None
    assert db_session.execute(select(PortfolioSnapshotBatch)).scalars().first() is not None


def test_apply_deletes_backfilled_rows_and_orphan_batch_only(db_session: Session) -> None:
    user_id = uuid.uuid4()
    seed_user(db_session, user_id)
    real_holding = uuid.uuid4()
    bad_holding = uuid.uuid4()

    # D1: entirely fictional (backfilled) day — batch becomes an orphan.
    db_session.add(PortfolioSnapshotBatch(user_id=user_id, snapshot_date=D1, status="complete"))
    db_session.add(
        PortfolioValueSnapshot(
            user_id=user_id,
            snapshot_date=D1,
            holding_id=bad_holding,
            currency="USD",
            market_value_base=Decimal("5000"),
            is_backfilled=True,
        )
    )
    # D2: mixed day — one real row, one leftover backfilled row for a
    # different holding. Batch must survive; only the bad row goes.
    db_session.add(PortfolioSnapshotBatch(user_id=user_id, snapshot_date=D2, status="complete"))
    db_session.add(
        PortfolioValueSnapshot(
            user_id=user_id,
            snapshot_date=D2,
            holding_id=real_holding,
            currency="USD",
            market_value_base=Decimal("1000"),
            is_backfilled=False,
        )
    )
    db_session.add(
        PortfolioValueSnapshot(
            user_id=user_id,
            snapshot_date=D2,
            holding_id=bad_holding,
            currency="USD",
            market_value_base=Decimal("9000"),
            is_backfilled=True,
        )
    )
    db_session.flush()

    result = purge_backfilled_portfolio_snapshots(db_session, apply_changes=True)
    assert result == {"snapshot_rows": 2, "orphan_batches": 1, "users": 1}
    db_session.flush()

    remaining_snapshots = list(db_session.execute(select(PortfolioValueSnapshot)).scalars())
    assert len(remaining_snapshots) == 1
    assert remaining_snapshots[0].holding_id == real_holding
    assert remaining_snapshots[0].is_backfilled is False

    remaining_batches = list(db_session.execute(select(PortfolioSnapshotBatch)).scalars())
    assert [b.snapshot_date for b in remaining_batches] == [D2]


def test_scoped_to_user_id_leaves_other_users_untouched(db_session: Session) -> None:
    user_a = uuid.uuid4()
    user_b = uuid.uuid4()
    seed_user(db_session, user_a)
    seed_user(db_session, user_b)
    for user_id in (user_a, user_b):
        db_session.add(PortfolioSnapshotBatch(user_id=user_id, snapshot_date=D1, status="complete"))
        db_session.add(
            PortfolioValueSnapshot(
                user_id=user_id,
                snapshot_date=D1,
                holding_id=uuid.uuid4(),
                currency="USD",
                market_value_base=Decimal("5000"),
                is_backfilled=True,
            )
        )
    db_session.flush()

    result = purge_backfilled_portfolio_snapshots(db_session, apply_changes=True, user_ids=[user_a])
    assert result == {"snapshot_rows": 1, "orphan_batches": 1, "users": 1}
    db_session.flush()

    remaining = list(
        db_session.execute(
            select(PortfolioValueSnapshot).where(PortfolioValueSnapshot.user_id == user_b)
        ).scalars()
    )
    assert len(remaining) == 1
    assert remaining[0].is_backfilled is True
