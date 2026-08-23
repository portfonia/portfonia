"""Tests for the incremental-window data layer (ADR-002 steps 3-4)."""

from __future__ import annotations

import dataclasses
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import patch

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.timezones import ET
from app.models.holding import Holding
from app.models.news import News
from app.models.news_surfaced import NewsSurfaced
from app.models.price_snapshot import PriceSnapshot
from app.models.report import Report
from app.models.ticker_theme import TickerTheme
from app.services import window_data
from app.services.window_data import (
    BOOTSTRAP_WATERMARK,
    _window_closes,
    compute_global_moves,
    day_window_bounds,
    detect_window_anomalies,
    load_day_news,
    load_news_window,
    lookback_trading_dates,
    mark_news_surfaced,
    resolve_global_moves,
    select_user_anomalies,
    unmark_news_surfaced,
    user_watermark,
)

_USER = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_B = uuid.UUID("00000000-0000-0000-0000-000000000002")


def _news(url: str, when: datetime) -> News:
    return News(url_hash=url, title="t", source="S", url=url, summary="s", published_at=when)


def _close(ticker: str, d: date, close: float) -> PriceSnapshot:
    return PriceSnapshot(
        ticker=ticker,
        market="US",
        session_node="close",
        trade_date=d,
        close=Decimal(str(close)),
    )


def _close_at(ticker: str, d: date, close: float, captured_at: datetime) -> PriceSnapshot:
    return PriceSnapshot(
        ticker=ticker,
        market="US",
        session_node="close",
        trade_date=d,
        close=Decimal(str(close)),
        captured_at=captured_at,
    )


# --- watermark ---------------------------------------------------------------


def test_watermark_cold_start(db_session: Session) -> None:
    assert user_watermark(db_session, _USER, "incremental") == BOOTSTRAP_WATERMARK


def test_watermark_from_last_report(db_session: Session) -> None:
    end = datetime(2026, 6, 10, 20, 30, tzinfo=UTC)
    db_session.add(
        Report(
            user_id=_USER,
            report_date=date(2026, 6, 10),
            report_type="incremental",
            session_node="after_close",
            status="success",
            period_end=end,
        )
    )
    db_session.flush()
    assert user_watermark(db_session, _USER, "incremental") == end


def test_watermark_excludes_the_report_being_regenerated(db_session: Session) -> None:
    """Regenerating a row in place must not read its own period_end back as the
    watermark — that collapses the window (the bug that produced an empty 'quiet
    day' report). Excluding the row by id falls back to the prior report / baseline."""
    prior_end = datetime(2026, 6, 5, 20, 30, tzinfo=UTC)
    db_session.add(
        Report(
            user_id=_USER,
            report_date=date(2026, 6, 5),
            report_type="incremental",
            session_node="after_close",
            status="success",
            period_end=prior_end,
        )
    )
    regen = Report(
        user_id=_USER,
        report_date=date(2026, 6, 8),
        report_type="incremental",
        session_node="after_close",
        status="needs_review",  # a DONE status, so it would otherwise count
        period_end=datetime(2026, 6, 8, 20, 30, tzinfo=UTC),
    )
    db_session.add(regen)
    db_session.flush()

    # Without exclusion the watermark is the regen row's own period_end (6/8).
    assert user_watermark(db_session, _USER, "incremental") == regen.period_end
    # Excluding it falls back to the prior 6/5 report.
    assert user_watermark(db_session, _USER, "incremental", exclude_report_id=regen.id) == prior_end


# --- news window -------------------------------------------------------------


def test_load_news_window_filters_by_published_at_upper_bound_only(db_session: Session) -> None:
    """No lower bound (H-DEBT-3 / issue #30): an unsurfaced item published
    before `start` is still selected — a strict lower bound is exactly what
    caused the permanent-miss bug this fixes, so only the upper bound
    (`<= end`) and the surfaced-dedup ledger gate selection now."""
    db_session.add_all(
        [
            _news("a", datetime(2026, 6, 1, tzinfo=UTC)),  # before window, never surfaced
            _news("b", datetime(2026, 6, 3, tzinfo=UTC)),  # in window
            _news("c", datetime(2026, 6, 5, tzinfo=UTC)),  # in window
            _news("d", datetime(2026, 6, 9, tzinfo=UTC)),  # after window
        ]
    )
    db_session.flush()
    items = load_news_window(
        db_session, datetime(2026, 6, 2, tzinfo=UTC), datetime(2026, 6, 6, tzinfo=UTC), _USER
    )
    assert {i.url_hash for i in items} == {"a", "b", "c"}


def test_load_news_window_excludes_already_surfaced(db_session: Session) -> None:
    """The permanent-miss regression: 'straggler' is published inside window A's
    date range but only ingested after window A already ran, so window A never
    sees it. The old `> start` lower bound would then also exclude it from
    window B (whose start = window A's period_end). The fix selects it once
    (the first window run after ingestion) and never again once that window's
    report is marked done."""
    report = Report(
        user_id=_USER,
        report_date=date(2026, 6, 5),
        report_type="incremental",
        session_node="after_close",
        status="success",
        period_start=datetime(2026, 6, 3, tzinfo=UTC),
        period_end=datetime(2026, 6, 5, tzinfo=UTC),
    )
    db_session.add(report)
    db_session.add(_news("straggler", datetime(2026, 6, 4, tzinfo=UTC)))
    db_session.flush()

    # Window B picks up the straggler (published in A's range, never surfaced).
    items = load_news_window(
        db_session, datetime(2026, 6, 5, tzinfo=UTC), datetime(2026, 6, 7, tzinfo=UTC), _USER
    )
    assert {i.url_hash for i in items} == {"straggler"}

    mark_news_surfaced(db_session, _USER, report.id, [i.url_hash for i in items])
    db_session.flush()

    # A later window (e.g. a same-day manual re-run) must not resurface it.
    items_again = load_news_window(
        db_session, datetime(2026, 6, 5, tzinfo=UTC), datetime(2026, 6, 8, tzinfo=UTC), _USER
    )
    assert items_again == []


def test_load_news_window_surfaced_is_scoped_per_user(db_session: Session) -> None:
    """PR #139 review: `news` is a global capture-layer store, but each user's
    report stream has its own watermark/window. Marking an item surfaced for
    User A must not hide it from User B, who may legitimately need to see it
    for the first time in a report of their own."""
    report_a = Report(
        user_id=_USER,
        report_date=date(2026, 6, 5),
        report_type="incremental",
        session_node="after_close",
        status="success",
        period_start=datetime(2026, 6, 3, tzinfo=UTC),
        period_end=datetime(2026, 6, 5, tzinfo=UTC),
    )
    db_session.add(report_a)
    db_session.add(_news("shared", datetime(2026, 6, 4, tzinfo=UTC)))
    db_session.flush()

    mark_news_surfaced(db_session, _USER, report_a.id, ["shared"])
    db_session.flush()

    # User A: already surfaced, excluded.
    items_a = load_news_window(
        db_session, datetime(2026, 6, 5, tzinfo=UTC), datetime(2026, 6, 8, tzinfo=UTC), _USER
    )
    assert items_a == []

    # User B: never surfaced for them, still selectable.
    items_b = load_news_window(
        db_session, datetime(2026, 6, 1, tzinfo=UTC), datetime(2026, 6, 8, tzinfo=UTC), _USER_B
    )
    assert {i.url_hash for i in items_b} == {"shared"}


# --- L1's own day-scoped window/news (design doc §4.8, second addendum) -----


def test_day_window_bounds_spans_exactly_one_et_calendar_day() -> None:
    """`day_window_bounds` is a pure function of `trade_date` — no session,
    no user, nothing else. This is what makes it safe as L1's window
    source: it cannot vary by who calls it."""
    start, end = day_window_bounds(date(2026, 6, 5))
    assert start.astimezone(ET).date() == date(2026, 6, 5)
    assert end.astimezone(ET).date() == date(2026, 6, 5)
    assert start < end


def test_lookback_trading_dates_is_a_pure_function_of_the_end_date() -> None:
    """Issue #128: L1 may carry multi-day headlines, but the date list
    cannot come from a user's watermark. Five weekdays ending on a Friday
    are the prior Mon-Fri; a Monday lookback skips the weekend."""
    assert lookback_trading_dates(date(2026, 8, 17), n=5) == [
        date(2026, 8, 11),
        date(2026, 8, 12),
        date(2026, 8, 13),
        date(2026, 8, 14),
        date(2026, 8, 17),
    ]
    monday = lookback_trading_dates(date(2026, 8, 17), n=1)
    assert monday == [date(2026, 8, 17)]


def test_lookback_trading_dates_always_includes_a_weekend_end() -> None:
    """Issue #178 regression: a manual report run's `eff_date` is whatever
    real ET calendar date it happens to run on (report_generator.py's
    `eff_date = report_date or now.astimezone(ET).date()`), which is not
    guaranteed to be a weekday — the scheduled batch only fires Mon/Wed/Fri,
    but a manual re-run (a documented, supported case — see CLAUDE.md's
    "manual quiet window") can happen on a Saturday/Sunday.

    Before the fix, `end` itself was silently dropped whenever it fell on a
    weekend (the loop's very first `cursor` value never passed
    `weekday() < 5`), so `lookback_moves.get(eff_date, {})` in
    `report_generator.py` always missed — every L1 candidate's `day_pct`
    came out `None` regardless of whether a real close existed for that
    date, and `get_l1_intel_batch` (`ticker_intel.py`) silently skips any
    candidate whose facts have `day_pct is None`. `end` must always be the
    list's last element, exactly as it is for a weekday `end` (the existing
    test above), since every caller of this function treats the last/`end`
    element as "today".

    The Saturday `n=5` case below is five *consecutive* calendar days
    (Tue-Sat), so on its own it would not catch a prefix that dropped its
    `weekday() < 5` filter and just walked back `n - 1` calendar days
    unconditionally — a Sunday `end` is the smallest input where the prefix
    must actually skip a weekend day (PR #179 review round 1 suggestion)."""
    saturday = date(2026, 8, 22)
    assert saturday.weekday() == 5

    result = lookback_trading_dates(saturday, n=5)

    assert result[-1] == saturday
    assert result == [
        date(2026, 8, 18),
        date(2026, 8, 19),
        date(2026, 8, 20),
        date(2026, 8, 21),
        date(2026, 8, 22),
    ]

    sunday = date(2026, 8, 23)
    assert lookback_trading_dates(sunday, n=5) == [
        date(2026, 8, 18),
        date(2026, 8, 19),
        date(2026, 8, 20),
        date(2026, 8, 21),
        date(2026, 8, 23),
    ]


def test_load_day_news_has_no_user_parameter_and_ignores_surfaced_ledger(
    db_session: Session,
) -> None:
    """L1's news source must never route through `news_surfaced` (a per-user
    Pass-2 dedup ledger) — doing so would make L1's candidate news set
    depend on which user's report happens to mark it first, reintroducing
    the exact per-user-contamination class this design closes. Unlike
    `load_news_window`, `load_day_news` takes no `user_id` at all."""
    report = Report(
        user_id=_USER,
        report_date=date(2026, 6, 5),
        report_type="incremental",
        session_node="after_close",
        status="success",
        period_start=datetime(2026, 6, 3, tzinfo=UTC),
        period_end=datetime(2026, 6, 5, tzinfo=UTC),
    )
    db_session.add(report)
    db_session.add_all(
        [
            _news("on_day", datetime(2026, 6, 5, 15, 0, tzinfo=UTC)),  # 11:00 ET Jun 5
            _news("before_day", datetime(2026, 6, 4, 23, 0, tzinfo=UTC)),  # 19:00 ET Jun 4
            _news("after_day", datetime(2026, 6, 6, 5, 0, tzinfo=UTC)),  # 01:00 ET Jun 6
        ]
    )
    db_session.flush()

    mark_news_surfaced(db_session, _USER, report.id, ["on_day"])
    db_session.flush()

    items = load_day_news(db_session, date(2026, 6, 5))

    assert {i.url_hash for i in items} == {"on_day"}


def test_unmark_news_surfaced_restores_candidate_set_for_retry(db_session: Session) -> None:
    """PR #139 review: generate_report reopens a needs_review row and reuses its
    frozen window. Without unmarking, the retry's load_news_window would see
    the first attempt's own marks and silently select a different (smaller)
    news set for the identical window."""
    report = Report(
        user_id=_USER,
        report_date=date(2026, 6, 5),
        report_type="incremental",
        session_node="after_close",
        status="needs_review",
        period_start=datetime(2026, 6, 3, tzinfo=UTC),
        period_end=datetime(2026, 6, 5, tzinfo=UTC),
    )
    db_session.add(report)
    db_session.add(_news("flagged", datetime(2026, 6, 4, tzinfo=UTC)))
    db_session.flush()

    mark_news_surfaced(db_session, _USER, report.id, ["flagged"])
    db_session.flush()
    assert (
        load_news_window(
            db_session, datetime(2026, 6, 5, tzinfo=UTC), datetime(2026, 6, 6, tzinfo=UTC), _USER
        )
        == []
    )

    unmark_news_surfaced(db_session, report.id)
    db_session.flush()

    items = load_news_window(
        db_session, datetime(2026, 6, 5, tzinfo=UTC), datetime(2026, 6, 6, tzinfo=UTC), _USER
    )
    assert {i.url_hash for i in items} == {"flagged"}


def test_unmark_news_surfaced_noop_for_report_with_no_marks(db_session: Session) -> None:
    """A retry of a `failed` row (which never reached mark_news_surfaced) must
    not raise even though there's nothing to delete."""
    report = Report(
        user_id=_USER,
        report_date=date(2026, 6, 5),
        report_type="incremental",
        session_node="after_close",
        status="failed",
        period_start=datetime(2026, 6, 3, tzinfo=UTC),
        period_end=datetime(2026, 6, 5, tzinfo=UTC),
    )
    db_session.add(report)
    db_session.flush()

    unmark_news_surfaced(db_session, report.id)  # must not raise


def test_mark_news_surfaced_idempotent_on_redelivery(db_session: Session) -> None:
    """A Celery redelivery (task_acks_late) of the same generation run must not
    raise IntegrityError re-inserting the same (user_id, news_id) pair."""
    report = Report(
        user_id=_USER,
        report_date=date(2026, 6, 5),
        report_type="incremental",
        session_node="after_close",
        status="success",
        period_start=datetime(2026, 6, 3, tzinfo=UTC),
        period_end=datetime(2026, 6, 5, tzinfo=UTC),
    )
    db_session.add(report)
    db_session.add(_news("dup", datetime(2026, 6, 4, tzinfo=UTC)))
    db_session.flush()

    mark_news_surfaced(db_session, _USER, report.id, ["dup"])
    db_session.flush()
    mark_news_surfaced(db_session, _USER, report.id, ["dup"])  # redelivery — must not raise
    db_session.flush()

    count = db_session.execute(select(func.count()).select_from(NewsSurfaced)).scalar_one()
    assert count == 1


def test_mark_news_surfaced_noop_on_empty_list(db_session: Session) -> None:
    report = Report(
        user_id=_USER,
        report_date=date(2026, 6, 5),
        report_type="incremental",
        session_node="after_close",
        status="skipped",
        period_start=datetime(2026, 6, 3, tzinfo=UTC),
        period_end=datetime(2026, 6, 5, tzinfo=UTC),
    )
    db_session.add(report)
    db_session.flush()

    mark_news_surfaced(db_session, _USER, report.id, [])  # must not raise or query

    count = db_session.execute(select(func.count()).select_from(NewsSurfaced)).scalar_one()
    assert count == 0


# --- anomalies from snapshots ------------------------------------------------


def test_detect_window_anomalies_flags_move_over_threshold(db_session: Session) -> None:
    db_session.add(
        Holding(
            user_id=_USER,
            name="Apple",
            ticker="AAPL",
            pricing_mode="auto",
            currency="USD",
            asset_type="stock",
        )
    )
    start = datetime(2026, 6, 2, 16, 0, tzinfo=UTC)
    db_session.add_all(
        [
            _close_at("AAPL", date(2026, 6, 2), 100.0, start),  # baseline (captured at start)
            _close("AAPL", date(2026, 6, 5), 105.0),  # +5% > 3% stock threshold
        ]
    )
    db_session.flush()

    anomalies, trading_days = detect_window_anomalies(
        db_session,
        datetime(2026, 6, 2, 16, 0, tzinfo=UTC),
        datetime(2026, 6, 5, 20, 30, tzinfo=UTC),
        _USER,
    )
    assert len(anomalies) == 1
    a = anomalies[0]
    assert a.identifier == "AAPL"
    assert a.prev_price == Decimal("100.0")
    assert a.current_price == Decimal("105.0")
    assert trading_days == 1  # one close trade_date inside (start_date, end_date]


def test_detect_window_anomalies_ignores_small_move_and_new_position(db_session: Session) -> None:
    db_session.add_all(
        [
            Holding(
                user_id=_USER,
                name="Apple",
                ticker="AAPL",
                pricing_mode="auto",
                currency="USD",
                asset_type="stock",
            ),
            Holding(
                user_id=_USER,
                name="NewCo",
                ticker="NEW",
                pricing_mode="auto",
                currency="USD",
                asset_type="stock",
            ),
        ]
    )
    db_session.add_all(
        [
            _close_at("AAPL", date(2026, 6, 2), 100.0, datetime(2026, 6, 2, 16, 0, tzinfo=UTC)),
            _close("AAPL", date(2026, 6, 5), 101.0),  # +1% < 3% → not flagged
            _close("NEW", date(2026, 6, 5), 50.0),  # no baseline before start → skipped
        ]
    )
    db_session.flush()

    anomalies, _ = detect_window_anomalies(
        db_session,
        datetime(2026, 6, 2, 16, 0, tzinfo=UTC),
        datetime(2026, 6, 5, 20, 30, tzinfo=UTC),
        _USER,
    )
    assert anomalies == []


def _stock(name: str, ticker: str) -> Holding:
    return Holding(
        user_id=_USER,
        name=name,
        ticker=ticker,
        pricing_mode="auto",
        currency="USD",
        asset_type="stock",
    )


def test_cumulative_threshold_scales_with_trading_days(db_session: Session) -> None:
    """A gradual drift (no single day beyond the per-day threshold) is judged only
    by the cumulative threshold (3% x trading_days). AAPL's +5% net over 3 days,
    with each day < 3%, clears neither trigger; BIGM's one +10% day fires the
    single-day trigger."""
    db_session.add_all([_stock("Apple", "AAPL"), _stock("BigMove", "BIGM")])
    start = datetime(2026, 6, 2, 16, 0, tzinfo=UTC)
    db_session.add_all(
        [
            _close_at("AAPL", date(2026, 6, 2), 100.0, start),  # baseline
            _close("AAPL", date(2026, 6, 3), 101.5),  # +1.5%
            _close("AAPL", date(2026, 6, 4), 103.0),  # +1.48%
            _close("AAPL", date(2026, 6, 5), 105.0),  # +1.94%; net +5% < 9% cumulative
            _close_at("BIGM", date(2026, 6, 2), 100.0, start),
            _close("BIGM", date(2026, 6, 5), 110.0),  # single +10% day → single-day trigger
        ]
    )
    db_session.flush()

    anomalies, trading_days = detect_window_anomalies(
        db_session,
        datetime(2026, 6, 2, 16, 0, tzinfo=UTC),
        datetime(2026, 6, 5, 20, 30, tzinfo=UTC),
        _USER,
    )
    assert trading_days == 3
    assert [a.identifier for a in anomalies] == ["BIGM"]


def test_single_day_trigger_catches_violent_session(db_session: Session) -> None:
    """The point-7 fix: a holding whose net move over the window is small but which
    had one violent trading day must still flag (a flat net would smooth it away)."""
    db_session.add_all([_stock("Whip", "WHIP")])
    db_session.add_all(
        [
            _close_at(
                "WHIP", date(2026, 6, 1), 100.0, datetime(2026, 6, 1, 16, 0, tzinfo=UTC)
            ),  # baseline
            _close("WHIP", date(2026, 6, 2), 100.0),
            _close("WHIP", date(2026, 6, 3), 100.0),
            _close("WHIP", date(2026, 6, 4), 88.0),  # -12% crash on one day
            _close("WHIP", date(2026, 6, 5), 100.5),  # recovers; net only +0.5%
        ]
    )
    db_session.flush()

    anomalies, _ = detect_window_anomalies(
        db_session,
        datetime(2026, 6, 1, 16, 0, tzinfo=UTC),
        datetime(2026, 6, 5, 20, 30, tzinfo=UTC),
        _USER,
    )
    assert [a.identifier for a in anomalies] == ["WHIP"]
    a = anomalies[0]
    assert abs(a.window_net_pct or Decimal(0)) < Decimal("0.01")  # net is tiny
    # but a single session moved well beyond the per-day threshold
    assert a.max_day_pct is not None and abs(a.max_day_pct) >= Decimal("0.10")
    assert a.trigger == "single_day"


def test_cumulative_threshold_capped_at_ten_percent(db_session: Session) -> None:
    """A pure cumulative drift (every day < 3%) flags only once net clears the 10%
    cap: BIG (+~10.5% net) flags, MID (+7% net) does not. Neither has a single day
    beyond the per-day threshold, so this isolates the cumulative cap."""
    db_session.add_all([_stock("Big", "BIG"), _stock("Mid", "MID")])
    start = datetime(2026, 6, 1, 16, 0, tzinfo=UTC)
    db_session.add_all(
        [
            _close_at("BIG", date(2026, 6, 1), 100.0, start),  # baseline
            _close("BIG", date(2026, 6, 2), 102.0),  # +2.0%
            _close("BIG", date(2026, 6, 3), 104.0),  # +1.96%
            _close("BIG", date(2026, 6, 4), 106.0),  # +1.92%
            _close("BIG", date(2026, 6, 5), 108.0),  # +1.89%
            _close("BIG", date(2026, 6, 8), 110.5),  # +2.31%; net +10.5% ≥ 10% cap
            _close_at("MID", date(2026, 6, 1), 100.0, start),
            _close("MID", date(2026, 6, 2), 101.5),
            _close("MID", date(2026, 6, 3), 103.0),
            _close("MID", date(2026, 6, 4), 104.5),
            _close("MID", date(2026, 6, 5), 106.0),
            _close("MID", date(2026, 6, 8), 107.0),  # net +7% < 10% cap → not flagged
        ]
    )
    db_session.flush()

    anomalies, trading_days = detect_window_anomalies(
        db_session,
        datetime(2026, 6, 1, 16, 0, tzinfo=UTC),
        datetime(2026, 6, 8, 20, 30, tzinfo=UTC),
        _USER,
    )
    assert trading_days == 5
    assert [a.identifier for a in anomalies] == ["BIG"]
    assert anomalies[0].threshold == Decimal("0.10")  # effective threshold = cap
    assert anomalies[0].trigger == "cumulative"


# --- premarket multi-day window: start_date's own close is in-window --------


def test_premarket_window_includes_start_date_close_as_anomaly(db_session: Session) -> None:
    """A multi-day window whose start falls BEFORE that day's market close (a
    premarket manual run, e.g. period_start = 08:13 ET on day D) must treat day
    D's close — captured at 16:00 ET, after period_start — as the first
    in-window trading day, not as the baseline. Previously `trade_date >
    start_date` excluded D entirely, the baseline absorbed D's close, and a
    violent move on D (e.g. +11%) went undetected."""
    db_session.add(_stock("Intel", "INTC"))
    start = datetime(2026, 6, 9, 8, 13, tzinfo=ET)
    end = datetime(2026, 6, 10, 8, 38, tzinfo=ET)
    db_session.add_all(
        [
            # Last close before the window opens (captured well before start).
            _close_at("INTC", date(2026, 6, 6), 100.0, datetime(2026, 6, 6, 16, 0, tzinfo=ET)),
            # D's close, captured at 16:00 ET on D — after period_start (08:13).
            _close_at("INTC", date(2026, 6, 9), 111.0, datetime(2026, 6, 9, 16, 0, tzinfo=ET)),
        ]
    )
    db_session.flush()

    anomalies, trading_days = detect_window_anomalies(db_session, start, end, _USER)
    assert trading_days == 1
    assert [a.identifier for a in anomalies] == ["INTC"]
    a = anomalies[0]
    assert a.prev_price == Decimal("100.0")
    assert a.current_price == Decimal("111.0")
    assert a.trigger == "single_day"


# --- same-day window collapse fix --------------------------------------------


def test_window_closes_same_day_excludes_pre_window_capture(db_session: Session) -> None:
    """A same-day window (start_date == end_date) cannot use the date-range
    query (it's empty by construction), so it falls back to captured_at > start.
    A close captured BEFORE the window started must be excluded."""
    start = datetime(2026, 6, 5, 10, 0, tzinfo=ET)
    end = datetime(2026, 6, 5, 17, 0, tzinfo=ET)
    db_session.add(_close_at("AAA", date(2026, 6, 5), 100.0, datetime(2026, 6, 5, 8, 0, tzinfo=ET)))
    db_session.flush()

    assert _window_closes(db_session, "AAA", start, end) == []


def test_window_closes_same_day_includes_post_window_capture(db_session: Session) -> None:
    """A close captured DURING the same-day window must be included even though
    the multi-day trade_date-range query would be empty."""
    start = datetime(2026, 6, 5, 10, 0, tzinfo=ET)
    end = datetime(2026, 6, 5, 17, 0, tzinfo=ET)
    db_session.add(
        _close_at("BBB", date(2026, 6, 5), 101.0, datetime(2026, 6, 5, 16, 5, tzinfo=ET))
    )
    db_session.flush()

    series = _window_closes(db_session, "BBB", start, end)
    assert len(series) == 1
    assert series[0].close == Decimal("101.0")


def test_trading_days_same_day_window_counts_captured_close(db_session: Session) -> None:
    """detect_window_anomalies' trading_days count uses the same same-day
    fallback: a close captured after period_start counts as one trading day."""
    start = datetime(2026, 6, 5, 10, 0, tzinfo=ET)
    end = datetime(2026, 6, 5, 17, 0, tzinfo=ET)
    db_session.add(
        _close_at("CCC", date(2026, 6, 5), 100.0, datetime(2026, 6, 5, 16, 5, tzinfo=ET))
    )
    db_session.flush()

    _, trading_days = detect_window_anomalies(db_session, start, end, _USER)
    assert trading_days == 1


def test_trading_days_same_day_window_excludes_stale_capture(db_session: Session) -> None:
    """A close captured before period_start (e.g. a backfilled row sharing
    today's trade_date) must not count toward trading_days for a same-day window."""
    start = datetime(2026, 6, 5, 10, 0, tzinfo=ET)
    end = datetime(2026, 6, 5, 17, 0, tzinfo=ET)
    db_session.add(_close_at("DDD", date(2026, 6, 5), 100.0, datetime(2026, 6, 5, 8, 0, tzinfo=ET)))
    db_session.flush()

    _, trading_days = detect_window_anomalies(db_session, start, end, _USER)
    assert trading_days == 0


# --- L0 split: compute_global_moves / select_user_anomalies (issue #128 A1) --


def _hk_holding(user_id: uuid.UUID, name: str, ticker: str, asset_class: str) -> Holding:
    return Holding(
        user_id=user_id,
        name=name,
        ticker=ticker,
        pricing_mode="auto",
        currency="USD",
        asset_type="stock",
        asset_class=asset_class,
    )


def test_select_user_anomalies_threshold_differs_by_user_asset_class(db_session: Session) -> None:
    """The whole point of keeping threshold judgment per-user (design doc
    §3.3): the SAME identifier's SAME global move can clear one user's
    threshold and not another's, because the two users classified it under
    different asset_class rows. STOCK cumulative_cap=0.10, EQUITY_US_TECH
    cumulative_cap=0.35 (both per_day=0.05) — a 12% net drift with no single
    day >= 5% clears STOCK's cap but not EQUITY_US_TECH's."""
    db_session.add_all(
        [
            _hk_holding(_USER, "Apple", "AAPL", "STOCK"),
            _hk_holding(_USER_B, "Apple", "AAPL", "EQUITY_US_TECH"),
        ]
    )
    start = datetime(2026, 6, 2, 16, 0, tzinfo=UTC)
    db_session.add_all(
        [
            _close_at("AAPL", date(2026, 6, 2), 100.0, start),  # baseline
            _close("AAPL", date(2026, 6, 3), 103.0),  # +3%
            _close("AAPL", date(2026, 6, 4), 106.09),  # +3%
            _close("AAPL", date(2026, 6, 5), 109.27),  # +3%
            _close("AAPL", date(2026, 6, 6), 112.55),  # +3%; net ~+12.5%
        ]
    )
    db_session.flush()
    end = datetime(2026, 6, 6, 20, 30, tzinfo=UTC)

    stock_anomalies, trading_days = detect_window_anomalies(db_session, start, end, _USER)
    tech_anomalies, _ = detect_window_anomalies(db_session, start, end, _USER_B)

    assert trading_days == 4
    assert [a.identifier for a in stock_anomalies] == ["AAPL"]  # STOCK cap 10% cleared
    assert stock_anomalies[0].trigger == "cumulative"
    assert tech_anomalies == []  # EQUITY_US_TECH cap 35%*trading_days-capped 20% not cleared


def test_compute_global_moves_computes_shared_identifier_once(db_session: Session) -> None:
    """Two different users' Holding rows for the same identifier must not
    cause its price series to be fetched/computed twice."""
    db_session.add_all(
        [
            _hk_holding(_USER, "NVIDIA", "NVDA", "EQUITY_US_TECH"),
            _hk_holding(_USER_B, "NVIDIA", "NVDA", "EQUITY_US_TECH"),
        ]
    )
    start = datetime(2026, 6, 2, 16, 0, tzinfo=UTC)
    db_session.add_all(
        [
            _close_at("NVDA", date(2026, 6, 2), 200.0, start),
            _close("NVDA", date(2026, 6, 3), 215.0),  # +7.5% single day
        ]
    )
    db_session.flush()
    end = datetime(2026, 6, 3, 20, 30, tzinfo=UTC)

    with patch(
        "app.services.window_data._compute_identifier_move",
        wraps=window_data._compute_identifier_move,
    ) as spy:
        moves, _ = compute_global_moves(db_session, start, end)

    assert spy.call_count == 1  # NVDA queried once, not once per holding row
    assert set(moves.keys()) == {"NVDA"}


def test_resolve_global_moves_with_day_bounds_yields_single_trading_day_move(
    db_session: Session,
) -> None:
    """L1's window (design doc §4.8, second addendum) is `day_window_bounds`,
    NOT any user's `[period_start, period_end]`. Two users whose OWN report
    windows differ wildly (one spans a single day, the other spans the full
    5-day run below) must still resolve to the IDENTICAL day-scoped move for
    the trade_date they share, because `day_window_bounds` never reads
    either of their `period_start`s at all — it's a pure function of the
    trade_date."""
    db_session.add(_hk_holding(_USER, "Apple", "AAPL", "EQUITY_US_TECH"))
    db_session.add_all(
        [
            _close_at("AAPL", date(2026, 6, 2), 100.0, datetime(2026, 6, 2, 16, 0, tzinfo=UTC)),
            _close("AAPL", date(2026, 6, 3), 103.0),
            _close("AAPL", date(2026, 6, 4), 106.09),
            _close("AAPL", date(2026, 6, 5), 109.27),
            _close("AAPL", date(2026, 6, 6), 112.55),  # today's close: +3% vs Jun 5's 109.27
        ]
    )
    db_session.flush()

    # User A's own report window: the full 5-day run (what §4.2 renders for them).
    user_a_moves, _ = resolve_global_moves(
        db_session,
        datetime(2026, 6, 2, 16, 0, tzinfo=UTC),
        datetime(2026, 6, 6, 20, 30, tzinfo=UTC),
    )
    # User B's own report window: just today (a fresh user, or a short manual re-run).
    user_b_moves, _ = resolve_global_moves(
        db_session,
        datetime(2026, 6, 5, 20, 30, tzinfo=UTC),
        datetime(2026, 6, 6, 20, 30, tzinfo=UTC),
    )
    assert user_a_moves["AAPL"].net_pct != user_b_moves["AAPL"].net_pct  # their OWN windows differ

    # L1's day-scoped window ignores both of the above entirely.
    day_start, day_end = day_window_bounds(date(2026, 6, 6))
    day_moves, _ = resolve_global_moves(db_session, day_start, day_end)

    assert day_moves["AAPL"].net_pct == Decimal("0.0300")  # (112.55-109.27)/109.27, quantized
    assert day_moves["AAPL"].prev_price == Decimal("109.27")
    assert day_moves["AAPL"].current_price == Decimal("112.55")


def test_select_user_anomalies_no_cross_user_leakage(db_session: Session) -> None:
    """Two users with disjoint holdings: neither user's anomaly list may
    contain the other's identifier, even though both are computed from the
    same shared `moves` dict (issue #128 A1 §1.3 — the core regression this
    checkpoint exists to fix)."""
    db_session.add_all(
        [
            _hk_holding(_USER, "NVIDIA", "NVDA", "EQUITY_US_TECH"),
            _hk_holding(_USER_B, "Apple", "AAPL", "STOCK"),
        ]
    )
    start = datetime(2026, 6, 2, 16, 0, tzinfo=UTC)
    db_session.add_all(
        [
            _close_at("NVDA", date(2026, 6, 2), 200.0, start),
            _close("NVDA", date(2026, 6, 3), 215.0),  # +7.5%
            _close_at("AAPL", date(2026, 6, 2), 100.0, start),
            _close("AAPL", date(2026, 6, 3), 106.0),  # +6%
        ]
    )
    db_session.flush()
    end = datetime(2026, 6, 3, 20, 30, tzinfo=UTC)

    moves, trading_days = compute_global_moves(db_session, start, end)
    assert set(moves.keys()) == {"NVDA", "AAPL"}  # computed globally, both present

    theme_map = window_data._load_theme_map(db_session)

    def _holdings_of(user_id: uuid.UUID) -> list[Holding]:
        return list(
            db_session.execute(select(Holding).where(Holding.user_id == user_id)).scalars().all()
        )

    user_a_anomalies = select_user_anomalies(moves, _holdings_of(_USER), trading_days, theme_map)
    user_b_anomalies = select_user_anomalies(moves, _holdings_of(_USER_B), trading_days, theme_map)

    assert [a.identifier for a in user_a_anomalies] == ["NVDA"]
    assert [a.identifier for a in user_b_anomalies] == ["AAPL"]


def test_select_user_anomalies_skips_manual_pricing_mode(db_session: Session) -> None:
    """A manually-priced holding must never be flagged even if its ticker
    happens to be in the shared `moves` dict (e.g. because another user
    auto-prices the same identifier) — belt-and-suspenders alongside
    global_identifier_universe already excluding manual holdings."""
    manual = _hk_holding(_USER, "NVIDIA (manual)", "NVDA", "EQUITY_US_TECH")
    manual.pricing_mode = "manual"
    db_session.add(manual)
    start = datetime(2026, 6, 2, 16, 0, tzinfo=UTC)
    db_session.add_all(
        [
            _close_at("NVDA", date(2026, 6, 2), 200.0, start),
            _close("NVDA", date(2026, 6, 3), 215.0),
        ]
    )
    db_session.flush()
    end = datetime(2026, 6, 3, 20, 30, tzinfo=UTC)

    moves, trading_days = compute_global_moves(db_session, start, end)
    theme_map = window_data._load_theme_map(db_session)
    anomalies = select_user_anomalies(moves, [manual], trading_days, theme_map)
    assert anomalies == []


def test_select_user_anomalies_matches_mixed_case_ticker_to_global_move(
    db_session: Session,
) -> None:
    """PR #151 review round 2: compute_global_moves/global_identifier_universe
    key `moves` by the UPPERCASED normalized identifier. A holding whose
    ticker isn't already uppercase — possible via POST /holdings/confirm,
    which accepts ParsedRow.ticker as-is and bypasses the upload parser's
    case normalization — must still match its own globally-computed move,
    not silently miss it because select_user_anomalies looked up with a
    different casing."""
    db_session.add(_hk_holding(_USER, "Apple", "aapl", "STOCK"))
    start = datetime(2026, 6, 2, 16, 0, tzinfo=UTC)
    db_session.add_all(
        [
            _close_at("AAPL", date(2026, 6, 2), 100.0, start),
            _close("AAPL", date(2026, 6, 3), 106.0),  # +6%
        ]
    )
    db_session.flush()
    end = datetime(2026, 6, 3, 20, 30, tzinfo=UTC)

    anomalies, _ = detect_window_anomalies(db_session, start, end, _USER)
    assert [a.identifier for a in anomalies] == ["AAPL"]


def test_detect_window_anomalies_cache_shares_compute_global_moves_across_calls(
    db_session: Session,
) -> None:
    """A shared `moves_cache` dict, passed to two detect_window_anomalies
    calls for the SAME window, must make compute_global_moves run only once
    — this is the mechanism (design doc §3.8) that keeps a multi-user batch's
    per-identifier compute cost from scaling with user count."""
    db_session.add_all(
        [
            _hk_holding(_USER, "NVIDIA", "NVDA", "EQUITY_US_TECH"),
            _hk_holding(_USER_B, "NVIDIA", "NVDA", "EQUITY_US_TECH"),
        ]
    )
    start = datetime(2026, 6, 2, 16, 0, tzinfo=UTC)
    db_session.add_all(
        [
            _close_at("NVDA", date(2026, 6, 2), 200.0, start),
            _close("NVDA", date(2026, 6, 3), 215.0),
        ]
    )
    db_session.flush()
    end = datetime(2026, 6, 3, 20, 30, tzinfo=UTC)

    cache: window_data.MovesCache = {}
    with patch(
        "app.services.window_data.compute_global_moves", wraps=window_data.compute_global_moves
    ) as spy:
        anomalies_a, _ = detect_window_anomalies(db_session, start, end, _USER, cache)
        anomalies_b, _ = detect_window_anomalies(db_session, start, end, _USER_B, cache)

    assert spy.call_count == 1
    assert [a.identifier for a in anomalies_a] == ["NVDA"]
    assert [a.identifier for a in anomalies_b] == ["NVDA"]


def test_detect_window_anomalies_without_cache_recomputes_each_call(db_session: Session) -> None:
    """Omitting moves_cache (every pre-A1 call site) preserves the old
    per-call behavior — no accidental cross-call state leakage between
    independent single-report generations."""
    db_session.add(_hk_holding(_USER, "NVIDIA", "NVDA", "EQUITY_US_TECH"))
    start = datetime(2026, 6, 2, 16, 0, tzinfo=UTC)
    db_session.add_all(
        [
            _close_at("NVDA", date(2026, 6, 2), 200.0, start),
            _close("NVDA", date(2026, 6, 3), 215.0),
        ]
    )
    db_session.flush()
    end = datetime(2026, 6, 3, 20, 30, tzinfo=UTC)

    with patch(
        "app.services.window_data.compute_global_moves", wraps=window_data.compute_global_moves
    ) as spy:
        detect_window_anomalies(db_session, start, end, _USER)
        detect_window_anomalies(db_session, start, end, _USER)

    assert spy.call_count == 2


def test_detect_window_anomalies_single_user_golden_fields(db_session: Session) -> None:
    """PR #151 review: design §3.7/§3.8 requires the split to leave a
    single-user anomaly list field-EQUAL to the pre-split function. The
    existing detect_window_anomalies_* tests above check only a few headline
    fields (identifier/prev_price/current_price/trigger/threshold) — a
    silent drop or rename elsewhere in the HoldingMove -> PriceAnomaly
    rebuild (session-arc fields, theme merge fields, constituents) wouldn't
    fail any of them. This locks the FULL dataclass, standalone and themed,
    against a synthetic (not production-seeded) TickerTheme row so it can't
    be silently invalidated by a future ticker_themes seed migration edit.
    """
    db_session.add_all(
        [
            _stock("Standalone Co", "STANDA"),
            Holding(
                user_id=_USER,
                name="Theme Big",
                ticker="THMBIG",
                pricing_mode="auto",
                currency="USD",
                asset_type="stock",
                asset_class="STOCK",
                current_value=Decimal("9000"),
            ),
            Holding(
                user_id=_USER,
                name="Theme Small",
                ticker="THMSML",
                pricing_mode="auto",
                currency="USD",
                asset_type="stock",
                asset_class="STOCK",
                current_value=Decimal("1000"),
            ),
        ]
    )
    db_session.add_all(
        [
            TickerTheme(
                ticker="THMBIG",
                theme="golden_theme",
                theme_label_zh="金测试主题",
                theme_label_en="Golden Test Theme",
                asset_class="STOCK",
            ),
            TickerTheme(
                ticker="THMSML",
                theme="golden_theme",
                theme_label_zh="金测试主题",
                theme_label_en="Golden Test Theme",
                asset_class="STOCK",
            ),
        ]
    )
    start = datetime(2026, 6, 2, 16, 0, tzinfo=UTC)
    db_session.add_all(
        [
            _close_at("STANDA", date(2026, 6, 2), 100.0, start),
            _close("STANDA", date(2026, 6, 3), 106.0),  # +6%, standalone (no theme row)
            _close_at("THMBIG", date(2026, 6, 2), 200.0, start),
            _close("THMBIG", date(2026, 6, 3), 210.0),  # +5%, dominant (current_value=9000)
            _close_at("THMSML", date(2026, 6, 2), 50.0, start),
            _close("THMSML", date(2026, 6, 3), 53.5),  # +7%, minor (current_value=1000)
        ]
    )
    db_session.flush()
    end = datetime(2026, 6, 3, 20, 30, tzinfo=UTC)

    anomalies, trading_days = detect_window_anomalies(db_session, start, end, _USER)
    assert trading_days == 1

    by_identifier = {a.identifier: a for a in anomalies}
    assert set(by_identifier) == {"STANDA", "golden_theme"}

    assert dataclasses.asdict(by_identifier["STANDA"]) == {
        "name": "Standalone Co",
        "identifier": "STANDA",
        "asset_type": "STOCK",
        "current_price": Decimal("106.0"),
        "prev_price": Decimal("100.0"),
        "pct_change": Decimal("0.0600"),
        "threshold": Decimal("0.05"),
        "trigger": "single_day",
        "market": "US",
        "baseline_date": date(2026, 6, 2),
        "latest_date": date(2026, 6, 3),
        "window_net_pct": Decimal("0.0600"),
        "max_day_pct": Decimal("0.0600"),
        "max_day_date": date(2026, 6, 3),
        "prev_close": Decimal("100.0"),
        "day_open": None,
        "day_high": None,
        "day_low": None,
        "day_close": Decimal("106.0"),
        "after_hours": None,
        "theme": None,
        "theme_label_zh": None,
        "theme_label_en": None,
        "constituents": [],
    }

    assert dataclasses.asdict(by_identifier["golden_theme"]) == {
        "name": "金测试主题",
        "identifier": "golden_theme",
        "asset_type": "STOCK",
        "current_price": Decimal("210.0"),
        "prev_price": Decimal("200.0"),
        "pct_change": Decimal("0.0520"),  # value-weighted: (9000*.05 + 1000*.07)/10000
        "threshold": Decimal("0.05"),
        "trigger": "single_day",
        "market": "US",
        "baseline_date": date(2026, 6, 2),
        "latest_date": date(2026, 6, 3),
        "window_net_pct": Decimal("0.0520"),
        "max_day_pct": Decimal("0.0500"),  # dominant constituent's own max_day_pct
        "max_day_date": date(2026, 6, 3),
        "prev_close": Decimal("200.0"),
        "day_open": None,
        "day_high": None,
        "day_low": None,
        "day_close": Decimal("210.0"),
        "after_hours": None,
        "theme": "golden_theme",
        "theme_label_zh": "金测试主题",
        "theme_label_en": "Golden Test Theme",
        "constituents": [
            {
                "name": "Theme Big",
                "identifier": "THMBIG",
                "pct_change": Decimal("0.0500"),
                "current_value": Decimal("9000"),
            },
            {
                "name": "Theme Small",
                "identifier": "THMSML",
                "pct_change": Decimal("0.0700"),
                "current_value": Decimal("1000"),
            },
        ],
    }
