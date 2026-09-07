"""Tests for GET /portfolio/performance's computation core (issue #360
Phase 1) — approximate EOD TWR, filters, benchmark normalization."""

from __future__ import annotations

import importlib
import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.models.benchmark_price import BenchmarkPrice
from app.models.portfolio_snapshot_batch import PortfolioSnapshotBatch
from app.models.portfolio_value_snapshot import PortfolioValueSnapshot
from app.models.price_snapshot import PriceSnapshot
from app.services.portfolio_performance import compute_portfolio_performance
from app.tests.conftest import seed_user

D1 = date(2026, 8, 1)
D2 = date(2026, 8, 2)
D3 = date(2026, 8, 3)
D4 = date(2026, 8, 4)


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
    ticker: str | None = None,
    currency: str = "USD",
    is_backfilled: bool = False,
) -> None:
    session.add(
        PortfolioValueSnapshot(
            user_id=user_id,
            snapshot_date=d,
            holding_id=holding_id,
            currency=currency,
            ticker=ticker,
            shares=shares,
            current_value=current_value,
            market_value_base=market_value_base,
            account=account,
            market=market,
            data_quality=data_quality,
            is_backfilled=is_backfilled,
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


def test_solo_full_exit_tracks_price_move_not_negative_100_pct(db_session: Session) -> None:
    """Regression for review 5124107298 finding 1 (PR #363): a full exit
    must read as a cash-flow-neutral price move on the exit day, not an
    exclusion from V_t_minus that manufactures an approximately -100%
    "return". The holding is repriced from `price_snapshots` for day 2
    even though it no longer has a snapshot row that day (position fully
    sold, book empty)."""
    user_id = uuid.uuid4()
    seed_user(db_session, user_id)
    holding_id = uuid.uuid4()
    _mark_complete(db_session, user_id, D1)
    _mark_complete(db_session, user_id, D2)  # batch complete, but zero holdings that day
    _row(
        db_session,
        user_id,
        D1,
        holding_id,
        shares=Decimal("10"),
        market_value_base=Decimal("1000"),
        ticker="AAPL",
    )
    # AAPL's real market price kept moving +10% even though the user sold
    # out of it entirely before day 2's snapshot was written.
    db_session.add(
        PriceSnapshot(
            ticker="AAPL", market="US", session_node="close", trade_date=D2, close=Decimal("110")
        )
    )
    db_session.flush()

    result = compute_portfolio_performance(
        db_session, user_id, range_key="ALL", benchmark_codes=[], twr=True, today=D2
    )
    assert result.portfolio.points[-1].return_pct_cumulative == Decimal("0.1000")
    # The book is genuinely empty after the sale — value_base correctly
    # drops to 0 even though TWR does not.
    assert result.portfolio.points[-1].value_base == Decimal("0.00")


def test_zero_share_day_t_row_also_triggers_reprice_not_exclusion(db_session: Session) -> None:
    """Re-review leftover (PR #363, approval comment): a day-t row that
    exists but carries `shares == 0` must be treated the same as no row at
    all — repriced from `price_snapshots`, not excluded from V_t_minus,
    which would silently reproduce finding 1's -100% bug for this one row
    shape even after the "no row at all" case was fixed."""
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
        shares=Decimal("10"),
        market_value_base=Decimal("1000"),
        ticker="AAPL",
    )
    # Degenerate day-2 row: shares collapsed to zero without the row being
    # removed — distinct from the "no row at all" scenario the other exit
    # test covers, but must be handled the same way.
    _row(
        db_session,
        user_id,
        D2,
        holding_id,
        shares=Decimal("0"),
        market_value_base=Decimal("0"),
        ticker="AAPL",
    )
    db_session.add(
        PriceSnapshot(
            ticker="AAPL", market="US", session_node="close", trade_date=D2, close=Decimal("110")
        )
    )
    db_session.flush()

    result = compute_portfolio_performance(
        db_session, user_id, range_key="ALL", benchmark_codes=[], twr=True, today=D2
    )
    assert result.portfolio.points[-1].return_pct_cumulative == Decimal("0.1000")


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


# --- issue #366: tracking_start, backfilled-row exclusion, common window ---


def test_tracking_start_ignores_backfilled_only_history_uses_first_real_complete_day(
    db_session: Session,
) -> None:
    """The retired composition-replay backfill wrote `is_backfilled=True`
    rows for days before the user was actually tracked. `tracking_start`
    must be the first REAL complete day, and that fictional earlier day
    must not appear as a series point at all."""
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
        shares=Decimal("10"),
        market_value_base=Decimal("5000"),
        is_backfilled=True,
    )
    _row(
        db_session, user_id, D2, holding_id, shares=Decimal("10"), market_value_base=Decimal("1000")
    )
    db_session.flush()

    result = compute_portfolio_performance(
        db_session, user_id, range_key="ALL", benchmark_codes=[], today=D2
    )
    assert result.portfolio.tracking_start == D2
    assert result.portfolio.start_date == D2
    assert len(result.portfolio.points) == 1
    assert result.portfolio.points[0].value_base == Decimal("1000.00")


def test_backfilled_rows_excluded_even_on_a_day_with_real_rows_too(db_session: Session) -> None:
    """A mixed day (one real row, one leftover backfilled row for a
    different holding) must sum only the real row's value."""
    user_id = uuid.uuid4()
    seed_user(db_session, user_id)
    real_id = uuid.uuid4()
    stale_id = uuid.uuid4()
    _mark_complete(db_session, user_id, D1)
    _row(db_session, user_id, D1, real_id, shares=Decimal("10"), market_value_base=Decimal("1000"))
    _row(
        db_session,
        user_id,
        D1,
        stale_id,
        shares=Decimal("50"),
        market_value_base=Decimal("5000"),
        is_backfilled=True,
    )
    db_session.flush()

    result = compute_portfolio_performance(
        db_session, user_id, range_key="ALL", benchmark_codes=[], today=D1
    )
    assert result.portfolio.tracking_start == D1
    assert len(result.portfolio.points) == 1
    assert result.portfolio.points[0].value_base == Decimal("1000.00")


def test_common_window_filtered_portfolio_shorter_than_benchmark(db_session: Session) -> None:
    """A dimension filter can push the FILTERED series' own first usable
    point later than the user-level `tracking_start` — e.g. AccountY's
    first day has a row but no usable price yet ("insufficient", excluded
    from the series — distinct from D8's "not held that day" $0 case).
    The benchmark must co-normalize to that later point, not its own
    full-range start (issue #366 D7), regressing against the independent-
    normalize behavior `test_benchmarks_independently_normalize_to_zero_
    at_own_start` covers for the no-portfolio-data case."""
    user_id = uuid.uuid4()
    seed_user(db_session, user_id)
    account_x = uuid.uuid4()
    account_y = uuid.uuid4()
    for d in (D1, D2, D3):
        _mark_complete(db_session, user_id, d)
    # AccountX: tracked from D1 with a usable value — establishes
    # tracking_start=D1 at the user level, unrelated to the accounts filter.
    for d in (D1, D2, D3):
        _row(
            db_session,
            user_id,
            d,
            account_x,
            shares=Decimal("10"),
            market_value_base=Decimal("1000"),
            account="AccountX",
        )
    # AccountY: a row exists on D1 but with no usable price yet
    # (market_value_base=None -> "insufficient", excluded from the series
    # entirely, NOT a D8 $0) — its own filtered series only becomes usable
    # from D2.
    _row(
        db_session,
        user_id,
        D1,
        account_y,
        shares=Decimal("10"),
        market_value_base=None,
        account="AccountY",
        data_quality="insufficient",
    )
    _row(
        db_session,
        user_id,
        D2,
        account_y,
        shares=Decimal("10"),
        market_value_base=Decimal("2000"),
        account="AccountY",
    )
    _row(
        db_session,
        user_id,
        D3,
        account_y,
        shares=Decimal("10"),
        market_value_base=Decimal("2200"),
        account="AccountY",
    )
    db_session.add_all(
        [
            BenchmarkPrice(index_code="sp500", price_date=D1, close_price=Decimal("100")),
            BenchmarkPrice(index_code="sp500", price_date=D2, close_price=Decimal("110")),
            BenchmarkPrice(index_code="sp500", price_date=D3, close_price=Decimal("121")),
        ]
    )
    db_session.flush()

    result = compute_portfolio_performance(
        db_session,
        user_id,
        range_key="ALL",
        benchmark_codes=["sp500"],
        accounts=["AccountY"],
        today=D3,
    )
    assert result.portfolio.tracking_start == D1  # unfiltered, user-level
    assert result.portfolio.start_date == D2  # filtered series' real first point
    assert [p.point_date for p in result.portfolio.points] == [D2, D3]
    assert result.portfolio.points[-1].return_pct_cumulative == Decimal("0.1000")

    sp500 = result.benchmarks[0]
    assert sp500.start_date == D1  # true data start still disclosed
    assert sp500.comparable is True
    assert [p.point_date for p in sp500.points] == [D2, D3]  # D1 clipped out
    assert sp500.points[0].return_pct_cumulative == Decimal("0")
    assert sp500.points[1].return_pct_cumulative == Decimal("0.1000")  # rebased off D2, not D1


def test_range_before_tracking_start_clips_benchmark_and_co_normalizes(
    db_session: Session,
) -> None:
    """No dimension filter involved this time: the user simply wasn't
    tracked yet on D1 (no batch at all that day). A benchmark spanning
    D1-D3 must still be re-anchored to D2 (`tracking_start`), not show its
    own D1-based cumulative % next to the portfolio's D2-based one."""
    user_id = uuid.uuid4()
    seed_user(db_session, user_id)
    holding_id = uuid.uuid4()
    _mark_complete(db_session, user_id, D2)
    _mark_complete(db_session, user_id, D3)
    _row(
        db_session, user_id, D2, holding_id, shares=Decimal("10"), market_value_base=Decimal("1000")
    )
    _row(
        db_session, user_id, D3, holding_id, shares=Decimal("10"), market_value_base=Decimal("1100")
    )
    db_session.add_all(
        [
            BenchmarkPrice(index_code="sp500", price_date=D1, close_price=Decimal("100")),
            BenchmarkPrice(index_code="sp500", price_date=D2, close_price=Decimal("105")),
            BenchmarkPrice(index_code="sp500", price_date=D3, close_price=Decimal("115.5")),
        ]
    )
    db_session.flush()

    result = compute_portfolio_performance(
        db_session, user_id, range_key="ALL", benchmark_codes=["sp500"], today=D3
    )
    assert result.portfolio.tracking_start == D2
    assert result.portfolio.start_date == D2

    sp500 = result.benchmarks[0]
    assert sp500.start_date == D1  # disclosed, but not the compare window
    assert sp500.comparable is True
    assert [p.point_date for p in sp500.points] == [D2, D3]
    assert sp500.points[0].return_pct_cumulative == Decimal("0")
    # 115.5/105 - 1 = +10%, rebased off D2's 105, not D1's 100.
    assert sp500.points[1].return_pct_cumulative == Decimal("0.1000")


def test_benchmark_with_no_overlap_in_compare_window_marked_non_comparable(
    db_session: Session,
) -> None:
    """A benchmark whose only data predates `tracking_start` has nothing to
    show in the common window — it must be marked non-comparable with an
    empty point list, never a silently cross-window %."""
    user_id = uuid.uuid4()
    seed_user(db_session, user_id)
    holding_id = uuid.uuid4()
    _mark_complete(db_session, user_id, D2)
    _row(
        db_session, user_id, D2, holding_id, shares=Decimal("10"), market_value_base=Decimal("1000")
    )
    # This benchmark only has data before tracking even starts.
    db_session.add(BenchmarkPrice(index_code="dow30", price_date=D1, close_price=Decimal("100")))
    db_session.flush()

    result = compute_portfolio_performance(
        db_session, user_id, range_key="ALL", benchmark_codes=["dow30"], today=D2
    )
    dow30 = result.benchmarks[0]
    assert dow30.comparable is False
    assert dow30.points == []
    assert dow30.start_date == D1  # still disclosed


def test_backfill_portfolio_value_history_script_removed() -> None:
    """Issue #366: the composition-replay backfill script is deleted
    outright, not deprecated — no import path should remain for future
    agents to accidentally resurrect it as a product step."""
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("app.scripts.backfill_portfolio_value_history")
