"""Tests for issue #433 — asset-class allocation history and monthly
performance on `compute_portfolio_performance`.

Reuses the `_row`/`_mark_complete`/`_live_holding` fixtures already built for
the cumulative-series tests in `test_portfolio_performance.py` rather than
duplicating them.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import event
from sqlalchemy.orm import Session

from app.models.benchmark_price import BenchmarkPrice
from app.services.portfolio_performance import compute_portfolio_performance
from app.tests.conftest import seed_user
from app.tests.test_portfolio_performance import _mark_complete, _row

D1 = date(2026, 8, 1)
D2 = date(2026, 8, 2)
D3 = date(2026, 8, 3)


def _close(
    session: Session, index_code: str, d: date, close: Decimal, currency: str = "USD"
) -> None:
    session.add(
        BenchmarkPrice(index_code=index_code, price_date=d, close_price=close, currency=currency)
    )


# ---------------------------------------------------------------------------
# Allocation
# ---------------------------------------------------------------------------


def test_allocation_math_60_40(db_session: Session) -> None:
    user_id = uuid.uuid4()
    seed_user(db_session, user_id)
    _mark_complete(db_session, user_id, D1)
    h1, h2 = uuid.uuid4(), uuid.uuid4()
    _row(
        db_session,
        user_id,
        D1,
        h1,
        shares=Decimal("1"),
        market_value_base=Decimal("60"),
        asset_class="EQUITY_US_BROAD",
    )
    _row(
        db_session,
        user_id,
        D1,
        h2,
        current_value=Decimal("40"),
        market_value_base=Decimal("40"),
        asset_class="CASH_EQUIV",
    )
    db_session.flush()

    result = compute_portfolio_performance(
        db_session, user_id, range_key="ALL", benchmark_codes=[], today=D1
    )
    point = result.allocation.points[0]
    assert point.weights == {
        "EQUITY_US_BROAD": Decimal("0.6000"),
        "CASH_EQUIV": Decimal("0.4000"),
    }
    assert sum(point.weights.values()) == Decimal("1.0000")
    assert point.is_incomplete is False
    assert point.excluded_holding_count == 0
    assert result.allocation.asset_classes == ["EQUITY_US_BROAD", "CASH_EQUIV"]


def test_allocation_incomplete_when_a_matching_row_has_no_value(db_session: Session) -> None:
    user_id = uuid.uuid4()
    seed_user(db_session, user_id)
    _mark_complete(db_session, user_id, D1)
    h1, h2, h3 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    _row(
        db_session,
        user_id,
        D1,
        h1,
        shares=Decimal("1"),
        market_value_base=Decimal("60"),
        asset_class="EQUITY_US_BROAD",
    )
    _row(
        db_session,
        user_id,
        D1,
        h2,
        current_value=Decimal("40"),
        market_value_base=Decimal("40"),
        asset_class="CASH_EQUIV",
    )
    # Third matching row has no usable value (stale price / data_quality
    # "insufficient") — excluded from both numerator and denominator, but
    # the other two rows' weights are unaffected.
    _row(
        db_session,
        user_id,
        D1,
        h3,
        shares=Decimal("1"),
        market_value_base=None,
        asset_class="REIT",
        data_quality="insufficient",
    )
    db_session.flush()

    result = compute_portfolio_performance(
        db_session, user_id, range_key="ALL", benchmark_codes=[], today=D1
    )
    point = result.allocation.points[0]
    assert point.weights == {
        "EQUITY_US_BROAD": Decimal("0.6000"),
        "CASH_EQUIV": Decimal("0.4000"),
    }
    assert point.is_incomplete is True
    assert point.excluded_holding_count == 1


def test_allocation_zero_denominator_is_a_gap_not_a_fabricated_stack(db_session: Session) -> None:
    user_id = uuid.uuid4()
    seed_user(db_session, user_id)
    _mark_complete(db_session, user_id, D1)
    h1 = uuid.uuid4()
    # Only row that day is unclassified (legacy) — no valued/classified row
    # at all, so the denominator is zero.
    _row(
        db_session,
        user_id,
        D1,
        h1,
        shares=Decimal("1"),
        market_value_base=Decimal("100"),
        asset_class=None,
    )
    db_session.flush()

    result = compute_portfolio_performance(
        db_session, user_id, range_key="ALL", benchmark_codes=[], today=D1
    )
    point = result.allocation.points[0]
    assert point.weights == {}
    assert point.is_incomplete is True
    assert point.excluded_holding_count == 1
    assert result.allocation.asset_classes == []


def test_allocation_does_not_rewrite_earlier_snapshots_on_reclassification(
    db_session: Session,
) -> None:
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
        shares=Decimal("1"),
        market_value_base=Decimal("100"),
        asset_class="STOCK",
    )
    # Same holding_id reclassified starting D2 — D1's already-written row
    # must keep reading back as STOCK.
    _row(
        db_session,
        user_id,
        D2,
        holding_id,
        shares=Decimal("1"),
        market_value_base=Decimal("100"),
        asset_class="EQUITY_US_TECH",
    )
    db_session.flush()

    result = compute_portfolio_performance(
        db_session, user_id, range_key="ALL", benchmark_codes=[], today=D2
    )
    by_date = {p.point_date: p for p in result.allocation.points}
    assert by_date[D1].weights == {"STOCK": Decimal("1.0000")}
    assert by_date[D2].weights == {"EQUITY_US_TECH": Decimal("1.0000")}


# ---------------------------------------------------------------------------
# Monthly performance
# ---------------------------------------------------------------------------


def test_monthly_compounds_daily_links_not_rounded_cumulative_subtraction(
    db_session: Session,
) -> None:
    """+10% then -10% compounds to -1.00%, never 0% — proves the monthly
    aggregation multiplies unrounded daily links rather than differencing
    already-rounded cumulative percentages (issue #433 contract test 5)."""
    user_id = uuid.uuid4()
    seed_user(db_session, user_id)
    holding_id = uuid.uuid4()
    d0 = date(2026, 8, 29)
    d1 = date(2026, 8, 30)  # +10%
    d2 = date(2026, 8, 31)  # -10% back down (not to par)
    for d in (d0, d1, d2):
        _mark_complete(db_session, user_id, d)
    _row(
        db_session, user_id, d0, holding_id, shares=Decimal("10"), market_value_base=Decimal("1000")
    )
    _row(
        db_session, user_id, d1, holding_id, shares=Decimal("10"), market_value_base=Decimal("1100")
    )
    _row(
        db_session, user_id, d2, holding_id, shares=Decimal("10"), market_value_base=Decimal("990")
    )
    db_session.flush()

    result = compute_portfolio_performance(
        db_session, user_id, range_key="ALL", benchmark_codes=[], today=d2
    )
    august = next(p for p in result.monthly_performance.points if p.month == "2026-08")
    assert august.portfolio_return_pct == Decimal("-0.0100")


def test_monthly_cash_flow_neutral_under_flat_price_deposit(db_session: Session) -> None:
    user_id = uuid.uuid4()
    seed_user(db_session, user_id)
    holding_id = uuid.uuid4()
    d0 = date(2026, 8, 10)
    d1 = date(2026, 8, 20)  # deposit: shares double at same per-share price
    for d in (d0, d1):
        _mark_complete(db_session, user_id, d)
    _row(
        db_session, user_id, d0, holding_id, shares=Decimal("10"), market_value_base=Decimal("1000")
    )
    _row(
        db_session, user_id, d1, holding_id, shares=Decimal("20"), market_value_base=Decimal("2000")
    )
    db_session.flush()

    twr_on = compute_portfolio_performance(
        db_session, user_id, range_key="ALL", benchmark_codes=[], twr=True, today=d1
    )
    twr_off = compute_portfolio_performance(
        db_session, user_id, range_key="ALL", benchmark_codes=[], twr=False, today=d1
    )
    august_on = next(p for p in twr_on.monthly_performance.points if p.month == "2026-08")
    august_off = next(p for p in twr_off.monthly_performance.points if p.month == "2026-08")
    assert august_on.portfolio_return_pct == Decimal("0.0000")
    # Toggle isolation (contract test 7): identical monthly output regardless
    # of the cumulative chart's twr flag.
    assert august_on == august_off
    # The cumulative TWR-off header still shows the raw (non-neutral)
    # market-value change, proving the two are genuinely different figures.
    assert twr_off.header.value_change_pct == Decimal("1.0000")


def test_monthly_partial_reasons_tracking_start_and_month_to_date(db_session: Session) -> None:
    user_id = uuid.uuid4()
    seed_user(db_session, user_id)
    holding_id = uuid.uuid4()
    tracking_start = date(2026, 9, 15)
    last_point = date(2026, 9, 30)
    for d in (tracking_start, last_point):
        _mark_complete(db_session, user_id, d)
    _row(
        db_session,
        user_id,
        tracking_start,
        holding_id,
        shares=Decimal("10"),
        market_value_base=Decimal("1000"),
    )
    _row(
        db_session,
        user_id,
        last_point,
        holding_id,
        shares=Decimal("10"),
        market_value_base=Decimal("1050"),
    )
    db_session.flush()

    # "Today" is inside September, and September is both the first and last
    # tracked month here (month_to_date precedence still holds), so exercise
    # the precedence rule with a separate case that has two distinct months.
    oct_point = date(2026, 10, 5)
    _mark_complete(db_session, user_id, oct_point)
    _row(
        db_session,
        user_id,
        oct_point,
        holding_id,
        shares=Decimal("10"),
        market_value_base=Decimal("1060"),
    )
    db_session.flush()

    result = compute_portfolio_performance(
        db_session, user_id, range_key="ALL", benchmark_codes=[], today=oct_point
    )
    by_month = {p.month: p for p in result.monthly_performance.points}
    sept = by_month["2026-09"]
    assert sept.partial_reason == "tracking_start"
    assert sept.start_date == tracking_start
    assert sept.end_date == last_point

    october = by_month["2026-10"]
    assert october.partial_reason == "month_to_date"
    assert october.end_date == oct_point


def test_monthly_one_point_first_month_is_baseline_only_zero(db_session: Session) -> None:
    user_id = uuid.uuid4()
    seed_user(db_session, user_id)
    holding_id = uuid.uuid4()
    only_day = date(2026, 9, 28)
    _mark_complete(db_session, user_id, only_day)
    _row(
        db_session,
        user_id,
        only_day,
        holding_id,
        shares=Decimal("10"),
        market_value_base=Decimal("1000"),
    )
    db_session.flush()

    result = compute_portfolio_performance(
        db_session, user_id, range_key="ALL", benchmark_codes=[], today=only_day
    )
    point = result.monthly_performance.points[0]
    assert point.month == "2026-09"
    assert point.portfolio_return_pct == Decimal("0.0000")
    assert point.partial_reason in ("tracking_start", "month_to_date")


def test_monthly_benchmark_uses_portfolios_own_start_end_dates(db_session: Session) -> None:
    user_id = uuid.uuid4()
    seed_user(db_session, user_id)
    holding_id = uuid.uuid4()
    d0 = date(2026, 8, 3)
    d1 = date(2026, 8, 28)
    for d in (d0, d1):
        _mark_complete(db_session, user_id, d)
    _row(
        db_session, user_id, d0, holding_id, shares=Decimal("10"), market_value_base=Decimal("1000")
    )
    _row(
        db_session, user_id, d1, holding_id, shares=Decimal("10"), market_value_base=Decimal("1100")
    )
    _close(db_session, "sp500", d0, Decimal("100"))
    _close(db_session, "sp500", d1, Decimal("110"))
    db_session.flush()

    result = compute_portfolio_performance(
        db_session,
        user_id,
        range_key="ALL",
        benchmark_codes=[],
        monthly_benchmark="sp500",
        today=d1,
    )
    point = result.monthly_performance.points[0]
    assert point.start_date == d0
    assert point.end_date == d1
    assert point.benchmark_return_pct == Decimal("0.1000")
    assert point.benchmark_unavailable_reason is None
    assert result.monthly_performance.benchmark_code == "sp500"
    assert result.monthly_performance.method == "approx_eod_twr"


def test_monthly_benchmark_null_with_reason_when_endpoint_unavailable(db_session: Session) -> None:
    user_id = uuid.uuid4()
    seed_user(db_session, user_id)
    holding_id = uuid.uuid4()
    d0 = date(2026, 8, 3)
    d1 = date(2026, 8, 28)
    for d in (d0, d1):
        _mark_complete(db_session, user_id, d)
    _row(
        db_session, user_id, d0, holding_id, shares=Decimal("10"), market_value_base=Decimal("1000")
    )
    _row(
        db_session, user_id, d1, holding_id, shares=Decimal("10"), market_value_base=Decimal("1100")
    )
    # No sp500 closes seeded at all -> missing_price at both endpoints.
    db_session.flush()

    result = compute_portfolio_performance(
        db_session,
        user_id,
        range_key="ALL",
        benchmark_codes=[],
        monthly_benchmark="sp500",
        today=d1,
    )
    point = result.monthly_performance.points[0]
    assert point.benchmark_return_pct is None
    assert point.benchmark_unavailable_reason == "missing_price"


def test_monthly_benchmark_independent_of_cumulative_multi_select(db_session: Session) -> None:
    user_id = uuid.uuid4()
    seed_user(db_session, user_id)
    holding_id = uuid.uuid4()
    d0 = date(2026, 8, 3)
    _mark_complete(db_session, user_id, d0)
    _row(
        db_session, user_id, d0, holding_id, shares=Decimal("10"), market_value_base=Decimal("1000")
    )
    _close(db_session, "csi300", d0, Decimal("100"), currency="CNY")
    _close(db_session, "sp500", d0, Decimal("100"))
    _close(db_session, "nasdaq", d0, Decimal("100"))
    db_session.flush()

    result = compute_portfolio_performance(
        db_session,
        user_id,
        range_key="ALL",
        benchmark_codes=["sp500", "nasdaq"],
        monthly_benchmark="csi300",
        today=d0,
    )
    assert result.monthly_performance.benchmark_code == "csi300"
    assert {b.index_code for b in result.benchmarks} == {"sp500", "nasdaq"}


def test_query_count_does_not_grow_with_range_length(db_session: Session) -> None:
    user_id = uuid.uuid4()
    seed_user(db_session, user_id)
    holding_id = uuid.uuid4()
    base = date(2026, 1, 5)
    for offset in range(0, 200, 3):
        d = base + timedelta(days=offset)
        _mark_complete(db_session, user_id, d)
        _row(
            db_session,
            user_id,
            d,
            holding_id,
            shares=Decimal("10"),
            market_value_base=Decimal("1000") + offset,
        )
        _close(db_session, "sp500", d, Decimal("100") + offset)
    db_session.flush()

    bind = db_session.get_bind()

    def _count(range_key: str, today: date) -> int:
        n = {"c": 0}

        def _before(*_args: object, **_kwargs: object) -> None:
            n["c"] += 1

        event.listen(bind, "before_cursor_execute", _before)
        try:
            compute_portfolio_performance(
                db_session,
                user_id,
                range_key=range_key,
                benchmark_codes=["sp500"],
                monthly_benchmark="sp500",
                today=today,
            )
        finally:
            event.remove(bind, "before_cursor_execute", _before)
        return n["c"]

    short = _count("1M", base + timedelta(days=40))
    long = _count("ALL", base + timedelta(days=199))
    assert short == long
