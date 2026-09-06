"""Tests for the first-enable portfolio value history backfill script
(issue #360 Phase 1, D2 amendment)."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.holding import Holding
from app.models.portfolio_value_snapshot import PortfolioValueSnapshot
from app.models.price_snapshot import PriceSnapshot
from app.scripts.backfill_portfolio_value_history import backfill_portfolio_value_history
from app.tests.conftest import seed_user

TODAY = date(2026, 9, 5)  # a Saturday — exercises the business-day filter


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


def test_backfill_dry_run_writes_nothing(db_session: Session) -> None:
    user_id = uuid.uuid4()
    seed_user(db_session, user_id)
    db_session.add(
        Holding(
            user_id=user_id,
            name="Apple",
            ticker="AAPL",
            currency="USD",
            pricing_mode="auto",
            shares=Decimal("10"),
        )
    )
    _seed_price(db_session, "AAPL", TODAY - timedelta(days=10), Decimal("100"))
    db_session.flush()

    result = backfill_portfolio_value_history(db_session, user_id, apply_changes=False, today=TODAY)
    assert result["rows_written"] == 0
    assert (
        db_session.execute(
            select(PortfolioValueSnapshot).where(PortfolioValueSnapshot.user_id == user_id)
        ).first()
        is None
    )


def test_backfill_apply_writes_flagged_rows_and_never_uses_created_at(db_session: Session) -> None:
    user_id = uuid.uuid4()
    seed_user(db_session, user_id)
    # created_at is set far in the past relative to the price history —
    # the backfill must anchor on price availability, not this field.
    holding = Holding(
        user_id=user_id,
        name="Apple",
        ticker="AAPL",
        currency="USD",
        pricing_mode="auto",
        shares=Decimal("10"),
    )
    db_session.add(holding)
    db_session.flush()
    holding.created_at = datetime(2020, 1, 1)
    price_start = TODAY - timedelta(days=5)
    _seed_price(db_session, "AAPL", price_start, Decimal("100"))
    db_session.flush()

    result = backfill_portfolio_value_history(db_session, user_id, apply_changes=True, today=TODAY)
    assert result["rows_written"] > 0

    rows = (
        db_session.execute(
            select(PortfolioValueSnapshot).where(PortfolioValueSnapshot.user_id == user_id)
        )
        .scalars()
        .all()
    )
    assert rows
    assert all(r.is_backfilled for r in rows)
    assert min(r.snapshot_date for r in rows) >= price_start
    assert max(r.snapshot_date for r in rows) < TODAY  # today is left to the daily task


def test_backfill_refuses_second_run_without_force(db_session: Session) -> None:
    user_id = uuid.uuid4()
    seed_user(db_session, user_id)
    db_session.add(
        Holding(
            user_id=user_id,
            name="Apple",
            ticker="AAPL",
            currency="USD",
            pricing_mode="auto",
            shares=Decimal("10"),
        )
    )
    db_session.flush()
    # Simulate real (non-backfilled) daily-task history already existing.
    db_session.add(
        PortfolioValueSnapshot(
            user_id=user_id,
            snapshot_date=TODAY,
            holding_id=uuid.uuid4(),
            currency="USD",
            market_value_base=Decimal("100"),
            is_backfilled=False,
            data_quality="ok",
        )
    )
    db_session.flush()

    result = backfill_portfolio_value_history(db_session, user_id, apply_changes=True, today=TODAY)
    assert result["refused"] == 1
    assert result["rows_written"] == 0


def test_backfill_never_overwrites_existing_rows_on_rerun(db_session: Session) -> None:
    user_id = uuid.uuid4()
    seed_user(db_session, user_id)
    db_session.add(
        Holding(
            user_id=user_id,
            name="Apple",
            ticker="AAPL",
            currency="USD",
            pricing_mode="auto",
            shares=Decimal("10"),
        )
    )
    _seed_price(db_session, "AAPL", TODAY - timedelta(days=5), Decimal("100"))
    db_session.flush()

    backfill_portfolio_value_history(db_session, user_id, apply_changes=True, today=TODAY)
    rows_first = (
        db_session.execute(
            select(PortfolioValueSnapshot).where(PortfolioValueSnapshot.user_id == user_id)
        )
        .scalars()
        .all()
    )
    assert rows_first

    # A second run WITHOUT --force is not refused (only REAL, non-backfill
    # history triggers the refusal — review 5124107298 finding 4; see
    # `_existing_real_history`'s docstring) and must remain a safe no-op:
    # ON CONFLICT DO NOTHING skips every already-written (user, date,
    # holding_id) key rather than duplicating or overwriting it.
    with patch("app.scripts.backfill_portfolio_value_history._run_time_fx_rates", return_value={}):
        result = backfill_portfolio_value_history(
            db_session, user_id, apply_changes=True, today=TODAY
        )
    assert result["refused"] == 0
    rows_second = (
        db_session.execute(
            select(PortfolioValueSnapshot).where(PortfolioValueSnapshot.user_id == user_id)
        )
        .scalars()
        .all()
    )
    assert len(rows_second) == len(rows_first)
