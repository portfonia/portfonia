"""Issue #639 rolling anomaly and rendered-report acceptance."""

from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from unittest.mock import patch

import pytest
from sqlalchemy.orm import Session

from app.core.timezones import ET
from app.models.holding import Holding
from app.models.price_snapshot import PriceSnapshot
from app.models.ticker_theme import TickerTheme
from app.services import window_data
from app.services.price_anomaly_detector import PriceAnomaly
from app.services.window_data import HoldingMove, _compute_identifier_move, select_user_anomalies
from app.tests import test_window_data as regression
from app.tests.test_issue_639_reports import USER, anomaly, generate

END = datetime(2026, 10, 2, 17, tzinfo=ET)
START = datetime(2026, 9, 30, 17, tzinfo=ET)
DAYS = [
    date(2026, 10, 2),
    date(2026, 10, 1),
    date(2026, 9, 30),
    date(2026, 9, 29),
    date(2026, 9, 28),
    date(2026, 9, 25),
]


def move(session: Session, closes: list[Decimal]) -> HoldingMove:
    session.add_all(
        [
            PriceSnapshot(
                ticker="AAA",
                market="US",
                session_node="close",
                trade_date=d,
                captured_at=datetime(d.year, d.month, d.day, 16, tzinfo=ET),
                close=c,
            )
            for d, c in zip(DAYS, closes, strict=False)
        ]
    )
    session.flush()
    result = _compute_identifier_move(session, "AAA", START, END, START.date())
    assert result is not None
    return result


def holding(asset_class: str = "STOCK") -> Holding:
    return Holding(
        user_id=USER,
        name="AAA",
        ticker="AAA",
        asset_type="stock",
        asset_class=asset_class,
        pricing_mode="auto",
        currency="USD",
        shares=Decimal(1),
    )


def quiet_window(latest: str, third: str, fifth: str | None = None) -> list[Decimal]:
    price = Decimal(latest)
    return [price, price / Decimal("1.01"), price / Decimal("1.03"), Decimal(third)] + (
        [Decimal(third), Decimal(fifth)] if fifth else []
    )


@pytest.mark.parametrize("latest,expected", [("116", "d3"), ("114", None)])
def test_639_09_three_day_move_triggers_beyond_quiet_window(
    db_session: Session, latest: str, expected: str | None
) -> None:
    raw = move(db_session, quiet_window(latest, "100"))
    result = select_user_anomalies({"AAA": raw}, [holding()], 2, {}, {})
    assert [a.trigger for a in result] == ([expected] if expected else [])
    assert raw.net_pct == Decimal(".0300")
    assert abs(raw.max_day_pct or Decimal(0)) < Decimal(".05")
    assert raw.d3_pct == (Decimal(latest) / 100 - 1).quantize(Decimal(".0001"))


@pytest.mark.parametrize(
    "latest,third,fifth,leverage,expected",
    [
        ("121", "110", "100", 1, "d5"),
        ("130", "100", "110", 2, "d3"),
        ("140", "130", "100", 2, "d5"),
        ("129", "100", "110", 2, None),
        ("139", "130", "100", 2, None),
    ],
)
def test_639_10_five_day_and_leveraged_thresholds(
    db_session: Session, latest: str, third: str, fifth: str, leverage: int, expected: str | None
) -> None:
    raw = move(db_session, quiet_window(latest, third, fifth))
    result = select_user_anomalies(
        {"AAA": raw}, [holding("EQUITY_US_BROAD")], 2, {}, {"AAA": Decimal(leverage)}
    )
    assert [a.trigger for a in result] == ([expected] if expected else [])


def test_639_11_single_day_keeps_priority_over_five_day(db_session: Session) -> None:
    raw = move(db_session, [Decimal(x) for x in (121, 110, 110, 110, 110, 100)])
    assert raw.d5_pct == Decimal(".2100")
    assert select_user_anomalies({"AAA": raw}, [holding()], 2, {}, {})[0].trigger == "single_day"


@pytest.mark.parametrize(
    "case",
    [
        regression.test_detect_window_anomalies_flags_move_over_threshold,
        regression.test_single_day_trigger_catches_violent_session,
        regression.test_cumulative_threshold_scales_with_trading_days,
        regression.test_detect_window_anomalies_single_user_golden_fields,
        regression.test_select_user_anomalies_matches_mixed_case_ticker_to_global_move,
        regression.test_select_user_anomalies_no_cross_user_leakage,
        regression.test_select_user_anomalies_skips_manual_pricing_mode,
        regression.test_cumulative_threshold_capped_at_ten_percent,
        regression.test_premarket_window_includes_start_date_close_as_anomaly,
        regression.test_select_user_anomalies_threshold_differs_by_user_asset_class,
        regression.test_select_user_anomalies_leverage_widens_cumulative_threshold,
        regression.test_select_user_anomalies_leverage_widens_single_day_threshold,
        regression.test_detect_window_anomalies_reads_leverage_override_from_db,
    ],
)
def test_639_11_existing_anomaly_rules_retain_results(
    db_session: Session, case: Callable[[Session], None]
) -> None:
    from app.tests.conftest import seed_user

    seed_user(db_session, regression._USER)
    seed_user(db_session, regression._USER_B)
    original = window_data.select_user_anomalies

    def compare(
        moves: dict[str, HoldingMove],
        holdings: Sequence[Holding],
        trading_days: int,
        theme_map: dict[str, TickerTheme],
        leverage_map: dict[str, Decimal],
    ) -> list[PriceAnomaly]:
        prior = original(
            {key: replace(value, d3_pct=None, d5_pct=None) for key, value in moves.items()},
            holdings,
            trading_days,
            theme_map,
            leverage_map,
        )
        current = original(moves, holdings, trading_days, theme_map, leverage_map)
        current_triggers = {a.identifier: a.trigger for a in current}
        for existing in prior:
            assert current_triggers[existing.identifier] == existing.trigger
        return current

    with (
        patch.object(window_data, "select_user_anomalies", side_effect=compare),
        patch.object(regression, "select_user_anomalies", side_effect=compare),
    ):
        case(db_session)


def test_639_12_insufficient_history_has_no_rolling_trigger(db_session: Session) -> None:
    raw = move(db_session, [Decimal(102), Decimal(101), Decimal(100)])
    assert raw.d3_pct is None
    assert raw.d5_pct is None
    assert select_user_anomalies({"AAA": raw}, [holding()], 2, {}, {}) == []


def test_639_13_rendered_report_has_rolling_columns_and_plain_triggers(db_session: Session) -> None:
    entries = []
    for i, trigger in enumerate(("single_day", "cumulative", "d3", "d5")):
        entry = anomaly(f"AAA{i}", ".03")
        entry.trigger = trigger
        entry.d3_pct = Decimal(".16")
        entry.d5_pct = Decimal(".21")
        entries.append(entry)
    report = generate(db_session, [], entries)
    rendered = report.report_md or ""
    assert "| Net % | 3-day % | 5-day % |" in rendered
    for description in ("single day", "window total", "3-day total", "5-day total"):
        assert description in rendered
    assert "single_day" not in rendered
    assert "cumulative" not in rendered
    assert "d3" not in rendered
    assert "d5" not in rendered


@pytest.mark.parametrize("missing", [False, True])
def test_639_theme_rolling_measures_share_window_weights(missing: bool) -> None:
    small, large = holding(), holding()
    large.shares = Decimal(3)
    first, second = anomaly("AAA", ".06"), anomaly("BBB", ".08")
    first.d3_pct, second.d3_pct = Decimal(".16"), Decimal(".24")
    first.d5_pct, second.d5_pct = Decimal(".21"), Decimal(".29")
    if missing:
        first.d3_pct = first.d5_pct = None
    theme = TickerTheme(
        ticker="AAA",
        theme="fixture",
        theme_label_zh="Fixture",
        theme_label_en="Fixture",
        asset_class="STOCK",
    )
    merged = window_data._merge_theme_anomalies([(small, first), (large, second)], theme)
    assert merged.d3_pct == (None if missing else Decimal(".2200"))
    assert merged.d5_pct == (None if missing else Decimal(".2700"))


def test_639_11_cumulative_keeps_priority_over_five_day(db_session: Session) -> None:
    raw = move(db_session, [Decimal(x) for x in ("110.04", "104.9", "100", "100", "100", "90")])
    assert raw.max_day_pct is not None and abs(raw.max_day_pct) < Decimal(".05")
    assert raw.net_pct > Decimal(".10")
    assert raw.d5_pct is not None and raw.d5_pct >= Decimal(".20")
    result = select_user_anomalies({"AAA": raw}, [holding()], 2, {}, {})
    assert result[0].trigger == "cumulative"
