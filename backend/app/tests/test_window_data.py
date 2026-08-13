"""Tests for the incremental-window data layer (ADR-002 steps 3-4)."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.timezones import ET
from app.models.holding import Holding
from app.models.news import News
from app.models.news_surfaced import NewsSurfaced
from app.models.price_snapshot import PriceSnapshot
from app.models.report import Report
from app.services.window_data import (
    BOOTSTRAP_WATERMARK,
    _window_closes,
    detect_window_anomalies,
    load_news_window,
    mark_news_surfaced,
    user_watermark,
)

_USER = uuid.UUID("00000000-0000-0000-0000-000000000001")


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
        db_session, datetime(2026, 6, 2, tzinfo=UTC), datetime(2026, 6, 6, tzinfo=UTC)
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
        db_session, datetime(2026, 6, 5, tzinfo=UTC), datetime(2026, 6, 7, tzinfo=UTC)
    )
    assert {i.url_hash for i in items} == {"straggler"}

    mark_news_surfaced(db_session, report.id, [i.url_hash for i in items])
    db_session.flush()

    # A later window (e.g. a same-day manual re-run) must not resurface it.
    items_again = load_news_window(
        db_session, datetime(2026, 6, 5, tzinfo=UTC), datetime(2026, 6, 8, tzinfo=UTC)
    )
    assert items_again == []


def test_mark_news_surfaced_idempotent_on_redelivery(db_session: Session) -> None:
    """A Celery redelivery (task_acks_late) of the same generation run must not
    raise IntegrityError re-inserting the same news_id."""
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

    mark_news_surfaced(db_session, report.id, ["dup"])
    db_session.flush()
    mark_news_surfaced(db_session, report.id, ["dup"])  # redelivery — must not raise
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

    mark_news_surfaced(db_session, report.id, [])  # must not raise or query

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

    anomalies, trading_days = detect_window_anomalies(db_session, start, end)
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

    _, trading_days = detect_window_anomalies(db_session, start, end)
    assert trading_days == 1


def test_trading_days_same_day_window_excludes_stale_capture(db_session: Session) -> None:
    """A close captured before period_start (e.g. a backfilled row sharing
    today's trade_date) must not count toward trading_days for a same-day window."""
    start = datetime(2026, 6, 5, 10, 0, tzinfo=ET)
    end = datetime(2026, 6, 5, 17, 0, tzinfo=ET)
    db_session.add(_close_at("DDD", date(2026, 6, 5), 100.0, datetime(2026, 6, 5, 8, 0, tzinfo=ET)))
    db_session.flush()

    _, trading_days = detect_window_anomalies(db_session, start, end)
    assert trading_days == 0
