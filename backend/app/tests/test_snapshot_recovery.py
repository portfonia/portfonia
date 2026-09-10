"""Recovery tests for missed / unpublished snapshot days (issue #373 Scenario 2).

The rule under test: a day comes back by replaying a frozen payload, or by a
recompute that is *provably* the same book — never by silently rebuilding it
from holdings that have moved.
"""

from __future__ import annotations

import logging
import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.fx_rate import FxRate
from app.models.holding import Holding
from app.models.portfolio_snapshot_batch import PortfolioSnapshotBatch
from app.models.portfolio_snapshot_outbox import PortfolioSnapshotOutbox
from app.models.portfolio_value_snapshot import PortfolioValueSnapshot
from app.models.price_snapshot import PriceSnapshot
from app.services.portfolio_history import capture_portfolio_value_snapshot, stage_user_snapshot
from app.services.snapshot_outbox import get_outbox_row
from app.services.snapshot_recovery import (
    CATCHUP_LOOKBACK_DAYS,
    recompute_is_safe,
    recover_portfolio_snapshots,
)
from app.services.user_purge import purge_user
from app.tests.conftest import seed_user

TODAY = date(2026, 9, 9)  # a Wednesday
MISSED_DAY = date(2026, 9, 8)
EVIDENCE_DAY = date(2026, 9, 7)


def _seed_price(session: Session, ticker: str, trade_date: date, close: Decimal) -> None:
    session.add(
        PriceSnapshot(
            ticker=ticker,
            market="US",
            session_node="close",
            trade_date=trade_date,
            close=close,
        )
    )


def _seed_holding(session: Session, user_id: uuid.UUID, ticker: str, shares: Decimal) -> Holding:
    holding = Holding(
        user_id=user_id,
        name=ticker,
        ticker=ticker,
        currency="USD",
        pricing_mode="auto",
        shares=shares,
    )
    session.add(holding)
    return holding


def _daily_rows(session: Session, snapshot_date: date) -> list[PortfolioValueSnapshot]:
    return list(
        session.execute(
            select(PortfolioValueSnapshot).where(
                PortfolioValueSnapshot.snapshot_date == snapshot_date
            )
        ).scalars()
    )


def _status(session: Session, user_id: uuid.UUID, snapshot_date: date) -> str | None:
    return session.execute(
        select(PortfolioSnapshotBatch.status).where(
            PortfolioSnapshotBatch.user_id == user_id,
            PortfolioSnapshotBatch.snapshot_date == snapshot_date,
        )
    ).scalar_one_or_none()


def test_unpublished_day_is_replayed_from_the_frozen_payload(db_session: Session) -> None:
    """The job ran, the publish never landed, the book then moved: replay must
    restore the evening's composition, not today's."""
    user_id = uuid.uuid4()
    seed_user(db_session, user_id)
    frozen_holding = _seed_holding(db_session, user_id, "AAPL", Decimal("10"))
    _seed_price(db_session, "AAPL", MISSED_DAY, Decimal("20"))
    db_session.flush()

    assert stage_user_snapshot(db_session, user_id, MISSED_DAY) == (1, "computed")
    db_session.commit()

    # Book moves before anyone notices the missing day.
    db_session.delete(frozen_holding)
    _seed_holding(db_session, user_id, "TSLA", Decimal("4"))
    _seed_price(db_session, "TSLA", MISSED_DAY, Decimal("100"))
    db_session.flush()

    report = recover_portfolio_snapshots(
        db_session, MISSED_DAY, MISSED_DAY, today=MISSED_DAY + timedelta(days=3)
    )

    assert (report.replayed, report.recomputed) == (1, 0)
    rows = _daily_rows(db_session, MISSED_DAY)
    assert [r.ticker for r in rows] == ["AAPL"]
    assert rows[0].market_value_base == Decimal("200.00")
    assert _status(db_session, user_id, MISSED_DAY) == "complete"
    outbox_row = get_outbox_row(db_session, user_id, MISSED_DAY)
    assert outbox_row is not None and outbox_row.status == "applied"


def test_recomputing_a_missed_day_from_a_moved_book_is_refused(
    db_session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    """No frozen payload + the book moved since the last frozen evidence: the
    day stays missing and the skip is logged. Nothing is invented."""
    user_id = uuid.uuid4()
    seed_user(db_session, user_id)
    _seed_holding(db_session, user_id, "AAPL", Decimal("10"))
    _seed_price(db_session, "AAPL", EVIDENCE_DAY, Decimal("20"))
    db_session.flush()
    capture_portfolio_value_snapshot(db_session, EVIDENCE_DAY)

    # The book moves (a new holding appears) before the missed day is noticed.
    _seed_holding(db_session, user_id, "TSLA", Decimal("4"))
    _seed_price(db_session, "TSLA", MISSED_DAY, Decimal("100"))
    db_session.flush()

    assert recompute_is_safe(db_session, user_id, MISSED_DAY) is False

    logging.getLogger("app.services.snapshot_recovery").disabled = False
    with caplog.at_level(logging.WARNING, logger="app.services.snapshot_recovery"):
        report = recover_portfolio_snapshots(db_session, MISSED_DAY, MISSED_DAY, today=TODAY)

    assert (report.replayed, report.recomputed, report.skipped_unsafe) == (0, 0, 1)
    assert _daily_rows(db_session, MISSED_DAY) == []
    assert _status(db_session, user_id, MISSED_DAY) is None
    assert get_outbox_row(db_session, user_id, MISSED_DAY) is None
    assert "book has moved" in caplog.text


def test_missed_day_is_recomputed_when_the_book_is_unchanged(db_session: Session) -> None:
    """Same composition as the last frozen evidence: the catch-up may rebuild
    the day (this is the FX-arrived-late / task-crashed case)."""
    user_id = uuid.uuid4()
    seed_user(db_session, user_id)
    _seed_holding(db_session, user_id, "AAPL", Decimal("10"))
    _seed_price(db_session, "AAPL", EVIDENCE_DAY, Decimal("20"))
    _seed_price(db_session, "AAPL", MISSED_DAY, Decimal("21"))
    db_session.flush()
    capture_portfolio_value_snapshot(db_session, EVIDENCE_DAY)

    assert recompute_is_safe(db_session, user_id, MISSED_DAY) is True

    report = recover_portfolio_snapshots(db_session, MISSED_DAY, MISSED_DAY, today=TODAY)

    assert (report.replayed, report.recomputed, report.skipped_unsafe) == (0, 1, 0)
    rows = _daily_rows(db_session, MISSED_DAY)
    assert len(rows) == 1
    assert rows[0].market_value_base == Decimal("210.00")
    assert _status(db_session, user_id, MISSED_DAY) == "complete"
    outbox_row = get_outbox_row(db_session, user_id, MISSED_DAY)
    assert outbox_row is not None and outbox_row.status == "applied"


def test_recompute_of_a_skipped_deps_day_is_retried_once_fx_arrives(db_session: Session) -> None:
    """A day left `skipped_deps` by a late FX capture is picked up by the
    catch-up instead of being lost (the pre-#373 code claimed the next run
    covered this; the next run captured a different date)."""
    user_id = uuid.uuid4()
    seed_user(db_session, user_id)
    _seed_holding(db_session, user_id, "AAPL", Decimal("10"))
    hk_holding = Holding(
        user_id=user_id,
        name="Tencent",
        ticker="0700.HK",
        currency="HKD",
        pricing_mode="auto",
        shares=Decimal("100"),
    )
    db_session.add(hk_holding)
    _seed_price(db_session, "AAPL", EVIDENCE_DAY, Decimal("20"))
    _seed_price(db_session, "0700.HK", EVIDENCE_DAY, Decimal("300"))
    db_session.add(FxRate(pair="USDHKD", rate=Decimal("7.8"), rate_date=EVIDENCE_DAY))
    db_session.flush()
    capture_portfolio_value_snapshot(db_session, EVIDENCE_DAY)

    # The FX pipeline then goes quiet for longer than the 10-day lookback, so
    # the next capture day freezes nothing at all.
    _seed_price(db_session, "0700.HK", MISSED_DAY, Decimal("310"))
    _seed_price(db_session, "AAPL", MISSED_DAY, Decimal("20"))
    quiet_day = MISSED_DAY + timedelta(days=14)
    db_session.flush()
    assert stage_user_snapshot(db_session, user_id, quiet_day) == (0, "skipped_deps")
    db_session.commit()

    # FX is backfilled for that day; the book itself never changed.
    db_session.add(FxRate(pair="USDHKD", rate=Decimal("7.9"), rate_date=quiet_day))
    db_session.flush()

    report = recover_portfolio_snapshots(
        db_session, quiet_day, quiet_day, today=quiet_day + timedelta(days=1)
    )

    assert (report.recomputed, report.skipped_deps) == (1, 0)
    assert len(_daily_rows(db_session, quiet_day)) == 2


def test_recompute_outside_the_short_window_is_refused(db_session: Session) -> None:
    """Beyond `CATCHUP_LOOKBACK_DAYS` a day without a frozen payload is not
    rebuilt, however unchanged the book looks."""
    user_id = uuid.uuid4()
    seed_user(db_session, user_id)
    _seed_holding(db_session, user_id, "AAPL", Decimal("10"))
    _seed_price(db_session, "AAPL", EVIDENCE_DAY, Decimal("20"))
    db_session.flush()
    capture_portfolio_value_snapshot(db_session, EVIDENCE_DAY)

    long_ago = EVIDENCE_DAY - timedelta(days=CATCHUP_LOOKBACK_DAYS + 3)
    assert long_ago.weekday() < 5
    report = recover_portfolio_snapshots(db_session, long_ago, long_ago, today=TODAY)

    assert report.skipped_old == 1
    assert _daily_rows(db_session, long_ago) == []


def test_already_complete_days_are_left_alone(db_session: Session) -> None:
    user_id = uuid.uuid4()
    seed_user(db_session, user_id)
    _seed_holding(db_session, user_id, "AAPL", Decimal("10"))
    _seed_price(db_session, "AAPL", EVIDENCE_DAY, Decimal("20"))
    db_session.flush()
    capture_portfolio_value_snapshot(db_session, EVIDENCE_DAY)

    outbox_row = get_outbox_row(db_session, user_id, EVIDENCE_DAY)
    assert outbox_row is not None
    outbox_row.status = "computed"  # pretend a stale row lingers
    db_session.flush()

    report = recover_portfolio_snapshots(db_session, EVIDENCE_DAY, EVIDENCE_DAY, today=TODAY)

    # Complete day: not "needing" work, so the stale row is not re-applied.
    assert (report.already_complete, report.replayed) == (1, 0)


def test_corrupt_payload_is_marked_failed_and_not_retried(db_session: Session) -> None:
    user_id = uuid.uuid4()
    seed_user(db_session, user_id)
    _seed_holding(db_session, user_id, "AAPL", Decimal("10"))
    _seed_price(db_session, "AAPL", MISSED_DAY, Decimal("20"))
    db_session.flush()
    stage_user_snapshot(db_session, user_id, MISSED_DAY)
    db_session.commit()

    outbox_row = get_outbox_row(db_session, user_id, MISSED_DAY)
    assert outbox_row is not None
    outbox_row.checksum = "0" * 64
    db_session.flush()

    report = recover_portfolio_snapshots(db_session, MISSED_DAY, MISSED_DAY, today=MISSED_DAY)

    assert report.failed == 1
    assert _daily_rows(db_session, MISSED_DAY) == []
    assert outbox_row.status == "failed"

    # A second run must not retry the same corrupt payload.
    again = recover_portfolio_snapshots(db_session, MISSED_DAY, MISSED_DAY, today=MISSED_DAY)
    assert (again.failed, again.replayed) == (1, 0)


def test_recovery_window_is_bounded(db_session: Session) -> None:
    with pytest.raises(ValueError):
        recover_portfolio_snapshots(db_session, date(2026, 1, 1), date(2026, 6, 1))
    with pytest.raises(ValueError):
        recover_portfolio_snapshots(db_session, date(2026, 6, 1), date(2026, 1, 1))


def test_weekend_days_are_not_recovered(db_session: Session) -> None:
    """Mon-Fri is the capture cadence; a Saturday has nothing to recover."""
    user_id = uuid.uuid4()
    seed_user(db_session, user_id)
    _seed_holding(db_session, user_id, "AAPL", Decimal("10"))
    db_session.flush()

    saturday = date(2026, 9, 5)
    assert saturday.weekday() == 5
    report = recover_portfolio_snapshots(db_session, saturday, saturday, today=TODAY)

    assert report.replayed == 0
    assert _daily_rows(db_session, saturday) == []


def test_purge_cascades_outbox_rows(db_session: Session) -> None:
    user_id = uuid.uuid4()
    seed_user(db_session, user_id)
    _seed_holding(db_session, user_id, "AAPL", Decimal("10"))
    _seed_price(db_session, "AAPL", TODAY, Decimal("20"))
    db_session.flush()
    capture_portfolio_value_snapshot(db_session, TODAY)

    purge_user(db_session, user_id)
    db_session.flush()

    assert (
        db_session.execute(
            select(PortfolioSnapshotOutbox).where(PortfolioSnapshotOutbox.user_id == user_id)
        ).first()
        is None
    )
