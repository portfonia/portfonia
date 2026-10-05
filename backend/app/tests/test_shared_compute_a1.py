"""Cross-user report fan-out regressions for scheduled shared intelligence."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.price_snapshot import PriceSnapshot
from app.models.report import Report
from app.services import window_data
from app.services.portfolio_calculator import Concentration, PortfolioSnapshot
from app.tests.conftest import SHARED_COMPUTE_NOW

_BASELINE_DATE = (SHARED_COMPUTE_NOW - timedelta(days=7)).date()
_BASELINE_AT = (SHARED_COMPUTE_NOW - timedelta(days=7)).replace(hour=16)


def _close(ticker: str, d: date, value: float) -> PriceSnapshot:
    return PriceSnapshot(
        ticker=ticker, market="US", session_node="close", trade_date=d, close=Decimal(str(value))
    )


def _close_at(ticker: str, d: date, value: float, captured_at: datetime) -> PriceSnapshot:
    return PriceSnapshot(
        ticker=ticker,
        market="US",
        session_node="close",
        trade_date=d,
        close=Decimal(str(value)),
        captured_at=captured_at,
    )


def _seed_price_snapshots(db_session: Session) -> None:
    db_session.add_all(
        [
            _close_at("NVDA", _BASELINE_DATE, 200, _BASELINE_AT),
            *[_close("NVDA", date(2026, 6, day), 215) for day in range(2, 7)],
            _close_at("AAPL", _BASELINE_DATE, 100, _BASELINE_AT),
            _close("AAPL", date(2026, 6, 2), 102.5),
            _close("AAPL", date(2026, 6, 3), 105.06),
            _close("AAPL", date(2026, 6, 4), 107.69),
            _close("AAPL", date(2026, 6, 5), 110.39),
            _close("AAPL", date(2026, 6, 6), 113.14),
            _close_at("SGOL", _BASELINE_DATE, 180, _BASELINE_AT),
            *[_close("SGOL", date(2026, 6, day), 190) for day in range(2, 7)],
        ]
    )
    db_session.flush()


def _anomalies(db_session: Session, user_id: object) -> set[str]:
    report = db_session.execute(
        select(Report).where(Report.user_id == user_id, Report.session_node != "fixture_seed")
    ).scalar_one()
    assert report.report_inputs is not None
    return {item["identifier"] for item in report.report_inputs["price_anomalies"]}


def _run_batch() -> None:
    from app.tasks.report_tasks import generate_incremental_report

    with (
        patch("app.services.report_generator._openrouter_client", return_value=MagicMock()),
        patch(
            "app.services.report_generator._call_llm",
            return_value="## §2 Macro Events\n\nNothing.\n\n## §3 Holdings Intelligence\n\nNothing.\n\n## §4 Exposure & Price Data\n\nNothing.\n\n"
            + "filler " * 500,
        ),
        patch("app.services.report_translation._openrouter_client", return_value=MagicMock()),
        patch("app.services.report_translation._call_llm", return_value="translated " * 500),
        patch("app.services.report_translation.time.sleep"),
    ):
        result = generate_incremental_report.run()
    assert result["status"] == "completed"


def test_no_cross_user_leakage_across_the_batch(
    db_session: Session, three_user_holdings: dict[str, object]
) -> None:
    _seed_price_snapshots(db_session)
    _run_batch()
    assert "NVDA" not in _anomalies(db_session, three_user_holdings["U3"])
    assert "AAPL" not in _anomalies(db_session, three_user_holdings["U3"])


def test_per_user_threshold_divergence_on_the_same_shared_identifier(
    db_session: Session, three_user_holdings: dict[str, object]
) -> None:
    _seed_price_snapshots(db_session)
    _run_batch()
    assert _anomalies(db_session, three_user_holdings["U1"]) == {"NVDA", "AAPL"}
    assert _anomalies(db_session, three_user_holdings["U2"]) == {"NVDA"}


def test_sgol_only_flags_for_the_user_who_holds_it(
    db_session: Session, three_user_holdings: dict[str, object]
) -> None:
    _seed_price_snapshots(db_session)
    _run_batch()
    assert _anomalies(db_session, three_user_holdings["U3"]) == {"gold"}


def test_compute_global_moves_runs_once_per_distinct_window_not_per_user(
    db_session: Session, three_user_holdings: dict[str, object]
) -> None:
    _seed_price_snapshots(db_session)
    with patch(
        "app.services.window_data.compute_global_moves", wraps=window_data.compute_global_moves
    ) as spy:
        _run_batch()
    assert spy.call_count == 1
    assert (
        len(
            db_session.execute(select(Report).where(Report.session_node != "fixture_seed"))
            .scalars()
            .all()
        )
        == 3
    )


def _empty_portfolio_snap() -> PortfolioSnapshot:
    return PortfolioSnapshot(
        base_currency="USD",
        holdings=[],
        total_base=Decimal("0"),
        by_currency={},
        by_asset_type={},
        by_market={},
        by_asset_class={},
        concentration=Concentration(
            top_holding_name="",
            top_holding_ratio=Decimal("0"),
            top_holding_asset_class="",
            top3_ratio=Decimal("0"),
            top_asset_class_name="",
            top_asset_class_ratio=Decimal("0"),
            single_holding_watch=False,
            single_holding_high=False,
            top3_watch=False,
            asset_class_watch=False,
            asset_class_high=False,
        ),
        stale_tickers=[],
    )
