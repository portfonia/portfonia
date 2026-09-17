"""One-off weekend-gap backfill (issue #487 Requirement 6 / acceptance 8)."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.holding import Holding
from app.models.portfolio_snapshot_batch import PortfolioSnapshotBatch
from app.models.portfolio_value_snapshot import PortfolioValueSnapshot
from app.models.price_snapshot import PriceSnapshot
from app.scripts.backfill_weekend_gaps import backfill_weekend_gaps
from app.services.portfolio_history import capture_portfolio_value_snapshot
from app.tests.conftest import seed_user

_START = date(2026, 9, 8)
_END = date(2026, 9, 14)
_FRI = date(2026, 9, 11)
_SAT = date(2026, 9, 12)
_SUN = date(2026, 9, 13)
_EVIDENCE = date(2026, 9, 4)


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


def _holding(session: Session, user_id: uuid.UUID, ticker: str, shares: Decimal) -> Holding:
    holding = Holding(
        user_id=user_id,
        name=ticker,
        ticker=ticker,
        currency="USD",
        pricing_mode="auto",
        shares=shares,
        market="US",
    )
    session.add(holding)
    return holding


def _weekend_rows(
    session: Session, user_id: uuid.UUID, snapshot_date: date
) -> list[PortfolioValueSnapshot]:
    return list(
        session.execute(
            select(PortfolioValueSnapshot).where(
                PortfolioValueSnapshot.user_id == user_id,
                PortfolioValueSnapshot.snapshot_date == snapshot_date,
            )
        ).scalars()
    )


def _batch_status(session: Session, user_id: uuid.UUID, snapshot_date: date) -> str | None:
    return session.execute(
        select(PortfolioSnapshotBatch.status).where(
            PortfolioSnapshotBatch.user_id == user_id,
            PortfolioSnapshotBatch.snapshot_date == snapshot_date,
        )
    ).scalar_one_or_none()


def test_unchanged_book_backfills_every_weekend_in_range(db_session: Session) -> None:
    """Acceptance 8a: holdings unchanged since before the window → every
    Saturday/Sunday in range gets a real approx_carried row + complete batch."""
    user_id = uuid.uuid4()
    seed_user(db_session, user_id)
    _holding(db_session, user_id, "AAPL", Decimal("10"))
    _seed_price(db_session, "AAPL", _FRI, Decimal("100"))
    db_session.flush()
    capture_portfolio_value_snapshot(db_session, _FRI)

    report = backfill_weekend_gaps(db_session, _START, _END)

    assert report.backfilled == 2
    for day in (_SAT, _SUN):
        rows = _weekend_rows(db_session, user_id, day)
        assert len(rows) == 1
        assert rows[0].data_quality == "approx_carried"
        assert rows[0].price_as_of == _FRI
        assert _batch_status(db_session, user_id, day) == "complete"


def test_composition_change_no_longer_blocks_the_weekend_backfill(
    db_session: Session,
) -> None:
    """Acceptance 8b (issue #497): a holding added after the last frozen
    evidence used to block the weekend backfill; the fingerprint gate that
    did that is deleted, so both weekend days now get a real row anyway."""
    user_id = uuid.uuid4()
    seed_user(db_session, user_id)
    _holding(db_session, user_id, "AAPL", Decimal("10"))
    _seed_price(db_session, "AAPL", _EVIDENCE, Decimal("20"))
    _seed_price(db_session, "AAPL", _FRI, Decimal("100"))
    db_session.flush()
    capture_portfolio_value_snapshot(db_session, _EVIDENCE)

    _holding(db_session, user_id, "TSLA", Decimal("4"))
    _seed_price(db_session, "TSLA", _FRI, Decimal("50"))
    db_session.flush()

    report = backfill_weekend_gaps(db_session, _START, _END)

    assert report.backfilled == 2
    for day in (_SAT, _SUN):
        rows = _weekend_rows(db_session, user_id, day)
        assert {r.ticker for r in rows} == {"AAPL", "TSLA"}
        assert _batch_status(db_session, user_id, day) == "complete"


def test_rerun_is_noop_on_already_complete_weekend_days(db_session: Session) -> None:
    """Acceptance 8c: a second run skips already-complete days, no duplicates."""
    user_id = uuid.uuid4()
    seed_user(db_session, user_id)
    _holding(db_session, user_id, "AAPL", Decimal("10"))
    _seed_price(db_session, "AAPL", _FRI, Decimal("100"))
    db_session.flush()
    capture_portfolio_value_snapshot(db_session, _FRI)

    first = backfill_weekend_gaps(db_session, _START, _END)
    second = backfill_weekend_gaps(db_session, _START, _END)

    assert first.backfilled == 2
    assert second.backfilled == 0
    assert second.already_complete == 2
    for day in (_SAT, _SUN):
        assert len(_weekend_rows(db_session, user_id, day)) == 1
