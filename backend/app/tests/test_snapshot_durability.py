"""Durability tests for the two-phase snapshot write (issue #373).

Scenario 1 — a mid-write failure must never leave a `complete` batch with
missing/partial rows.
Scenario 2 — a day that was computed but not published must be replayable
from the frozen payload, even after the live book has moved.
"""

from __future__ import annotations

import uuid
from datetime import date
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
from app.services import portfolio_history
from app.services.portfolio_history import (
    apply_outbox_row,
    capture_portfolio_value_snapshot,
    stage_user_snapshot,
)
from app.services.snapshot_outbox import OutboxPayloadError, get_outbox_row
from app.tests.conftest import seed_user

TODAY = date(2026, 9, 5)


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


def _seed_fx(session: Session, pair: str, rate_date: date, rate: Decimal) -> None:
    session.add(FxRate(pair=pair, rate=rate, rate_date=rate_date))


def _seed_auto_holding(
    session: Session, user_id: uuid.UUID, ticker: str, shares: Decimal
) -> Holding:
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


def _batches(session: Session) -> list[PortfolioSnapshotBatch]:
    return list(session.execute(select(PortfolioSnapshotBatch)).scalars())


def _live_rows(session: Session) -> list[PortfolioValueSnapshot]:
    return list(
        session.execute(
            select(PortfolioValueSnapshot).order_by(PortfolioValueSnapshot.holding_id)
        ).scalars()
    )


def test_capture_freezes_payload_and_publishes_exactly_those_rows(db_session: Session) -> None:
    """The live rows must be the frozen payload's rows — that is the contract
    `complete` is allowed to imply (issue #373 constraint 4)."""
    user_id = uuid.uuid4()
    seed_user(db_session, user_id)
    _seed_auto_holding(db_session, user_id, "AAPL", Decimal("3"))
    _seed_price(db_session, "AAPL", TODAY, Decimal("50"))
    db_session.flush()

    capture_portfolio_value_snapshot(db_session, TODAY)

    outbox_row = get_outbox_row(db_session, user_id, TODAY)
    assert outbox_row is not None
    assert outbox_row.status == "applied"
    assert outbox_row.applied_at is not None

    rows = _live_rows(db_session)
    assert len(rows) == 1
    assert rows[0].market_value_base == Decimal("150.00")

    # Replaying an applied row is idempotent: same rows, no duplicates.
    assert apply_outbox_row(db_session, outbox_row) == 1
    assert len(_live_rows(db_session)) == 1


def test_mid_write_failure_never_leaves_a_partial_complete_batch(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scenario 1: an exception while publishing the second user rolls back
    the first user's already-written rows too — the day is retryable and no
    batch is left `complete` with missing rows."""
    u1, u2 = uuid.uuid4(), uuid.uuid4()
    for user_id, ticker in ((u1, "AAPL"), (u2, "MSFT")):
        seed_user(db_session, user_id)
        _seed_auto_holding(db_session, user_id, ticker, Decimal("1"))
        _seed_price(db_session, ticker, TODAY, Decimal("10"))
    db_session.flush()

    real_upsert = portfolio_history._upsert_rows
    calls = {"n": 0}

    def _failing_upsert(session: Session, rows: list[dict[str, object]]) -> int:
        calls["n"] += 1
        written = real_upsert(session, rows)
        if calls["n"] == 1:
            return written
        raise RuntimeError("simulated failure while publishing the second user")

    monkeypatch.setattr(portfolio_history, "_upsert_rows", _failing_upsert)

    with pytest.raises(RuntimeError):
        capture_portfolio_value_snapshot(db_session, TODAY)
    db_session.rollback()

    assert _live_rows(db_session) == []
    # The day is left explicitly not-published, never half-published: the
    # batch rows from the freeze phase stay `pending` (the reader only trusts
    # `complete`), so nothing is exposed and the day is clearly retryable.
    assert sorted(b.status for b in _batches(db_session)) == ["pending", "pending"]

    # The intent, however, is durable: both users' payloads survived the
    # failed publish, so the day can be replayed rather than recomputed.
    frozen = list(db_session.execute(select(PortfolioSnapshotOutbox)).scalars())
    assert sorted(r.status for r in frozen) == ["computed", "computed"]

    monkeypatch.undo()
    capture_portfolio_value_snapshot(db_session, TODAY)

    assert len(_live_rows(db_session)) == 2
    assert sorted(b.status for b in _batches(db_session)) == ["complete", "complete"]
    assert {r.status for r in db_session.execute(select(PortfolioSnapshotOutbox)).scalars()} == {
        "applied"
    }


def test_failed_publish_is_replayable_after_the_book_moved(db_session: Session) -> None:
    """Scenario 2 core: a day frozen before the publish failed must replay the
    *frozen* composition, not today's holdings."""
    user_id = uuid.uuid4()
    seed_user(db_session, user_id)
    frozen_holding = _seed_auto_holding(db_session, user_id, "AAPL", Decimal("10"))
    _seed_price(db_session, "AAPL", TODAY, Decimal("20"))
    db_session.flush()

    written, status = stage_user_snapshot(db_session, user_id, TODAY)
    assert (written, status) == (1, "computed")
    db_session.commit()

    # The book moves on before the publish lands: AAPL sold, TSLA bought.
    db_session.delete(frozen_holding)
    _seed_auto_holding(db_session, user_id, "TSLA", Decimal("5"))
    _seed_price(db_session, "TSLA", TODAY, Decimal("100"))
    db_session.flush()

    outbox_row = get_outbox_row(db_session, user_id, TODAY)
    assert outbox_row is not None
    assert apply_outbox_row(db_session, outbox_row) == 1

    rows = _live_rows(db_session)
    assert [r.ticker for r in rows] == ["AAPL"]
    assert rows[0].market_value_base == Decimal("200.00")
    assert outbox_row.status == "applied"
    batch = db_session.execute(
        select(PortfolioSnapshotBatch).where(PortfolioSnapshotBatch.user_id == user_id)
    ).scalar_one()
    assert batch.status == "complete"


def test_shrinking_book_drops_the_orphan_row_for_that_day(db_session: Session) -> None:
    """Contract 4 / delete-replace: after a same-day re-capture with one
    holding sold, the day's live rows must equal the new payload — the sold
    holding's row must not survive under a `complete` batch."""
    user_id = uuid.uuid4()
    seed_user(db_session, user_id)
    _seed_auto_holding(db_session, user_id, "AAPL", Decimal("2"))
    sold = _seed_auto_holding(db_session, user_id, "MSFT", Decimal("3"))
    _seed_price(db_session, "AAPL", TODAY, Decimal("10"))
    _seed_price(db_session, "MSFT", TODAY, Decimal("5"))
    db_session.flush()

    capture_portfolio_value_snapshot(db_session, TODAY)
    assert {r.ticker for r in _live_rows(db_session)} == {"AAPL", "MSFT"}

    # MSFT is sold, then the same day is captured again (catch-up re-run).
    db_session.delete(sold)
    db_session.flush()
    capture_portfolio_value_snapshot(db_session, TODAY)

    rows = _live_rows(db_session)
    assert [r.ticker for r in rows] == ["AAPL"]
    assert rows[0].market_value_base == Decimal("20.00")
    assert [b.status for b in _batches(db_session)] == ["complete"]
    outbox_row = get_outbox_row(db_session, user_id, TODAY)
    assert outbox_row is not None and outbox_row.status == "applied"


def test_empty_book_replace_clears_a_day_that_had_rows(db_session: Session) -> None:
    """The empty-book exit day is a replace too: a payload of zero rows must
    delete the day's previous live rows before `complete` is set."""
    user_id = uuid.uuid4()
    seed_user(db_session, user_id)
    holding = _seed_auto_holding(db_session, user_id, "AAPL", Decimal("2"))
    _seed_price(db_session, "AAPL", TODAY, Decimal("10"))
    db_session.flush()

    capture_portfolio_value_snapshot(db_session, TODAY)
    assert len(_live_rows(db_session)) == 1

    # Last holding sold: the exit-day capture freezes an empty payload.
    db_session.delete(holding)
    db_session.flush()
    capture_portfolio_value_snapshot(db_session, TODAY)

    assert _live_rows(db_session) == []
    assert [b.status for b in _batches(db_session)] == ["complete"]
    outbox_row = get_outbox_row(db_session, user_id, TODAY)
    assert outbox_row is not None and outbox_row.status == "applied"


def test_replay_replaces_orphan_rows_left_by_an_earlier_partial_state(
    db_session: Session,
) -> None:
    """The recovery path uses the same replace-set apply, so an orphan row
    cannot survive a replay onto an already-unpublished day."""
    user_id = uuid.uuid4()
    seed_user(db_session, user_id)
    holding = _seed_auto_holding(db_session, user_id, "AAPL", Decimal("1"))
    _seed_price(db_session, "AAPL", TODAY, Decimal("10"))
    db_session.flush()
    stage_user_snapshot(db_session, user_id, TODAY)
    db_session.commit()

    # An orphan row for a holding that is not in the frozen payload.
    orphan = PortfolioValueSnapshot(
        user_id=user_id,
        snapshot_date=TODAY,
        holding_id=uuid.uuid4(),
        ticker="ORPHAN",
        currency="USD",
        base_currency="USD",
    )
    db_session.add(orphan)
    db_session.flush()

    outbox_row = get_outbox_row(db_session, user_id, TODAY)
    assert outbox_row is not None
    assert apply_outbox_row(db_session, outbox_row) == 1

    assert [r.holding_id for r in _live_rows(db_session)] == [holding.id]


def test_skipped_deps_freezes_no_payload(db_session: Session) -> None:
    """An unpriceable day must stay absent rather than frozen half-complete."""
    user_id = uuid.uuid4()
    seed_user(db_session, user_id)
    holding = Holding(
        user_id=user_id,
        name="Tencent",
        ticker="0700.HK",
        currency="HKD",
        pricing_mode="auto",
        shares=Decimal("100"),
    )
    db_session.add(holding)
    _seed_price(db_session, "0700.HK", TODAY, Decimal("300"))
    db_session.flush()
    # No USDHKD rate seeded — the FX dependency is not ready.

    written, status = stage_user_snapshot(db_session, user_id, TODAY)

    assert (written, status) == (0, "skipped_deps")
    assert get_outbox_row(db_session, user_id, TODAY) is None
    assert _live_rows(db_session) == []


def test_empty_book_exit_day_is_frozen_and_published(db_session: Session) -> None:
    """A zero-holdings day (D5's real $0) is replayable like any other."""
    user_id = uuid.uuid4()
    seed_user(db_session, user_id)
    db_session.flush()

    written, status = stage_user_snapshot(db_session, user_id, TODAY)
    assert (written, status) == (0, "computed")

    # Not published yet: `complete` must not appear before the apply phase.
    batch = db_session.execute(
        select(PortfolioSnapshotBatch).where(PortfolioSnapshotBatch.user_id == user_id)
    ).scalar_one()
    assert batch.status == "pending"

    outbox_row = get_outbox_row(db_session, user_id, TODAY)
    assert outbox_row is not None
    assert apply_outbox_row(db_session, outbox_row) == 0
    assert batch.status == "complete"
    assert outbox_row.status == "applied"


def test_same_day_rerun_refreezes_the_payload_with_latest_data(db_session: Session) -> None:
    """A same-day catch-up run publishes the latest data, not the payload the
    first attempt froze (the live write path's existing upsert semantics)."""
    user_id = uuid.uuid4()
    seed_user(db_session, user_id)
    _seed_auto_holding(db_session, user_id, "AAPL", Decimal("1"))
    _seed_price(db_session, "AAPL", TODAY, Decimal("10"))
    db_session.flush()

    capture_portfolio_value_snapshot(db_session, TODAY)
    assert _live_rows(db_session)[0].market_value_base == Decimal("10.00")

    # A later close arrives the same day; the catch-up run must publish it.
    price = db_session.execute(
        select(PriceSnapshot).where(PriceSnapshot.ticker == "AAPL")
    ).scalar_one()
    price.close = Decimal("11")
    db_session.flush()
    capture_portfolio_value_snapshot(db_session, TODAY)

    assert _live_rows(db_session)[0].market_value_base == Decimal("11.00")


def test_corrupted_payload_is_not_applied(db_session: Session) -> None:
    user_id = uuid.uuid4()
    seed_user(db_session, user_id)
    _seed_auto_holding(db_session, user_id, "AAPL", Decimal("1"))
    _seed_price(db_session, "AAPL", TODAY, Decimal("10"))
    db_session.flush()
    stage_user_snapshot(db_session, user_id, TODAY)

    outbox_row = get_outbox_row(db_session, user_id, TODAY)
    assert outbox_row is not None
    outbox_row.checksum = "0" * 64
    db_session.flush()

    with pytest.raises(OutboxPayloadError):
        apply_outbox_row(db_session, outbox_row)
