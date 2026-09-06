"""Tests for GET /portfolio/performance's computation core (issue #360
Phase 1) — approximate EOD TWR, filters, benchmark normalization."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models.benchmark_price import BenchmarkPrice
from app.models.portfolio_snapshot_batch import PortfolioSnapshotBatch
from app.models.portfolio_value_snapshot import PortfolioValueSnapshot
from app.services.portfolio_performance import compute_portfolio_performance
from app.tests.conftest import seed_user

D1 = date(2026, 8, 1)
D2 = date(2026, 8, 2)
D3 = date(2026, 8, 3)


def _mark_complete(session: Session, user_id: uuid.UUID, d: date) -> None:
    session.add(PortfolioSnapshotBatch(user_id=user_id, snapshot_date=d, status="complete"))


def _row(
    session: Session,
    user_id: uuid.UUID,
    d: date,
    holding_id: uuid.UUID,
    *,
    shares: Decimal | None = None,
    current_value: Decimal | None = None,
    market_value_base: Decimal | None,
    account: str | None = None,
    market: str | None = "US",
    data_quality: str = "ok",
) -> None:
    session.add(
        PortfolioValueSnapshot(
            user_id=user_id,
            snapshot_date=d,
            holding_id=holding_id,
            currency="USD",
            shares=shares,
            current_value=current_value,
            market_value_base=market_value_base,
            account=account,
            market=market,
            data_quality=data_quality,
        )
    )


def test_twr_flat_price_mid_period_deposit_is_near_zero_but_raw_mv_change_isnt(
    db_session: Session,
) -> None:
    user_id = uuid.uuid4()
    seed_user(db_session, user_id)
    holding_id = uuid.uuid4()
    for d in (D1, D2, D3):
        _mark_complete(db_session, user_id, d)
    _row(
        db_session, user_id, D1, holding_id, shares=Decimal("10"), market_value_base=Decimal("1000")
    )
    # Deposit: shares double at the SAME price -> a cash flow, not a return.
    _row(
        db_session, user_id, D2, holding_id, shares=Decimal("20"), market_value_base=Decimal("2000")
    )
    _row(
        db_session, user_id, D3, holding_id, shares=Decimal("20"), market_value_base=Decimal("2000")
    )
    db_session.flush()

    twr_result = compute_portfolio_performance(
        db_session, user_id, range_key="ALL", benchmark_codes=[], twr=True, today=D3
    )
    raw_result = compute_portfolio_performance(
        db_session, user_id, range_key="ALL", benchmark_codes=[], twr=False, today=D3
    )

    twr_final_pct = twr_result.portfolio.points[-1].return_pct_cumulative
    assert abs(twr_final_pct) < Decimal("0.001")  # ~0%, cash-flow-neutral
    assert raw_result.header.value_change_pct == Decimal("1.0000")  # +100% raw MV change
    assert raw_result.header.value_change_base == Decimal("1000")


def test_twr_price_move_with_no_quantity_change_matches_the_price_move(
    db_session: Session,
) -> None:
    user_id = uuid.uuid4()
    seed_user(db_session, user_id)
    holding_id = uuid.uuid4()
    _mark_complete(db_session, user_id, D1)
    _mark_complete(db_session, user_id, D2)
    _row(
        db_session, user_id, D1, holding_id, shares=Decimal("10"), market_value_base=Decimal("1000")
    )
    _row(
        db_session, user_id, D2, holding_id, shares=Decimal("10"), market_value_base=Decimal("1100")
    )
    db_session.flush()

    result = compute_portfolio_performance(
        db_session, user_id, range_key="ALL", benchmark_codes=[], twr=True, today=D2
    )
    assert result.portfolio.points[-1].return_pct_cumulative == Decimal("0.1000")


def test_filter_on_historical_account_keeps_sold_lot(db_session: Session) -> None:
    user_id = uuid.uuid4()
    seed_user(db_session, user_id)
    h1 = uuid.uuid4()
    _mark_complete(db_session, user_id, D1)
    _mark_complete(db_session, user_id, D2)
    _row(
        db_session,
        user_id,
        D1,
        h1,
        shares=Decimal("10"),
        market_value_base=Decimal("1000"),
        account="AccountX",
    )
    # Day 2: position sold out of AccountX entirely, a different holding
    # appears in a different account.
    h2 = uuid.uuid4()
    _row(
        db_session,
        user_id,
        D2,
        h2,
        shares=Decimal("5"),
        market_value_base=Decimal("500"),
        account="AccountY",
    )
    db_session.flush()

    result = compute_portfolio_performance(
        db_session,
        user_id,
        range_key="ALL",
        benchmark_codes=[],
        accounts=["AccountX"],
        today=D2,
    )
    assert result.portfolio.empty is False
    assert len(result.portfolio.points) == 2
    assert result.portfolio.points[0].value_base == Decimal("1000.00")
    assert result.portfolio.points[1].value_base == Decimal("0.00")  # sold out of this account


def test_filter_on_account_never_held_gives_empty_series(db_session: Session) -> None:
    user_id = uuid.uuid4()
    seed_user(db_session, user_id)
    h1 = uuid.uuid4()
    _mark_complete(db_session, user_id, D1)
    _row(
        db_session,
        user_id,
        D1,
        h1,
        shares=Decimal("10"),
        market_value_base=Decimal("1000"),
        account="AccountX",
    )
    db_session.flush()

    result = compute_portfolio_performance(
        db_session,
        user_id,
        range_key="ALL",
        benchmark_codes=[],
        accounts=["NeverHeldAccount"],
        today=D1,
    )
    assert result.portfolio.empty is True
    assert result.portfolio.points == []


def test_manual_unsupported_value_edit_counts_as_cash_flow_under_twr(db_session: Session) -> None:
    user_id = uuid.uuid4()
    seed_user(db_session, user_id)
    holding_id = uuid.uuid4()
    _mark_complete(db_session, user_id, D1)
    _mark_complete(db_session, user_id, D2)
    _row(
        db_session,
        user_id,
        D1,
        holding_id,
        current_value=Decimal("100"),
        market_value_base=Decimal("100"),
    )
    # User manually re-typed the estimated value — a level jump, not a "return".
    _row(
        db_session,
        user_id,
        D2,
        holding_id,
        current_value=Decimal("500"),
        market_value_base=Decimal("500"),
    )
    db_session.flush()

    result = compute_portfolio_performance(
        db_session, user_id, range_key="ALL", benchmark_codes=[], twr=True, today=D2
    )
    assert result.portfolio.points[-1].return_pct_cumulative == Decimal("0.0000")
    assert result.header.value_change_base == Decimal("400")


def test_header_label_is_always_market_value_change(db_session: Session) -> None:
    user_id = uuid.uuid4()
    seed_user(db_session, user_id)
    holding_id = uuid.uuid4()
    _mark_complete(db_session, user_id, D1)
    _row(db_session, user_id, D1, holding_id, shares=Decimal("1"), market_value_base=Decimal("100"))
    db_session.flush()

    for twr_flag in (True, False):
        result = compute_portfolio_performance(
            db_session, user_id, range_key="ALL", benchmark_codes=[], twr=twr_flag, today=D1
        )
        assert result.header.label == "market_value_change"


def test_benchmarks_independently_normalize_to_zero_at_own_start(db_session: Session) -> None:
    user_id = uuid.uuid4()
    seed_user(db_session, user_id)
    db_session.add_all(
        [
            BenchmarkPrice(index_code="sp500", price_date=D1, close_price=Decimal("5000")),
            BenchmarkPrice(index_code="sp500", price_date=D2, close_price=Decimal("5500")),
            BenchmarkPrice(index_code="nasdaq", price_date=D1, close_price=Decimal("17000")),
            BenchmarkPrice(index_code="nasdaq", price_date=D2, close_price=Decimal("16150")),
        ]
    )
    db_session.flush()

    result = compute_portfolio_performance(
        db_session,
        user_id,
        range_key="ALL",
        benchmark_codes=["sp500", "nasdaq"],
        today=D2,
    )
    by_code = {b.index_code: b for b in result.benchmarks}
    assert by_code["sp500"].points[0].return_pct_cumulative == Decimal("0")
    assert by_code["sp500"].points[1].return_pct_cumulative == Decimal("0.1000")  # +10%
    assert by_code["nasdaq"].points[0].return_pct_cumulative == Decimal("0")
    assert by_code["nasdaq"].points[1].return_pct_cumulative == Decimal("-0.0500")  # -5%


def test_no_matching_history_keeps_benchmarks_but_marks_portfolio_empty(
    db_session: Session,
) -> None:
    user_id = uuid.uuid4()
    seed_user(db_session, user_id)
    db_session.add(BenchmarkPrice(index_code="sp500", price_date=D1, close_price=Decimal("5000")))
    db_session.flush()

    result = compute_portfolio_performance(
        db_session, user_id, range_key="ALL", benchmark_codes=["sp500"], today=D1
    )
    assert result.portfolio.empty is True
    assert len(result.benchmarks) == 1
    assert result.benchmarks[0].points  # benchmark still drawn
