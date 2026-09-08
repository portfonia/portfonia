"""Issue #377: first snapshot on a non-trading day must keep index history."""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import event
from sqlalchemy.orm import Session

from app.models.benchmark_price import BenchmarkPrice
from app.models.fx_rate import FxRate
from app.models.portfolio_snapshot_batch import PortfolioSnapshotBatch
from app.models.portfolio_value_snapshot import PortfolioValueSnapshot
from app.services.fx_conversion import conversion_pairs
from app.services.portfolio_performance import (
    VALID_RANGES,
    BenchmarkPoint,
    BenchmarkSeries,
    compute_portfolio_performance,
)
from app.tests.conftest import seed_user

SEP3 = date(2026, 9, 3)
SEP4 = date(2026, 9, 4)
SEP5 = date(2026, 9, 5)
SEP6 = date(2026, 9, 6)
SEP7 = date(2026, 9, 7)
SEP8 = date(2026, 9, 8)


def _complete(session: Session, user_id: uuid.UUID, d: date) -> None:
    session.add(PortfolioSnapshotBatch(user_id=user_id, snapshot_date=d, status="complete"))


def _snapshot(
    session: Session,
    user_id: uuid.UUID,
    d: date,
    holding_id: uuid.UUID,
    value: Decimal,
    *,
    account: str | None = None,
) -> None:
    session.add(
        PortfolioValueSnapshot(
            user_id=user_id,
            snapshot_date=d,
            holding_id=holding_id,
            currency="USD",
            base_currency="USD",
            shares=Decimal("10"),
            market_value_base=value,
            account=account,
            market="US",
            data_quality="ok",
        )
    )


def _close(
    session: Session, index_code: str, d: date, price: Decimal, currency: str = "USD"
) -> None:
    session.add(
        BenchmarkPrice(index_code=index_code, price_date=d, close_price=price, currency=currency)
    )


def _fx(session: Session, pair: str, d: date, rate: Decimal) -> None:
    session.add(FxRate(pair=pair, rate=rate, rate_date=d, source="test"))


def _by_date(series: BenchmarkSeries) -> dict[date, BenchmarkPoint]:
    return {point.point_date: point for point in series.points}


def test_conversion_pairs_preserve_both_cross_legs() -> None:
    assert conversion_pairs("USD", "USD") == []
    assert conversion_pairs("USD", "CNY") == ["USDCNY"]
    assert conversion_pairs("EUR", "CNY") == ["USDEUR", "USDCNY"]
    assert conversion_pairs("XXX", "USD") is None


def test_sep7_first_snapshot_keeps_full_history_and_worked_values(db_session: Session) -> None:
    user_id = uuid.uuid4()
    seed_user(db_session, user_id)
    holding_id = uuid.uuid4()
    _complete(db_session, user_id, SEP7)
    _snapshot(db_session, user_id, SEP7, holding_id, Decimal("1000"))
    for code in ("sp500", "dow30", "nasdaq"):
        _close(db_session, code, SEP3, Decimal("100"))
        _close(db_session, code, SEP4, Decimal("110"))
    db_session.flush()

    result = compute_portfolio_performance(
        db_session,
        user_id,
        range_key="1M",
        benchmark_codes=["sp500", "dow30", "nasdaq"],
        today=SEP7,
    )
    assert result.portfolio.start_date == SEP7
    assert result.portfolio.points[-1].return_pct_cumulative == Decimal("0.0000")
    assert result.header.value_change_pct == Decimal("0.0000")
    assert len(result.benchmarks) == 3
    for series in result.benchmarks:
        points = _by_date(series)
        assert series.displayable is True
        assert series.comparable is True
        assert series.normalization == "portfolio_start"
        assert series.anchor_date == SEP7
        assert series.comparison_status == "baseline_only"
        assert series.comparison_return_pct == Decimal("0.0000")
        assert series.comparison_start == SEP7
        assert series.comparison_end == SEP7
        assert points[SEP3].return_pct_cumulative == Decimal("-0.0909")
        assert points[SEP3].price_as_of == SEP3
        assert points[SEP3].carried is False
        for day in (SEP4, SEP5, SEP6, SEP7):
            assert points[day].return_pct_cumulative == Decimal("0.0000")
            assert points[day].price_as_of == SEP4
        assert points[SEP7].carried is True
        assert points[SEP7].unavailable_reason is None


def test_first_trading_day_gain_after_holiday_baseline(db_session: Session) -> None:
    user_id = uuid.uuid4()
    seed_user(db_session, user_id)
    holding_id = uuid.uuid4()
    _complete(db_session, user_id, SEP7)
    _complete(db_session, user_id, SEP8)
    _snapshot(db_session, user_id, SEP7, holding_id, Decimal("1000"))
    _snapshot(db_session, user_id, SEP8, holding_id, Decimal("1100"))
    _close(db_session, "sp500", SEP3, Decimal("100"))
    _close(db_session, "sp500", SEP4, Decimal("110"))
    _close(db_session, "sp500", SEP8, Decimal("121"))
    db_session.flush()

    result = compute_portfolio_performance(
        db_session, user_id, range_key="1M", benchmark_codes=["sp500"], twr=False, today=SEP8
    )
    sp500 = result.benchmarks[0]
    points = _by_date(sp500)
    assert result.portfolio.points[-1].return_pct_cumulative == Decimal("0.1000")
    assert sp500.anchor_date == SEP7
    assert points[SEP8].return_pct_cumulative == Decimal("0.1000")
    assert points[SEP8].price_as_of == SEP8
    assert sp500.comparison_status == "available"
    assert sp500.comparison_return_pct == Decimal("0.1000")
    # Must not re-anchor to Sep 8's 121 and drop the first move (121/121-1=0).
    assert points[SEP7].return_pct_cumulative == Decimal("0.0000")


def test_all_six_ranges_keep_pre_snapshot_history(db_session: Session) -> None:
    user_id = uuid.uuid4()
    seed_user(db_session, user_id)
    holding_id = uuid.uuid4()
    _complete(db_session, user_id, SEP7)
    _snapshot(db_session, user_id, SEP7, holding_id, Decimal("1000"))
    _close(db_session, "sp500", SEP3, Decimal("100"))
    _close(db_session, "sp500", SEP4, Decimal("110"))
    db_session.flush()

    for range_key in VALID_RANGES:
        result = compute_portfolio_performance(
            db_session, user_id, range_key=range_key, benchmark_codes=["sp500"], today=SEP7
        )
        points = _by_date(result.benchmarks[0])
        assert SEP3 in points
        assert SEP7 in points
        assert points[SEP3].return_pct_cumulative == Decimal("-0.0909")
        assert result.benchmarks[0].comparison_status == "baseline_only"


def test_ten_day_price_eligible_eleven_day_stale(db_session: Session) -> None:
    user_id = uuid.uuid4()
    seed_user(db_session, user_id)
    holding_id = uuid.uuid4()
    _complete(db_session, user_id, SEP7)
    _snapshot(db_session, user_id, SEP7, holding_id, Decimal("1000"))
    ten = SEP7 - timedelta(days=10)
    eleven = SEP7 - timedelta(days=11)
    _close(db_session, "sp500", ten, Decimal("100"))
    _close(db_session, "dow30", eleven, Decimal("100"))
    db_session.flush()

    result = compute_portfolio_performance(
        db_session,
        user_id,
        range_key="1M",
        benchmark_codes=["sp500", "dow30"],
        today=SEP7,
    )
    by_code = {series.index_code: series for series in result.benchmarks}
    sp500 = _by_date(by_code["sp500"])
    assert sp500[SEP7].return_pct_cumulative == Decimal("0.0000")
    assert sp500[SEP7].price_as_of == ten
    assert by_code["sp500"].comparable is True
    dow = _by_date(by_code["dow30"])
    assert dow[SEP7].return_pct_cumulative is None
    assert dow[SEP7].unavailable_reason == "stale_price"
    assert by_code["dow30"].comparable is False
    assert by_code["dow30"].comparison_status == "anchor_unavailable"
    assert by_code["dow30"].displayable is True
    assert by_code["dow30"].normalization == "own_start"


def test_future_price_never_used(db_session: Session) -> None:
    user_id = uuid.uuid4()
    seed_user(db_session, user_id)
    holding_id = uuid.uuid4()
    _complete(db_session, user_id, SEP7)
    _snapshot(db_session, user_id, SEP7, holding_id, Decimal("1000"))
    _close(db_session, "sp500", SEP8, Decimal("121"))
    db_session.flush()

    result = compute_portfolio_performance(
        db_session, user_id, range_key="1M", benchmark_codes=["sp500"], today=SEP8
    )
    sp500 = result.benchmarks[0]
    points = _by_date(sp500)
    assert SEP7 not in points
    assert sp500.normalization == "own_start"
    assert sp500.anchor_date == SEP8
    assert sp500.comparable is False
    assert sp500.comparison_status == "anchor_unavailable"
    assert points[SEP8].return_pct_cumulative == Decimal("0.0000")


def test_invalid_nonpositive_price(db_session: Session) -> None:
    user_id = uuid.uuid4()
    seed_user(db_session, user_id)
    holding_id = uuid.uuid4()
    _complete(db_session, user_id, SEP7)
    _snapshot(db_session, user_id, SEP7, holding_id, Decimal("1000"))
    _close(db_session, "sp500", SEP4, Decimal("0"))
    db_session.flush()

    result = compute_portfolio_performance(
        db_session, user_id, range_key="1M", benchmark_codes=["sp500"], today=SEP7
    )
    series = result.benchmarks[0]
    assert series.points == []
    assert series.displayable is False
    assert series.normalization == "unavailable"
    assert series.comparison_status == "anchor_unavailable"


def test_fx_ten_day_and_eleven_day_and_future(db_session: Session) -> None:
    ten = SEP7 - timedelta(days=10)
    eleven = SEP7 - timedelta(days=11)

    ok_user = uuid.uuid4()
    seed_user(db_session, ok_user)
    ok_holding = uuid.uuid4()
    _complete(db_session, ok_user, SEP7)
    _snapshot(db_session, ok_user, SEP7, ok_holding, Decimal("1000"))
    _close(db_session, "sp500", SEP4, Decimal("110"))
    _fx(db_session, "USDCNY", ten, Decimal("7.1"))

    stale_user = uuid.uuid4()
    seed_user(db_session, stale_user)
    stale_holding = uuid.uuid4()
    _complete(db_session, stale_user, SEP7)
    _snapshot(db_session, stale_user, SEP7, stale_holding, Decimal("1000"))
    _fx(db_session, "USDEUR", eleven, Decimal("0.9"))

    db_session.flush()

    ok = compute_portfolio_performance(
        db_session,
        ok_user,
        range_key="1M",
        benchmark_codes=["sp500"],
        base_currency="CNY",
        today=SEP7,
    )
    ok_point = _by_date(ok.benchmarks[0])[SEP7]
    assert ok_point.return_pct_cumulative == Decimal("0.0000")
    assert ok_point.fx_as_of == {"USDCNY": ten}
    assert ok_point.carried is True

    stale = compute_portfolio_performance(
        db_session,
        stale_user,
        range_key="1M",
        benchmark_codes=["sp500"],
        base_currency="EUR",
        today=SEP7,
    )
    stale_point = _by_date(stale.benchmarks[0])[SEP7]
    assert stale_point.unavailable_reason == "stale_fx"


def test_future_fx_never_used_for_p0(db_session: Session) -> None:
    user_id = uuid.uuid4()
    seed_user(db_session, user_id)
    holding_id = uuid.uuid4()
    _complete(db_session, user_id, SEP7)
    _snapshot(db_session, user_id, SEP7, holding_id, Decimal("1000"))
    _close(db_session, "nasdaq", SEP4, Decimal("110"), currency="EUR")
    _fx(db_session, "USDEUR", SEP8, Decimal("0.92"))
    db_session.flush()

    future = compute_portfolio_performance(
        db_session,
        user_id,
        range_key="1M",
        benchmark_codes=["nasdaq"],
        base_currency="USD",
        today=SEP8,
    )
    assert future.portfolio.empty is False
    future_series = future.benchmarks[0]
    assert future_series.comparison_status == "anchor_unavailable"
    assert future_series.normalization == "own_start"
    assert future_series.anchor_date == SEP8
    assert SEP7 not in _by_date(future_series)
    assert _by_date(future_series)[SEP8].return_pct_cumulative == Decimal("0.0000")


def test_unchanged_close_moves_with_fx(db_session: Session) -> None:
    user_id = uuid.uuid4()
    seed_user(db_session, user_id)
    holding_id = uuid.uuid4()
    _complete(db_session, user_id, SEP7)
    _complete(db_session, user_id, SEP8)
    _snapshot(db_session, user_id, SEP7, holding_id, Decimal("1000"))
    _snapshot(db_session, user_id, SEP8, holding_id, Decimal("1000"))
    _close(db_session, "sp500", SEP4, Decimal("110"))
    _fx(db_session, "USDCNY", SEP7, Decimal("7.0"))
    _fx(db_session, "USDCNY", SEP8, Decimal("7.7"))
    db_session.flush()

    result = compute_portfolio_performance(
        db_session,
        user_id,
        range_key="1M",
        benchmark_codes=["sp500"],
        base_currency="CNY",
        today=SEP8,
    )
    points = _by_date(result.benchmarks[0])
    assert points[SEP7].price_as_of == SEP4
    assert points[SEP8].price_as_of == SEP4
    assert points[SEP7].return_pct_cumulative == Decimal("0.0000")
    assert points[SEP8].return_pct_cumulative == Decimal("0.1000")
    assert points[SEP8].fx_as_of == {"USDCNY": SEP8}


def test_missing_mid_window_valuation_is_incomplete(db_session: Session) -> None:
    user_id = uuid.uuid4()
    seed_user(db_session, user_id)
    holding_id = uuid.uuid4()
    p_mid = date(2026, 8, 20)
    p_end = date(2026, 9, 7)
    _complete(db_session, user_id, p_mid)
    _complete(db_session, user_id, p_end)
    _snapshot(db_session, user_id, p_mid, holding_id, Decimal("1000"))
    _snapshot(db_session, user_id, p_end, holding_id, Decimal("1100"))
    _close(db_session, "sp500", p_mid, Decimal("100"))
    _close(db_session, "sp500", p_end - timedelta(days=11), Decimal("110"))
    db_session.flush()

    result = compute_portfolio_performance(
        db_session, user_id, range_key="1Y", benchmark_codes=["sp500"], today=p_end
    )
    sp500 = result.benchmarks[0]
    points = _by_date(sp500)
    assert sp500.normalization == "portfolio_start"
    assert points[p_end].return_pct_cumulative is None
    assert points[p_end].unavailable_reason == "stale_price"
    assert sp500.comparable is False
    assert sp500.comparison_status == "incomplete_window"
    assert sp500.comparison_return_pct is None
    assert sp500.displayable is True


def test_history_after_p1_does_not_enter_comparison_return(db_session: Session) -> None:
    user_id = uuid.uuid4()
    seed_user(db_session, user_id)
    holding_id = uuid.uuid4()
    _complete(db_session, user_id, SEP4)
    _complete(db_session, user_id, SEP7)
    _snapshot(db_session, user_id, SEP4, holding_id, Decimal("1000"))
    _snapshot(db_session, user_id, SEP7, holding_id, Decimal("1100"))
    _close(db_session, "sp500", SEP4, Decimal("110"))
    _close(db_session, "sp500", SEP8, Decimal("220"))
    db_session.flush()

    result = compute_portfolio_performance(
        db_session, user_id, range_key="1M", benchmark_codes=["sp500"], today=SEP8
    )
    sp500 = result.benchmarks[0]
    points = _by_date(sp500)
    assert result.portfolio.end_date == SEP7
    assert SEP8 in points
    assert points[SEP8].return_pct_cumulative == Decimal("1.0000")
    assert sp500.comparison_end == SEP7
    assert sp500.comparison_return_pct == Decimal("0.0000")
    assert sp500.comparable is True


def test_empty_and_filtered_empty_keep_benchmark_history(db_session: Session) -> None:
    user_id = uuid.uuid4()
    seed_user(db_session, user_id)
    holding_id = uuid.uuid4()
    _close(db_session, "sp500", SEP3, Decimal("100"))
    _close(db_session, "sp500", SEP4, Decimal("110"))
    db_session.flush()

    empty = compute_portfolio_performance(
        db_session, user_id, range_key="1M", benchmark_codes=["sp500"], today=SEP7
    )
    assert empty.portfolio.empty is True
    assert empty.benchmarks[0].comparison_status == "no_portfolio"
    assert empty.benchmarks[0].comparable is False
    assert empty.benchmarks[0].displayable is True
    assert empty.benchmarks[0].normalization == "own_start"
    assert _by_date(empty.benchmarks[0])[SEP3].return_pct_cumulative == Decimal("0.0000")

    _complete(db_session, user_id, SEP7)
    _snapshot(db_session, user_id, SEP7, holding_id, Decimal("1000"), account="A")
    db_session.flush()
    filtered = compute_portfolio_performance(
        db_session,
        user_id,
        range_key="1M",
        benchmark_codes=["sp500"],
        accounts=["Missing"],
        today=SEP7,
    )
    assert filtered.portfolio.empty is True
    assert filtered.benchmarks[0].displayable is True
    assert filtered.header.value_base == Decimal("0")


def test_cleared_book_still_draws_historical_portfolio(db_session: Session) -> None:
    user_id = uuid.uuid4()
    seed_user(db_session, user_id)
    holding_id = uuid.uuid4()
    _complete(db_session, user_id, SEP4)
    _complete(db_session, user_id, SEP7)
    _snapshot(db_session, user_id, SEP4, holding_id, Decimal("1000"))
    _snapshot(db_session, user_id, SEP7, holding_id, Decimal("0"))
    _close(db_session, "sp500", SEP4, Decimal("110"))
    db_session.flush()

    result = compute_portfolio_performance(
        db_session, user_id, range_key="1M", benchmark_codes=["sp500"], today=SEP7
    )
    assert result.portfolio.empty is False
    assert [point.point_date for point in result.portfolio.points] == [SEP4, SEP7]


def test_twr_toggle_does_not_change_index_series(db_session: Session) -> None:
    user_id = uuid.uuid4()
    seed_user(db_session, user_id)
    holding_id = uuid.uuid4()
    _complete(db_session, user_id, SEP7)
    _complete(db_session, user_id, SEP8)
    _snapshot(db_session, user_id, SEP7, holding_id, Decimal("1000"))
    _snapshot(db_session, user_id, SEP8, holding_id, Decimal("1100"))
    _close(db_session, "sp500", SEP4, Decimal("110"))
    _close(db_session, "sp500", SEP8, Decimal("121"))
    db_session.flush()

    on = compute_portfolio_performance(
        db_session, user_id, range_key="1M", benchmark_codes=["sp500"], twr=True, today=SEP8
    )
    off = compute_portfolio_performance(
        db_session, user_id, range_key="1M", benchmark_codes=["sp500"], twr=False, today=SEP8
    )
    assert [p.return_pct_cumulative for p in on.benchmarks[0].points] == [
        p.return_pct_cumulative for p in off.benchmarks[0].points
    ]
    assert on.header.value_change_pct == off.header.value_change_pct


def test_deselecting_index_does_not_change_portfolio_or_header(db_session: Session) -> None:
    user_id = uuid.uuid4()
    seed_user(db_session, user_id)
    holding_id = uuid.uuid4()
    _complete(db_session, user_id, SEP7)
    _snapshot(db_session, user_id, SEP7, holding_id, Decimal("1000"))
    _close(db_session, "sp500", SEP4, Decimal("110"))
    _close(db_session, "nasdaq", SEP4, Decimal("200"))
    db_session.flush()

    both = compute_portfolio_performance(
        db_session,
        user_id,
        range_key="1M",
        benchmark_codes=["sp500", "nasdaq"],
        today=SEP7,
    )
    one = compute_portfolio_performance(
        db_session, user_id, range_key="1M", benchmark_codes=["sp500"], today=SEP7
    )
    assert both.header.value_base == one.header.value_base
    assert both.header.value_change_pct == one.header.value_change_pct
    assert [p.point_date for p in both.portfolio.points] == [
        p.point_date for p in one.portfolio.points
    ]


def test_query_count_does_not_grow_with_day_count(db_session: Session) -> None:
    user_id = uuid.uuid4()
    seed_user(db_session, user_id)
    for offset in range(40):
        _close(db_session, "sp500", date(2026, 7, 30) + timedelta(days=offset), Decimal("100"))
        _close(db_session, "dow30", date(2026, 7, 30) + timedelta(days=offset), Decimal("200"))
    db_session.flush()

    bind = db_session.get_bind()
    counts: list[int] = []

    def _run(today: date) -> None:
        n = {"c": 0}

        def _before(*_args: object, **_kwargs: object) -> None:
            n["c"] += 1

        event.listen(bind, "before_cursor_execute", _before)
        try:
            compute_portfolio_performance(
                db_session,
                user_id,
                range_key="ALL",
                benchmark_codes=["sp500", "dow30"],
                today=today,
            )
        finally:
            event.remove(bind, "before_cursor_execute", _before)
        counts.append(n["c"])

    _run(date(2026, 8, 10))
    _run(date(2026, 9, 7))
    assert counts[0] == counts[1]
    assert counts[0] < 20
