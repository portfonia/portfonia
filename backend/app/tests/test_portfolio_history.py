"""Tests for the daily portfolio value snapshot writer (issue #360 Phase 1)."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.fx_rate import FxRate
from app.models.holding import Holding
from app.models.portfolio_snapshot_batch import PortfolioSnapshotBatch
from app.models.portfolio_value_snapshot import PortfolioValueSnapshot
from app.models.price_snapshot import PriceSnapshot
from app.services.portfolio_history import capture_portfolio_value_snapshot
from app.services.user_purge import purge_user
from app.tests.conftest import capture_user_day, seed_user

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


def test_write_snapshot_is_idempotent_and_upserts_on_rerun(db_session: Session) -> None:
    user_id = uuid.uuid4()
    seed_user(db_session, user_id)
    holding = Holding(
        user_id=user_id,
        name="Apple",
        ticker="AAPL",
        currency="USD",
        pricing_mode="auto",
        shares=Decimal("10"),
    )
    db_session.add(holding)
    _seed_price(db_session, "AAPL", TODAY, Decimal("100"))
    db_session.flush()

    written1, status1 = capture_user_day(db_session, user_id, TODAY)
    assert written1 == 1
    assert status1 == "complete"

    # Re-running the same day (catch-up) upserts, not duplicates.
    written2, status2 = capture_user_day(db_session, user_id, TODAY)
    assert written2 == 1
    assert status2 == "complete"

    rows = (
        db_session.execute(
            select(PortfolioValueSnapshot).where(
                PortfolioValueSnapshot.user_id == user_id,
                PortfolioValueSnapshot.snapshot_date == TODAY,
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].holding_id == holding.id
    assert rows[0].market_value_base == Decimal("1000.00")


def test_batch_marked_skipped_deps_when_fx_missing(db_session: Session) -> None:
    user_id = uuid.uuid4()
    seed_user(db_session, user_id)
    db_session.add(
        Holding(
            user_id=user_id,
            name="Tencent",
            ticker="0700.HK",
            currency="HKD",
            pricing_mode="auto",
            shares=Decimal("100"),
        )
    )
    _seed_price(db_session, "0700.HK", TODAY, Decimal("300"))
    db_session.flush()
    # No FX rate seeded at all for USDHKD — dependency not ready yet.

    written, status = capture_user_day(db_session, user_id, TODAY)
    assert written == 0
    assert status == "skipped_deps"

    batch = db_session.execute(
        select(PortfolioSnapshotBatch).where(
            PortfolioSnapshotBatch.user_id == user_id,
            PortfolioSnapshotBatch.snapshot_date == TODAY,
        )
    ).scalar_one()
    assert batch.status == "skipped_deps"

    rows = (
        db_session.execute(
            select(PortfolioValueSnapshot).where(PortfolioValueSnapshot.user_id == user_id)
        )
        .scalars()
        .all()
    )
    assert rows == []


def test_cash_holding_local_value_flat_but_base_moves_with_fx(db_session: Session) -> None:
    user_id = uuid.uuid4()
    seed_user(db_session, user_id)
    db_session.add(
        Holding(
            user_id=user_id,
            name="HKD Cash",
            currency="HKD",
            pricing_mode="manual",
            asset_type="cash",
            current_value=Decimal("1000"),
        )
    )
    _seed_fx(db_session, "USDHKD", date(2026, 9, 4), Decimal("7.8"))
    _seed_fx(db_session, "USDHKD", TODAY, Decimal("7.9"))
    db_session.flush()

    _, status_day1 = capture_user_day(db_session, user_id, date(2026, 9, 4))
    _, status_day2 = capture_user_day(db_session, user_id, TODAY)
    assert status_day1 == "complete"
    assert status_day2 == "complete"

    rows = {
        r.snapshot_date: r
        for r in db_session.execute(
            select(PortfolioValueSnapshot).where(PortfolioValueSnapshot.user_id == user_id)
        ).scalars()
    }
    day1, day2 = rows[date(2026, 9, 4)], rows[TODAY]
    # Local (native-currency) value is unchanged — cash carries no price return.
    assert day1.current_value == day2.current_value == Decimal("1000")
    # Base-currency value floats purely with the FX rate.
    assert day1.market_value_base == (Decimal("1000") / Decimal("7.8")).quantize(Decimal("0.01"))
    assert day2.market_value_base == (Decimal("1000") / Decimal("7.9")).quantize(Decimal("0.01"))
    assert day1.market_value_base != day2.market_value_base


def test_base_currency_column_records_capture_time_preference_not_current(
    db_session: Session,
) -> None:
    """Issue #367 review finding A (blacktomb42, review 5563537095): each
    row must record the currency `market_value_base` was ACTUALLY computed
    in at write time, since `users.base_currency` can change between two
    capture days (`PATCH /me/report-currency`) — the reader has no other
    way to tell a real market move apart from a mere unit change."""
    user_id = uuid.uuid4()
    user = seed_user(db_session, user_id)
    holding = Holding(
        user_id=user_id,
        name="USD Cash",
        currency="USD",
        pricing_mode="manual",
        asset_type="cash",
        current_value=Decimal("100"),
    )
    db_session.add(holding)
    day1 = date(2026, 9, 4)
    day2 = TODAY
    _seed_fx(db_session, "USDCNY", day1, Decimal("7"))
    _seed_fx(db_session, "USDCNY", day2, Decimal("7"))
    db_session.flush()

    capture_user_day(db_session, user_id, day1)
    user.base_currency = "CNY"
    db_session.flush()
    capture_user_day(db_session, user_id, day2)

    rows = {
        r.snapshot_date: r
        for r in db_session.execute(
            select(PortfolioValueSnapshot).where(PortfolioValueSnapshot.user_id == user_id)
        ).scalars()
    }
    assert rows[day1].base_currency == "USD"
    assert rows[day1].market_value_base == Decimal("100.00")
    assert rows[day2].base_currency == "CNY"
    assert rows[day2].market_value_base == Decimal("700.00")  # 100 USD * 7, same real value


def test_insufficient_value_omitted_not_zero_padded(db_session: Session) -> None:
    user_id = uuid.uuid4()
    seed_user(db_session, user_id)
    db_session.add(
        Holding(
            user_id=user_id,
            name="No Price Yet",
            ticker="NOPRICE",
            currency="USD",
            pricing_mode="auto",
            shares=Decimal("5"),
        )
    )
    # Base currency is USD, holding currency is USD too — no FX dependency,
    # so the batch can still complete even though the price itself is missing.
    db_session.flush()

    written, status = capture_user_day(db_session, user_id, TODAY)
    assert written == 1
    assert status == "complete"

    row = db_session.execute(
        select(PortfolioValueSnapshot).where(PortfolioValueSnapshot.user_id == user_id)
    ).scalar_one()
    assert row.market_value_base is None
    assert row.data_quality == "insufficient"


def test_capture_portfolio_value_snapshot_writes_for_every_active_user_with_holdings(
    db_session: Session,
) -> None:
    u1, u2 = uuid.uuid4(), uuid.uuid4()
    seed_user(db_session, u1)
    seed_user(db_session, u2)
    db_session.add_all(
        [
            Holding(
                user_id=u1,
                name="Apple",
                ticker="AAPL",
                currency="USD",
                pricing_mode="auto",
                shares=Decimal("1"),
            ),
            Holding(
                user_id=u2,
                name="Cash",
                currency="USD",
                pricing_mode="manual",
                asset_type="cash",
                current_value=Decimal("500"),
            ),
        ]
    )
    _seed_price(db_session, "AAPL", TODAY, Decimal("200"))
    db_session.flush()

    result = capture_portfolio_value_snapshot(db_session, TODAY)
    assert result["users"] == 2
    assert result["complete"] == 2
    assert result["skipped_deps"] == 0
    assert result["written"] == 2


def test_full_exit_still_captured_after_last_holding_deleted(db_session: Session) -> None:
    """Issue #367 review finding B (blacktomb42, review 5563537095): a user
    whose LAST holding is deleted must still get a (zero-holdings, $0)
    snapshot batch written on subsequent days — `stage_user_snapshot`
    already handles this correctly (D5's real $0 case), but the daily
    fan-out previously stopped calling it at all once the user dropped out
    of `holdings` entirely, silently freezing their portfolio history at
    its last real value instead of recording the exit."""
    user_id = uuid.uuid4()
    seed_user(db_session, user_id)
    day1 = date(2026, 9, 4)
    day2 = TODAY
    holding = Holding(
        user_id=user_id,
        name="USD Cash",
        currency="USD",
        pricing_mode="manual",
        asset_type="cash",
        current_value=Decimal("100"),
    )
    db_session.add(holding)
    db_session.flush()

    result_day1 = capture_portfolio_value_snapshot(db_session, day1)
    assert result_day1["users"] == 1
    assert result_day1["written"] == 1

    db_session.delete(holding)
    db_session.flush()

    result_day2 = capture_portfolio_value_snapshot(db_session, day2)
    assert result_day2["users"] == 1  # still selected — was tracked before
    assert result_day2["complete"] == 1
    assert result_day2["written"] == 0  # zero holdings, D5's real $0

    batches = {
        b.snapshot_date: b.status
        for b in db_session.execute(
            select(PortfolioSnapshotBatch).where(PortfolioSnapshotBatch.user_id == user_id)
        ).scalars()
    }
    assert batches == {day1: "complete", day2: "complete"}


def test_user_purge_cascades_snapshot_and_batch_rows(db_session: Session) -> None:
    user_id = uuid.uuid4()
    seed_user(db_session, user_id)
    db_session.add(
        Holding(
            user_id=user_id,
            name="Apple",
            ticker="AAPL",
            currency="USD",
            pricing_mode="auto",
            shares=Decimal("1"),
        )
    )
    _seed_price(db_session, "AAPL", TODAY, Decimal("100"))
    db_session.flush()
    capture_user_day(db_session, user_id, TODAY)

    assert (
        db_session.execute(
            select(PortfolioValueSnapshot).where(PortfolioValueSnapshot.user_id == user_id)
        ).first()
        is not None
    )

    purge_user(db_session, user_id)
    db_session.flush()

    assert (
        db_session.execute(
            select(PortfolioValueSnapshot).where(PortfolioValueSnapshot.user_id == user_id)
        ).first()
        is None
    )
    assert (
        db_session.execute(
            select(PortfolioSnapshotBatch).where(PortfolioSnapshotBatch.user_id == user_id)
        ).first()
        is None
    )
